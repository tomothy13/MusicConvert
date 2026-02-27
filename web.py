from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import asyncio
import uuid
import os
import zipfile
from pathlib import Path
import queue
import logging
from logging.handlers import RotatingFileHandler

from main import download_url_to_m4a, sanitize_filename
from logging_setup import setup_logging
from db import init_db, create_album, add_song, list_albums, get_album, get_song, extract_metadata, update_album_art, add_link
from db import list_songs, list_links
from mutagen.mp4 import MP4
import sqlite3
import yt_dlp

# ----------------------
# Configuration & Setup
# ----------------------
HERE = Path(__file__).parent.resolve()
OUTPUT_ROOT = HERE / 'web_output'
OUTPUT_ROOT.mkdir(exist_ok=True)

# Initialize centralized logging for the web process
logger = setup_logging('musicconvert.web')

# Initialize SQLite DB
DB_PATH = HERE / 'musicconvert.db'
db_conn = init_db(DB_PATH)

app = FastAPI()
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


# ----------------------
# Class-based Web Server
# ----------------------
class MusicConvertServer:
    """Encapsulates job queues, processing and route handlers."""

    def __init__(self):
        # job_id -> asyncio.Queue[str]
        self.job_queues: dict[str, asyncio.Queue] = {}
        self.job_zip_paths: dict[str, Path] = {}

    # Route: index page
    async def index(self, request: Request):
        return templates.TemplateResponse('index.html', {"request": request})

    # Route: enqueue a job (POST form)
    async def enqueue(self, request: Request):
        form = await request.form()
        raw = form.get('links', '')
        parts = [p.strip() for p in raw.replace(',', '\n').split('\n') if p.strip()]
        if not parts:
            logger.info('enqueue called with no links')
            return {"status": "no links"}

        job_id = str(uuid.uuid4())
        q: asyncio.Queue = asyncio.Queue()
        self.job_queues[job_id] = q

        # schedule background worker
        asyncio.create_task(self._process_links(job_id, parts, q))
        return {"job_id": job_id}

    # Route: WebSocket for logs
    async def ws_handler(self, websocket: WebSocket, job_id: str):
        await websocket.accept()
        q = self.job_queues.get(job_id)
        if q is None:
            await websocket.send_text('Unknown job id')
            await websocket.close()
            logger.warning('WebSocket connection for unknown job id: %s', job_id)
            return
        try:
            while True:
                msg = await q.get()
                try:
                    await websocket.send_text(msg)
                except Exception:
                    # Client disconnected; stop sending further messages
                    logger.info('WebSocket client disconnected for job %s', job_id)
                    break
                if msg == '__DONE__':
                    zip_path = self.job_zip_paths.get(job_id)
                    if zip_path:
                        try:
                            await websocket.send_text(f'ZIP_READY:{zip_path.name}')
                        except Exception:
                            logger.info('WebSocket client disconnected before ZIP_READY for job %s', job_id)
                    try:
                        await websocket.close()
                    except Exception:
                        pass
                    break
        except Exception as e:
            logger.exception('Exception in ws_handler for job %s: %s', job_id, e)
            try:
                await websocket.close()
            except Exception:
                pass

    # Route: download resulting ZIP
    async def download(self, job_id: str):
        zip_path = self.job_zip_paths.get(job_id)
        if not zip_path or not zip_path.exists():
            return {"status": "not_ready"}
        return FileResponse(path=str(zip_path), filename=zip_path.name, media_type='application/zip')

    # Background worker: process links and emit log messages
    async def _process_links(self, job_id: str, links: list[str], q: asyncio.Queue):
        job_dir = OUTPUT_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        archive_file = str(job_dir / 'archive.txt')
        error_file = str(job_dir / 'error.txt')

        await q.put(f'Job {job_id} started, {len(links)} link(s)')

        conn = db_conn
        for idx, link in enumerate(links, start=1):
            await q.put(f'[{idx}/{len(links)}] Starting: {link}')
            try:
                # Probe the link to obtain playlist/title information for duplication checks
                try:
                    with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as probe_ydl:
                        info = probe_ydl.extract_info(link, download=False)
                except Exception:
                    info = {}

                probe_title = (info.get('title') or info.get('playlist_title') or '').strip()

                # Check DB for existing album with same name (case-insensitive)
                if probe_title:
                    try:
                        cur = conn.cursor()
                        cur.execute('SELECT id FROM albums WHERE lower(name) = ?', (probe_title.lower(),))
                        if cur.fetchone():
                            await q.put(f'[{idx}/{len(links)}] Skipping: album "{probe_title}" already exists in database')
                            try:
                                add_link(conn, link, 'skipped:album_exists')
                            except Exception:
                                pass
                            continue
                    except Exception:
                        pass

                # Check archive files for exact URL match to avoid duplicates
                already_seen = False
                try:
                    for jobdir in OUTPUT_ROOT.iterdir():
                        if not jobdir.is_dir():
                            continue
                        af = jobdir / 'archive.txt'
                        if af.exists():
                            try:
                                with open(af, 'r', encoding='utf-8') as f:
                                    for ln in f:
                                        if ln.strip().endswith('\t' + link) or ('\t' + link) in ln:
                                            already_seen = True
                                            break
                            except Exception:
                                continue
                        if already_seen:
                            break
                except Exception:
                    already_seen = False

                if already_seen:
                    await q.put(f'[{idx}/{len(links)}] Skipping: link already processed previously')
                    try:
                        add_link(conn, link, 'skipped:already_seen')
                    except Exception:
                        pass
                    continue

                # Create a thread-safe queue for progress messages from the downloader
                thread_q: queue.Queue = queue.Queue()

                # Forwarder: move thread queue messages into the asyncio queue for WebSocket
                async def _forward_thread_q(tq: queue.Queue, out_q: asyncio.Queue):
                    import time as _time
                    while True:
                        try:
                            msg = tq.get_nowait()
                        except Exception:
                            await asyncio.sleep(0.1)
                            continue
                        await out_q.put(msg)
                        if msg == '__DL_DONE__':
                            break

                forwarder = asyncio.create_task(_forward_thread_q(thread_q, q))

                # run blocking download in threadpool and forward its progress
                try:
                    add_link(conn, link, 'queued')
                except Exception:
                    pass
                ok = await asyncio.to_thread(download_url_to_m4a, link, str(job_dir), archive_file, error_file, thread_q)

                # wait for forwarder to finish consuming progress messages
                await forwarder

                await q.put(f'[{idx}/{len(links)}] Finished: {link} -> {"OK" if ok else "FAILED"}')
                try:
                    add_link(conn, link, 'success' if ok else 'failed')
                except Exception:
                    pass
            except Exception as e:
                logger.exception('Error processing link %s for job %s: %s', link, job_id, e)
                await q.put(f'[{idx}/{len(links)}] Exception: {e}')
                try:
                    add_link(conn, link, f'error:{e}')
                except Exception:
                    pass

        # Populate DB with albums/songs found in job_dir
        try:
            await q.put('Indexing files into database...')
            # Use db_conn created at module scope
            conn = db_conn
            # Scan directories: each subdirectory is treated as an album
            for entry in job_dir.iterdir():
                if entry.is_dir():
                    album_name = entry.name
                    album_id = create_album(conn, album_name, str(entry))
                    # collect potential album artist/cover from first song
                    album_artist = None
                    album_art = None
                    for f in entry.rglob('*.m4a'):
                        meta = extract_metadata(str(f))
                        sid = add_song(conn, album_id, f.name, str(f), meta)
                        # try to read embedded cover using mutagen
                        try:
                            m = MP4(str(f))
                            covr = m.tags.get('covr') if m.tags else None
                            if covr and len(covr) > 0 and album_art is None:
                                try:
                                    album_art = bytes(covr[0])
                                except Exception:
                                    try:
                                        album_art = covr[0]
                                    except Exception:
                                        album_art = None
                        except Exception:
                            pass
                        if not album_artist and meta.get('artist'):
                            album_artist = meta.get('artist')
                    # update album with artist and art blob if found
                    if album_id:
                        try:
                            if album_art:
                                update_album_art(conn, album_id, album_artist, album_art)
                            elif album_artist:
                                update_album_art(conn, album_id, album_artist, None)
                        except Exception:
                            pass
                elif entry.is_file() and entry.suffix.lower() in ('.m4a', '.m4a'):
                    # single-file downloads: create an album for the job if needed
                    album_name = f'job_{job_id}'
                    album_id = create_album(conn, album_name, str(job_dir))
                    meta = extract_metadata(str(entry))
                    add_song(conn, album_id, entry.name, str(entry), meta)
            await q.put('Indexing complete')
        except Exception as e:
            logger.exception('Failed to index job %s into database: %s', job_id, e)
            await q.put(f'Indexing error: {e}')

        # create zip archive AFTER indexing so files are present and DB reflects them
        zip_name = f'music_{job_id}.zip'
        zip_path = OUTPUT_ROOT / zip_name
        await q.put('Creating ZIP archive...')
        try:
            added_count = 0
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(job_dir):
                    for f in files:
                        full = Path(root) / f
                        rel = full.relative_to(job_dir)
                        try:
                            zf.write(full, arcname=rel)
                            added_count += 1
                        except Exception:
                            logger.exception('Failed to add file %s to zip for job %s', full, job_id)
            # Always notify client a ZIP creation attempt completed; then provide READY state
            await q.put('ZIP_CREATED')
            if added_count > 0:
                self.job_zip_paths[job_id] = zip_path
                # notify immediately that the ZIP is ready for download
                await q.put(f'ZIP_READY:{zip_path.name}')
            else:
                # signal empty so client shows helpful message
                await q.put('ZIP_READY:empty')
        except Exception as e:
            logger.exception('Failed to create ZIP for job %s: %s', job_id, e)
            await q.put(f'Failed to create ZIP: {e}')

        await q.put('__DONE__')
        # remove this job from active queues so admin list clears
        try:
            self.job_queues.pop(job_id, None)
        except Exception:
            pass


# ----------------------
# Register routes
# ----------------------
server = MusicConvertServer()
app.get('/')(server.index)
app.post('/enqueue')(server.enqueue)
app.websocket('/ws/{job_id}')(server.ws_handler)
app.get('/download/{job_id}')(server.download)

# API: albums listing and album detail
@app.get('/api/albums')
async def api_albums():
    try:
        albums = list_albums(db_conn)
        return {'albums': albums}
    except Exception as e:
        logger.exception('api_albums error: %s', e)
        return {'error': str(e)}


@app.get('/api/songs')
async def api_songs():
    try:
        songs = list_songs(db_conn)
        return {'songs': songs}
    except Exception as e:
        logger.exception('api_songs error: %s', e)
        return {'error': str(e)}


@app.get('/api/cover/{song_id}')
async def api_cover(song_id: int):
    try:
        song = get_song(db_conn, song_id)
        if not song:
            return Response(status_code=404)
        path = song.get('filepath')
        if not path or not Path(path).exists():
            return Response(status_code=404)
        try:
            mp4 = MP4(path)
            covr = mp4.tags.get('covr')
            if covr and len(covr) > 0:
                data = covr[0]
                # ensure bytes
                try:
                    data = bytes(data)
                except Exception:
                    pass
                # try to guess image type via first bytes
                if isinstance(data, (bytes, bytearray)) and data[:8].startswith(b'\x89PNG'):
                    ctype = 'image/png'
                elif data[:3] == b'GIF':
                    ctype = 'image/gif'
                else:
                    ctype = 'image/jpeg'
                return Response(content=data, media_type=ctype)
        except Exception:
            logger.exception('Failed to extract cover art for song %s', song_id)
            return Response(status_code=204)
        return Response(status_code=204)
    except Exception as e:
        logger.exception('api_cover error: %s', e)
        return Response(status_code=500)


@app.get('/api/album-cover/{album_id}')
async def api_album_cover(album_id: int):
    try:
        cur = db_conn.cursor()
        cur.execute('SELECT art FROM albums WHERE id = ?', (album_id,))
        r = cur.fetchone()
        if r and r[0]:
            data = r[0]
            try:
                data = bytes(data)
            except Exception:
                pass
            # guess type
            if isinstance(data, (bytes, bytearray)) and data[:8].startswith(b'\x89PNG'):
                ctype = 'image/png'
            else:
                ctype = 'image/jpeg'
            return Response(content=data, media_type=ctype)
        # fallback: try first song cover
        album = get_album(db_conn, album_id)
        if album and album.get('songs'):
            sid = album['songs'][0]['id']
            return await api_cover(sid)
        return Response(status_code=204)
    except Exception as e:
        logger.exception('api_album_cover error: %s', e)
        return Response(status_code=500)


@app.get('/api/links')
async def api_links():
    try:
        # Scan OUTPUT_ROOT job directories for archive.txt files
        links = []
        for jobdir in OUTPUT_ROOT.iterdir():
            if jobdir.is_dir():
                af = jobdir / 'archive.txt'
                if af.exists():
                    try:
                        with open(af, 'r', encoding='utf-8') as f:
                            for ln in f:
                                ln = ln.strip()
                                if not ln: continue
                                parts = ln.split('\t')
                                if len(parts) >= 2:
                                    links.append({'album': parts[0], 'url': parts[1], 'job': jobdir.name})
                                else:
                                    links.append({'line': ln, 'job': jobdir.name})
                    except Exception:
                        logger.exception('Failed to read archive for job %s', jobdir)
        return {'links': links}
    except Exception as e:
        logger.exception('api_links error: %s', e)
        return {'error': str(e)}


@app.get('/api/admin/logs')
async def api_admin_logs():
    try:
        # Return last ~200 lines of server error log
        log_path = Path(__file__).parent / 'error.log'
        if not log_path.exists():
            return {'lines': []}
        with open(log_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b''
            while size > 0 and len(data) < 200 * 200:
                read_size = min(block, size)
                f.seek(size - read_size)
                data = f.read(read_size) + data
                size -= read_size
            text = data.decode('utf-8', errors='replace')
            lines = text.strip().splitlines()[-200:]
        return {'lines': lines}
    except Exception as e:
        logger.exception('api_admin_logs error: %s', e)
        return {'error': str(e)}


@app.get('/api/admin/jobs')
async def api_admin_jobs():
    try:
        # Return a list of active job ids
        jobs = list(server.job_queues.keys())
        return {'jobs': jobs}
    except Exception as e:
        logger.exception('api_admin_jobs error: %s', e)
        return {'error': str(e)}


@app.post('/api/admin/query')
async def api_admin_query(request: Request):
    try:
        # Simple, restricted SQL runner for admins. No password required (SELECT-only).
        body = await request.json()
        query = body.get('query')
        if not query or not isinstance(query, str):
            return {'error': 'invalid_query'}
        q = query.strip()
        # Only allow SELECT queries and single statement
        if not q.lower().startswith('select') or ';' in q:
            return {'error': 'only_select_allowed'}
        # Execute safely
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(q)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(1000)
        conn.close()
        return {'columns': cols, 'rows': rows}
    except Exception as e:
        logger.exception('api_admin_query error: %s', e)
        return {'error': str(e)}


@app.get('/api/albums/{album_id}')
async def api_album(album_id: int):
    try:
        album = get_album(db_conn, album_id)
        if not album:
            return {'error': 'not_found'}
        return album
    except Exception as e:
        logger.exception('api_album error: %s', e)
        return {'error': str(e)}


@app.get('/download/song/{song_id}')
async def download_song(song_id: int):
    try:
        song = get_song(db_conn, song_id)
        if not song:
            return {'status': 'not_found'}
        path = song.get('filepath')
        if not path or not Path(path).exists():
            return {'status': 'missing'}
        return FileResponse(path, filename=song.get('filename'))
    except Exception as e:
        logger.exception('download_song error: %s', e)
        return {'error': str(e)}


@app.get('/download/album/{album_id}')
async def download_album(album_id: int):
    try:
        album = get_album(db_conn, album_id)
        if not album:
            return {'status': 'not_found'}
        # create a zip in OUTPUT_ROOT named after the album
        safe_name = sanitize_filename(album.get('name') or f'album_{album_id}')
        zip_name = f"{safe_name}.zip"
        zip_path = OUTPUT_ROOT / zip_name
        added = 0
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for s in album.get('songs', []):
                fp = Path(s.get('filepath') or '')
                if not fp.exists():
                    # try album directory fallback
                    adir = album.get('directory')
                    if adir:
                        alt = Path(adir) / s.get('filename')
                        if alt.exists():
                            fp = alt
                if fp and fp.exists():
                    arcname = fp.name
                    try:
                        zf.write(fp, arcname=arcname)
                        added += 1
                    except Exception:
                        logger.exception('Failed to add %s to album zip %s', fp, zip_path)
        if added == 0:
            return {'status': 'empty'}
        return FileResponse(str(zip_path), filename=zip_name, media_type='application/zip')
    except Exception as e:
        logger.exception('download_album error: %s', e)
        return {'error': str(e)}


if __name__ == '__main__':
    import uvicorn
    # Read host/port from environment for easy homelab deployment
    host = os.environ.get('WEB_HOST', '0.0.0.0')
    port = int(os.environ.get('WEB_PORT', '8000'))
    print(f"Starting MusicConvert web UI on http://{host}:{port}")
    uvicorn.run('web:app', host=host, port=port, reload=False)

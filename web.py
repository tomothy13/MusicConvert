from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import asyncio
import uuid
import os
import zipfile
from pathlib import Path
import queue
import logging
import tempfile
from logging.handlers import RotatingFileHandler

from main import download_url_to_m4a, sanitize_filename
from logging_setup import setup_logging
from db import init_db, create_album, add_song, list_albums, get_album, get_song, extract_metadata, update_album_art, add_link, update_album_zip
from db import list_songs, list_links
from mutagen.mp4 import MP4, MP4Cover
import sqlite3
import subprocess
import re
import yt_dlp

# ----------------------
# Configuration & Setup
# ----------------------
HERE = Path(__file__).parent.resolve()
# central data directory (keeps outputs out of repo root)
DATA_ROOT = HERE / 'data'
DATA_ROOT.mkdir(exist_ok=True)
OUTPUT_ROOT = DATA_ROOT / 'files'
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

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
        # job_id -> list of (arcname, filepath) tuples for on-demand zipping
        self.job_contents: dict[str, list[tuple[str,str]]] = {}
        # job_id -> list[str] recent log lines
        self.job_logs: dict[str, list[str]] = {}

    async def emit_log(self, job_id: str, msg: str):
        """Append a message to the job's log buffer and push to the queue if present."""
        lst = self.job_logs.setdefault(job_id, [])
        lst.append(msg)
        # cap buffer size
        if len(lst) > 2000:
            del lst[0:len(lst)-2000]
        q = self.job_queues.get(job_id)
        if q:
            try:
                await q.put(msg)
            except Exception:
                logger.exception('Failed to put message onto job queue %s', job_id)

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
        # Accept connection. If the job is active, stream its queue. If the job already finished
        # but has a ZIP, allow a one-off connection to report ZIP_READY so clients can download.
        await websocket.accept()
        q = self.job_queues.get(job_id)
        if q is None:
            contents = self.job_contents.get(job_id)
            if contents:
                # derive a friendly name
                zip_name = f'music_{job_id}.zip'
                try:
                    await websocket.send_text(f'ZIP_READY:{zip_name}')
                except Exception:
                    pass
                try:
                    await websocket.close()
                except Exception:
                    pass
                logger.info('WebSocket one-off ZIP_READY sent for finished job %s', job_id)
                return
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
                # If the job finished, notify client about ZIP readiness and close
                if msg == '__DONE__':
                    contents = self.job_contents.get(job_id)
                    if contents:
                        try:
                            await websocket.send_text(f'ZIP_READY:music_{job_id}.zip')
                        except Exception:
                            logger.info('WebSocket client disconnected before ZIP_READY for job %s', job_id)
                    try:
                        await websocket.close()
                    except Exception:
                        pass
                    break
                    break
        except Exception as e:
            logger.exception('Exception in ws_handler for job %s: %s', job_id, e)
            try:
                await websocket.close()
            except Exception:
                pass

    # Route: download resulting ZIP
    async def download(self, job_id: str):
        # Create and stream a temporary ZIP on demand from the job's recorded contents.
        contents = self.job_contents.get(job_id)
        if not contents:
            return {"status": "not_ready"}
        # create temp zip file
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
            tmp_name = tmp.name
            tmp.close()
            with zipfile.ZipFile(tmp_name, 'w', zipfile.ZIP_DEFLATED) as zf:
                for arcname, fp in contents:
                    try:
                        if os.path.exists(fp):
                            zf.write(fp, arcname=arcname)
                    except Exception:
                        logger.exception('Failed to add %s to temp zip for job %s', fp, job_id)

            def _stream_file(path):
                try:
                    with open(path, 'rb') as fh:
                        while True:
                            chunk = fh.read(64 * 1024)
                            if not chunk:
                                break
                            yield chunk
                finally:
                    try:
                        os.unlink(path)
                    except Exception:
                        pass

            headers = {'Content-Disposition': f'attachment; filename="music_{job_id}.zip"'}
            return StreamingResponse(_stream_file(tmp_name), media_type='application/zip', headers=headers)
        except Exception as e:
            logger.exception('Failed to create/stream zip for job %s: %s', job_id, e)
            return {'error': str(e)}

    # Background worker: process links and emit log messages
    async def _process_links(self, job_id: str, links: list[str], q: asyncio.Queue):
        job_dir = OUTPUT_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        archive_file = str(job_dir / 'archive.txt')
        error_file = str(job_dir / 'error.txt')

        await self.emit_log(job_id, f'Job {job_id} started, {len(links)} link(s)')

        conn = db_conn
        for idx, link in enumerate(links, start=1):
            await self.emit_log(job_id, f'[{idx}/{len(links)}] Starting: {link}')
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
                            await self.emit_log(job_id, f'[{idx}/{len(links)}] Skipping: album "{probe_title}" already exists in database')
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
                    await self.emit_log(job_id, f'[{idx}/{len(links)}] Skipping: link already processed previously')
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
                        # write into server logs as well
                        await self.emit_log(job_id, msg)
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

                await self.emit_log(job_id, f'[{idx}/{len(links)}] Finished: {link} -> {"OK" if ok else "FAILED"}')
                try:
                    add_link(conn, link, 'success' if ok else 'failed')
                except Exception:
                    pass
            except Exception as e:
                logger.exception('Error processing link %s for job %s: %s', link, job_id, e)
                await self.emit_log(job_id, f'[{idx}/{len(links)}] Exception: {e}')
                try:
                    add_link(conn, link, f'error:{e}')
                except Exception:
                    pass

        # Populate DB with albums/songs found in job_dir
        try:
            await self.emit_log(job_id, 'Indexing files into database...')
            # Debug: list job_dir contents for troubleshooting
            try:
                total_files = 0
                total_dirs = 0
                for p in job_dir.rglob('*'):
                    if p.is_file():
                        total_files += 1
                    elif p.is_dir():
                        total_dirs += 1
                await self.emit_log(job_id, f'Indexing: found {total_dirs} directories and {total_files} files under job dir')
            except Exception:
                pass
            # Use db_conn created at module scope
            conn = db_conn
            # Scan directories: each subdirectory is treated as an album
            for entry in job_dir.iterdir():
                if entry.is_dir():
                    album_name = entry.name
                    album_id = create_album(conn, album_name, str(entry))
                    await self.emit_log(job_id, f'Indexing album folder: {album_name}')
                    # collect potential album artist/cover from first song
                    album_artist = None
                    album_art = None
                    # collect audio files of several common extensions; prefer .m4a if present
                    audio_exts = ('.m4a', '.mp4', '.mp3', '.webm', '.m4b', '.mkv', '.opus')
                    files = [p for p in entry.rglob('*') if p.is_file() and p.suffix.lower() in audio_exts]
                    # sort so .m4a preferred first
                    files.sort(key=lambda p: 0 if p.suffix.lower()=='.m4a' else 1)
                    # pick thumbs if present
                    thumb = None
                    for ext in ('.jpg', '.jpeg', '.png', '.webp'):
                        t = next((p for p in entry.glob(f'*{ext}') if p.is_file()), None)
                        if t:
                            thumb = t
                            break
                    for f in files:
                        try:
                            target = f
                            # Prefer to trust downloader to produce .m4a files.
                            # If file is an MP4/M4A container, attempt to write/read MP4 tags and cover art.
                            meta = {}
                            if f.suffix.lower() in ('.m4a', '.mp4'):
                                try:
                                    mp4 = MP4(str(target))
                                    tags = mp4.tags or {}
                                    # Title
                                    cur_title = tags.get('\xa9nam', [None])[0]
                                    if not cur_title:
                                        stem = target.stem
                                        title = re.sub(r'^\s*\d+\s*-\s*', '', stem).strip()
                                        tags['\xa9nam'] = [title]
                                    # Artist
                                    cur_artist = tags.get('\xa9ART', [None])[0]
                                    if not cur_artist and album_artist:
                                        tags['\xa9ART'] = [album_artist]
                                    # Album
                                    tags['\xa9alb'] = [album_name]
                                    if album_artist:
                                        tags['aART'] = [album_artist]
                                    # Track parse
                                    if 'trkn' not in tags:
                                        mtr = re.match(r"\s*(\d+)\s*-\s*(.*)", target.stem)
                                        if mtr:
                                            try:
                                                tn = int(mtr.group(1))
                                                tags['trkn'] = [(tn, 0)]
                                            except Exception:
                                                pass
                                    # embed cover if a thumbnail image exists
                                    if thumb:
                                        try:
                                            with open(thumb, 'rb') as tfp:
                                                data = tfp.read()
                                            if thumb.suffix.lower() == '.png':
                                                covr = MP4Cover(data, imageformat=MP4Cover.FORMAT_PNG)
                                            else:
                                                covr = MP4Cover(data)
                                            tags['covr'] = [covr]
                                        except Exception:
                                            pass
                                    mp4.tags = tags
                                    mp4.save()
                                except Exception:
                                    # Not a valid MP4 container or tagging failed; continue without tagging
                                    pass
                                # Extract metadata via ffprobe/mutagen
                                meta = extract_metadata(str(target))
                                sid = add_song(conn, album_id, target.name, str(target), meta)
                                await self.emit_log(job_id, f'Added song: {target.name} -> song_id={sid}')
                                # try to pull embedded cover art from file
                                try:
                                    m2 = MP4(str(target))
                                    covr = m2.tags.get('covr') if m2.tags else None
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
                            else:
                                # Non-m4a files: record metadata and add to DB, but do not transcode here.
                                meta = extract_metadata(str(target))
                                sid = add_song(conn, album_id, target.name, str(target), meta)
                                await self.emit_log(job_id, f'Added non-m4a file (no transcode): {target.name} -> song_id={sid}')
                        except Exception as e:
                            await self.emit_log(job_id, f'Failed processing file {f}: {e}')
                        if not album_artist and meta.get('artist'):
                            album_artist = meta.get('artist')
                    # update album with artist and art blob if found
                    if album_id:
                        try:
                            if album_art:
                                update_album_art(conn, album_id, album_artist, album_art)
                                await self.emit_log(job_id, f'Updated album art for album {album_name}')
                            elif album_artist:
                                update_album_art(conn, album_id, album_artist, None)
                                await self.emit_log(job_id, f'Updated album artist for album {album_name}: {album_artist}')
                            else:
                                await self.emit_log(job_id, f'No album artist/art found for {album_name}')
                        except Exception:
                            await self.emit_log(job_id, f'Failed to update album {album_name} metadata')
                elif entry.is_file() and entry.suffix.lower() in ('.m4a', '.m4a'):
                    # single-file downloads: create an album for the job if needed
                    album_name = f'job_{job_id}'
                    album_id = create_album(conn, album_name, str(job_dir))
                    meta = extract_metadata(str(entry))
                    add_song(conn, album_id, entry.name, str(entry), meta)
            await self.emit_log(job_id, 'Indexing complete')
            # remove job-level archive/error files (not needed)
            try:
                af = job_dir / 'archive.txt'
                ef = job_dir / 'error.txt'
                if af.exists():
                    af.unlink()
                if ef.exists():
                    ef.unlink()
                await self.emit_log(job_id, 'Removed job archive/error files')
            except Exception:
                pass
        except Exception as e:
            logger.exception('Failed to index job %s into database: %s', job_id, e)
            await self.emit_log(job_id, f'Indexing error: {e}')

        # Prepare a list of files for on-demand zipping (don't persist zip to project directory)
        await self.emit_log(job_id, 'Preparing files for ZIP (on-demand)...')
        try:
            added_count = 0
            contents = []
            # iterate top-level entries and record per-album files without random parents
            for entry in job_dir.iterdir():
                # skip helper files
                if entry.name in ('archive.txt', 'error.txt'):
                    continue
                if entry.is_dir():
                    for f in entry.rglob('*'):
                        if f.is_file():
                            # only include .m4a audio files and common images
                            if f.suffix.lower() != '.m4a' and f.suffix.lower() not in ('.jpg', '.jpeg', '.png'):
                                continue
                            try:
                                rel = Path(entry.name) / f.relative_to(entry)
                                contents.append((str(rel), str(f)))
                                added_count += 1
                            except Exception:
                                logger.exception('Failed to record file %s for job %s', f, job_id)
                elif entry.is_file():
                    try:
                        if entry.suffix.lower() not in ('.m4a', '.jpg', '.jpeg', '.png'):
                            continue
                        contents.append((entry.name, str(entry)))
                        added_count += 1
                    except Exception:
                        logger.exception('Failed to record file %s for job %s', entry, job_id)
            # store contents in memory for on-demand streaming later
            if added_count > 0:
                self.job_contents[job_id] = contents
                await self.emit_log(job_id, 'ZIP_CREATED')
                await self.emit_log(job_id, f'ZIP_READY:music_{job_id}.zip')
            else:
                await self.emit_log(job_id, 'ZIP_CREATED')
                await self.emit_log(job_id, 'ZIP_READY:empty')
        except Exception as e:
            logger.exception('Failed to prepare ZIP contents for job %s: %s', job_id, e)
            await self.emit_log(job_id, f'Failed to prepare ZIP: {e}')

        await self.emit_log(job_id, '__DONE__')
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


@app.get('/api/jobs/{job_id}/logs')
async def api_job_logs(job_id: str):
    try:
        logs = server.job_logs.get(job_id, [])
        return {'lines': logs}
    except Exception as e:
        logger.exception('api_job_logs error: %s', e)
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
        # Stream a temporary zip built on-demand from DB-listed files
        safe_name = sanitize_filename(album.get('name') or f'album_{album_id}')
        zip_name = f"{safe_name}.zip"
        added = 0
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
            tmp_name = tmp.name
            tmp.close()
            with zipfile.ZipFile(tmp_name, 'w', zipfile.ZIP_DEFLATED) as zf:
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
                            logger.exception('Failed to add %s to album zip (temp) for album %s', fp, album_id)
            if added == 0:
                try:
                    os.unlink(tmp_name)
                except Exception:
                    pass
                return {'status': 'empty'}

            def _stream_file(path):
                try:
                    with open(path, 'rb') as fh:
                        while True:
                            chunk = fh.read(64 * 1024)
                            if not chunk:
                                break
                            yield chunk
                finally:
                    try:
                        os.unlink(path)
                    except Exception:
                        pass

            headers = {'Content-Disposition': f'attachment; filename="{zip_name}"'}
            return StreamingResponse(_stream_file(tmp_name), media_type='application/zip', headers=headers)
        except Exception as e:
            logger.exception('download_album streaming error: %s', e)
            return {'error': str(e)}
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

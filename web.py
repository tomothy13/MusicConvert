from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, FileResponse
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

from main import download_url_to_m4a
from logging_setup import setup_logging

# ----------------------
# Configuration & Setup
# ----------------------
HERE = Path(__file__).parent.resolve()
OUTPUT_ROOT = HERE / 'web_output'
OUTPUT_ROOT.mkdir(exist_ok=True)

# Initialize centralized logging for the web process
logger = setup_logging('musicconvert.web')

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
                await websocket.send_text(msg)
                if msg == '__DONE__':
                    zip_path = self.job_zip_paths.get(job_id)
                    if zip_path:
                        await websocket.send_text(f'ZIP_READY:{zip_path.name}')
                    await websocket.close()
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

        for idx, link in enumerate(links, start=1):
            await q.put(f'[{idx}/{len(links)}] Starting: {link}')
            try:
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
                ok = await asyncio.to_thread(download_url_to_m4a, link, str(job_dir), archive_file, error_file, thread_q)

                # wait for forwarder to finish consuming progress messages
                await forwarder

                await q.put(f'[{idx}/{len(links)}] Finished: {link} -> {"OK" if ok else "FAILED"}')
            except Exception as e:
                logger.exception('Error processing link %s for job %s: %s', link, job_id, e)
                await q.put(f'[{idx}/{len(links)}] Exception: {e}')

        # create zip archive
        zip_name = f'music_{job_id}.zip'
        zip_path = OUTPUT_ROOT / zip_name
        await q.put('Creating ZIP archive...')
        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(job_dir):
                    for f in files:
                        full = Path(root) / f
                        rel = full.relative_to(job_dir)
                        zf.write(full, arcname=rel)
            self.job_zip_paths[job_id] = zip_path
            await q.put('ZIP_CREATED')
        except Exception as e:
            logger.exception('Failed to create ZIP for job %s: %s', job_id, e)
            await q.put(f'Failed to create ZIP: {e}')

        await q.put('__DONE__')


# ----------------------
# Register routes
# ----------------------
server = MusicConvertServer()
app.get('/')(server.index)
app.post('/enqueue')(server.enqueue)
app.websocket('/ws/{job_id}')(server.ws_handler)
app.get('/download/{job_id}')(server.download)


if __name__ == '__main__':
    import uvicorn
    # Read host/port from environment for easy homelab deployment
    host = os.environ.get('WEB_HOST', '0.0.0.0')
    port = int(os.environ.get('WEB_PORT', '8000'))
    print(f"Starting MusicConvert web UI on http://{host}:{port}")
    uvicorn.run('web:app', host=host, port=port, reload=False)

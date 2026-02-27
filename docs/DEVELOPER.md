Developer Guide — MusicConvert

Overview
- `web.py` — FastAPI web frontend and job orchestration.
- `main.py` — Downloader using `yt-dlp` and post-processing (converts to .m4a and embeds tags).
- `db.py` — SQLite helpers for albums/songs and metadata extraction via `ffprobe`.
- `templates/index.html` — Frontend UI served by FastAPI templates.
- `logging_setup.py` — Centralized logging to `error.log` and stdout.

How it works
1. UI POSTs to `/enqueue` with newline-separated links.
2. `MusicConvertServer._process_links` runs `download_url_to_m4a` in a thread, streaming progress over a WebSocket.
3. After downloads finish, files are indexed into the SQLite DB and a ZIP is created for download.
4. Library pages query `/api/albums`, `/api/songs`, `/api/links` and individual media endpoints.

Important implementation details
- Safety: SQL calls in `db.py` use parameterized queries for inserts/reads.
- Admin: a restricted, SELECT-only SQL runner is available behind `ADMIN_PASSWORD`.
- Tagging: `main.py` uses `mutagen` to write MP4 tags and embed cover art if a thumbnail was downloaded.

Running locally
- Create a venv: `python3 -m venv venv && source venv/bin/activate`
- Install deps: `pip install -r requirements.txt`
- Start server: `python web.py` (optionally set `WEB_HOST` + `WEB_PORT` env vars)

Testing a conversion
- Use the UI to enqueue a YouTube playlist link (it must contain `list=`).
- Watch the WebSocket status log and download the ZIP when ready.

Error handling and robustness
- All public endpoints include try/except blocks and log exceptions to `error.log`.
- The admin SQL runner limits statements to single `SELECT` queries and forbids `;`.
- The UI validates links client-side and prevents starting a job with invalid entries.

Extending the system
- To add authentication, integrate OAuth or basic auth in `web.py` and protect admin routes.
- To persist longer logs, forward `error.log` to a central log system or rotate more aggressively.
- To support richer metadata, extend `db.add_song` metadata to store original source URL(s).

Notes for maintainers
- Keep `static/` present — FastAPI mounts it at startup and will fail if missing.
- Use `WEB_HOST` to restrict the interface; the system will warn if not configured for LAN.

Contact
- For questions, modify this doc and open an issue in the repository with reproduction steps.

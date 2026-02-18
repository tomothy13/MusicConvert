# MusicConvert — Setup & Quick Usage

A small FastAPI-based service that converts YouTube videos/playlists to iPod-friendly M4A and packages results as a ZIP.

## Setup (recommended for homelab)

Download and run the installer script on your Linux host:

```bash
curl -fsSL https://raw.githubusercontent.com/tomothy13/MusicConvert/main/setup_service.sh -o setup_service.sh
bash setup_service.sh
```

This will create a `venv`, install Python dependencies, detect your LAN IP, and install a `systemd` service named `musicconvert`.

Service logs:
- System journal: `sudo journalctl -u musicconvert -f`
- Project rotating log: `error.log` in the repository root

To uninstall, download and run the teardown script:

```bash
curl -fsSL https://raw.githubusercontent.com/tomothy13/MusicConvert/main/teardown_service.sh -o teardown_service.sh
bash teardown_service.sh
```

## Quick local usage

For local development or a quick test:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
WEB_HOST=127.0.0.1 WEB_PORT=8000 python web.py
```

Open http://127.0.0.1:8000, paste links (one per line), submit, and watch live progress. When finished, download the ZIP.

## Files & logs

- Outputs and ZIPs: `web_output/`
- Server/runtime log: `error.log` (rotating file in project root)
- Setup/teardown script logs (on installer host): `/var/log/musicconvert-setup.log` and `/var/log/musicconvert-teardown.log` if writable, otherwise `setup.log`/`teardown.log` next to the scripts.

## Troubleshooting (short)

- If the web UI is unreachable from LAN, ensure the service bound to the LAN IP (printed by setup) or change `WEB_HOST` in the systemd unit to `0.0.0.0`.
- If ffmpeg is missing, install it manually (`apt install ffmpeg` on Debian/Ubuntu) before running the setup script.
- Check `sudo journalctl -u musicconvert` for runtime errors.

## Links

- Setup script (raw): https://raw.githubusercontent.com/tomothy13/MusicConvert/main/setup_service.sh
- Teardown script (raw): https://raw.githubusercontent.com/tomothy13/MusicConvert/main/teardown_service.sh

MusicConvert — macOS packaging

Building a macOS .app with py2app

1) Create and activate a Python virtualenv (recommended):

```bash
python3 -m venv venv
source venv/bin/activate
```

2) Install runtime + build deps:

```bash
pip install -r requirements.txt
```

3) Install system dependency `ffmpeg` (Homebrew recommended):

```bash
brew install ffmpeg
```

4) Build the .app with py2app:

```bash
python3 setup.py py2app
```

After building, the product will be in `dist/MusicConvert.app`. Move it to `/Applications` or open it from Finder.

Notes

- `ffmpeg` is required at runtime to extract and embed audio/cover art. If not present ytdlp may leave raw webm/webp files.
- The build bundles Python and the listed packages from `requirements.txt`. If you prefer a lightweight wrapper instead of bundling Python, consider using the existing `musicconvert.command` or using Platypus.
- If you want a custom icon, add an `icon.icns` to the project and update `setup.py` OPTIONS with `"iconfile": "icon.icns"`.

Running the web UI (local / homelab)

1) Start the FastAPI server (binds to all interfaces by default):

```bash
cd /Users/tomothy/Documents/MusicConvert
source venv/bin/activate
python web.py
```

The server listens on port `8000` by default and will bind to `0.0.0.0` so other machines on your LAN can reach it. To change host/port, set `WEB_HOST` and `WEB_PORT` environment variables.

2) Example systemd unit for running on a Linux homelab (create `/etc/systemd/system/musicconvert.service`):

```ini
[Unit]
Description=MusicConvert web service
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/MusicConvert
Environment=PATH=/path/to/MusicConvert/venv/bin
Environment=WEB_HOST=0.0.0.0
Environment=WEB_PORT=8000
ExecStart=/path/to/MusicConvert/venv/bin/python /path/to/MusicConvert/web.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now musicconvert
sudo journalctl -u musicconvert -f
```

Notes:
- Ensure `ffmpeg` is installed and available in the PATH used by the service.
- For macOS persistent launches, use `launchd`/`launchctl` instead of systemd.

Setup (Ubuntu VM)

1) Copy or clone the repo onto your Ubuntu VM.
2) Run the included setup script which provisions a venv, installs deps and starts the service:

```bash
cd /path/to/MusicConvert
./setup_service.sh
```

Manual dev setup (without systemd)

```bash

MusicConvert — Homelab web service

This repository runs a lightweight FastAPI web UI that accepts YouTube/video/playlist links, downloads audio (M4A/AAC) using `yt-dlp` and `ffmpeg`, and packages the results into a ZIP you can download.

Quick setup (Ubuntu)

1) Copy or clone the repo onto your Ubuntu VM, or run the included setup script which prompts for a GitHub URL and a clone directory:

```bash
cd /path/where/you/want/to/run
./setup_service.sh
```

The script will:
- prompt for a GitHub repo URL and a target directory to clone into (if you choose to clone),
- create a Python virtual environment, install Python requirements,
- ensure `ffmpeg` is installed via apt,
- detect the server LAN IP and create a `systemd` unit that sets `WEB_HOST` to that IP and starts the web service.

Manual dev setup (without systemd)

```bash
cd /path/to/MusicConvert
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo apt-get install -y ffmpeg
python web.py
```

Running and usage

1) Open the UI in your browser on the machine running the server (or another machine on the LAN):

   http://<server-ip>:8000

2) Paste one or more YouTube/video/playlist links (comma or newline separated) into the textarea and click "Start".

3) Live progress: the UI displays lightweight live progress messages per download — you will see messages like:

- "[1/3] Starting: <url>"
- "downloading:12.3%:..." (live percentage, bytes and eta)
- "finished:<filename>"
- "Creating ZIP archive..."
- A "Download" link will appear when the ZIP is ready.

4) Click the download link to save `music_<job_id>.zip` to your PC. The ZIP contains the downloaded folders/files from that job.

Notes

- Ensure `ffmpeg` is installed and available in the PATH used by the service.
- Job outputs are kept under `web_output/<job_id>/` on the server. If you want automatic cleanup, I can add a policy to remove jobs older than N days or delete after ZIP download.


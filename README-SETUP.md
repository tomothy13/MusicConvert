# MusicConvert — Setup, Teardown & Troubleshooting

This file explains how to install the MusicConvert service on a Linux homelab host and how to remove it safely. It includes clickable raw download links for the setup and teardown scripts so you can curl them directly.

Important: replace `<GITHUB_USER>` and `<REPO>` with your GitHub username and repository name if you are hosting this project on GitHub. Example raw URL pattern:

- `https://raw.githubusercontent.com/<GITHUB_USER>/<REPO>/main/setup_service.sh`
- `https://raw.githubusercontent.com/<GITHUB_USER>/<REPO>/main/teardown_service.sh`

Quick install (one-liner)

```bash
# Download and run the setup script (replace placeholders before running)
curl -fsSL https://raw.githubusercontent.com/<GITHUB_USER>/<REPO>/main/setup_service.sh -o setup_service.sh
bash setup_service.sh
```

Quick uninstall (one-liner)

```bash
curl -fsSL https://raw.githubusercontent.com/<GITHUB_USER>/<REPO>/main/teardown_service.sh -o teardown_service.sh
bash teardown_service.sh
```

Where the scripts log

- Preferred system logs for the scripts: `/var/log/musicconvert-setup.log` and `/var/log/musicconvert-teardown.log` (created when writable).
- Fallback logs (if `/var/log` is not writable): `setup.log` or `teardown.log` next to the downloaded script.
- The running service (systemd) logs to the system journal and the process also writes to `error.log` in the repository root.

Inspecting the running service

```bash
# Show live systemd logs for the service
sudo journalctl -u musicconvert -f

# Tail the rotating file-based server log in the project dir
tail -f /path/to/MusicConvert/error.log
```

Troubleshooting hints

- ffmpeg missing: `setup_service.sh` attempts to install `ffmpeg` via `apt`. If your distro does not use `apt`, install `ffmpeg` manually before running setup.
- Permission errors writing to `/etc/systemd/system/` or `/var/log/`: run the setup script with a user that has `sudo` privileges when prompted.
- If the service fails to start, check `sudo systemctl status musicconvert` and the journal for the detailed traceback.
- If the web UI is not reachable from other LAN machines, verify the detected LAN IP printed by the setup script or edit the `WEB_HOST` in the systemd unit to `0.0.0.0` to bind on all interfaces.

Customizing the download link

If you host this repo on GitHub, replace the `<GITHUB_USER>` and `<REPO>` placeholders above with your values. The raw URLs will then be direct, clickable links that download the scripts.

Example (replace `your-user` and `MusicConvert`):

`https://raw.githubusercontent.com/your-user/MusicConvert/main/setup_service.sh`

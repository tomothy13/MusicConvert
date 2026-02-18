#!/usr/bin/env bash
set -euo pipefail

# Determine script directory early so fallback logs can live next to the script
SCRIPT_DIR="$(cd "$(dirname "${0}")" && pwd)"

# Log file for this setup script (prefer /var/log but fall back to project dir)
LOG_FILE="/var/log/musicconvert-setup.log"
if ! touch "$LOG_FILE" >/dev/null 2>&1; then
  LOG_FILE="$SCRIPT_DIR/setup.log"
fi

# Redirect all stdout/stderr to the log (and still show it on stdout)
exec > >(tee -a "$LOG_FILE") 2>&1

# setup_service.sh
# Sets up a Python venv, installs requirements, ensures ffmpeg,
# detects the server LAN IP and installs/enables a systemd service
# that runs the FastAPI web UI bound to the LAN IP.

PYTHON=python3

# Prompt for GitHub repo and target clone directory. If the script is already
# inside a cloned repository (contains web.py), offer to use it or clone fresh.
read -p "Clone repository from GitHub? (y/N): " CLONE_ANSWER
CLONE_ANSWER=${CLONE_ANSWER:-N}
if [[ "$CLONE_ANSWER" =~ ^[Yy]$ ]]; then
  read -p "GitHub repo URL (e.g. https://github.com/user/MusicConvert.git): " GIT_URL
  read -p "Target directory to clone into (absolute path): " TARGET_DIR
  if [ -z "$GIT_URL" ] || [ -z "$TARGET_DIR" ]; then
    echo "Git URL and target directory are required to clone. Aborting."
    exit 1
  fi
  echo "Cloning $GIT_URL into $TARGET_DIR"
  mkdir -p "$TARGET_DIR"
  # Ensure basic system packages are present on a fresh Ubuntu install
  if command -v apt-get >/dev/null 2>&1; then
    echo "Ensuring required system packages are installed: git, python3, python3-venv, python3-pip, ffmpeg"
    sudo apt-get update
    sudo apt-get install -y git python3 python3-venv python3-pip ffmpeg
  else
    echo "apt-get not found. Please install git, python3, python3-venv, python3-pip and ffmpeg manually."
    exit 1
  fi
  git clone "$GIT_URL" "$TARGET_DIR"
  SCRIPT_DIR="$TARGET_DIR"
else
  # Use current directory
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
fi

VENV_DIR="$SCRIPT_DIR/venv"

echo "Project dir: $SCRIPT_DIR"

# Ensure system packages exist (for non-clone path); attempt to install if apt available
if ! command -v $PYTHON >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    echo "Installing Python3 and supporting packages via apt..."
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv python3-pip git ffmpeg
  else
    echo "python3 not found and apt-get unavailable. Please install Python3 and dependencies manually."
    exit 1
  fi
fi

echo "Creating virtualenv in $VENV_DIR (if missing)..."
if [ ! -d "$VENV_DIR" ]; then
  $PYTHON -m venv "$VENV_DIR"
fi

echo "Activating venv and installing requirements..."
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r "$SCRIPT_DIR/requirements.txt"

echo "Checking ffmpeg..."
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. Installing via apt (requires sudo)..."
  sudo apt-get update
  sudo apt-get install -y ffmpeg
fi

# Detect LAN IP: prefer route-based detection, fallback to hostname -I
LAN_IP=""
LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}') || true
if [ -z "$LAN_IP" ]; then
  LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}') || true
fi
if [ -z "$LAN_IP" ]; then
  echo "Could not detect LAN IP automatically. Using 0.0.0.0 (bind all)."
  LAN_IP="0.0.0.0"
fi

echo "Detected LAN IP: $LAN_IP"

# Create a systemd service file
SERVICE_NAME="musicconvert"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "Writing systemd unit to $SERVICE_FILE (requires sudo)"
sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=MusicConvert web service
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${SCRIPT_DIR}
Environment=PATH=${VENV_DIR}/bin
Environment=WEB_HOST=${LAN_IP}
Environment=WEB_PORT=8000
ExecStart=${VENV_DIR}/bin/python ${SCRIPT_DIR}/web.py
Restart=on-failure
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

echo "Reloading systemd, enabling and starting $SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

echo "Service started. Check status with: sudo systemctl status ${SERVICE_NAME}"
echo "Logs: sudo journalctl -u ${SERVICE_NAME} -f"
echo "Open: http://${LAN_IP}:8000 from another machine on your LAN (or http://localhost:8000 locally)."

deactivate || true

echo "Setup complete."

#!/usr/bin/env bash
set -euo pipefail

# Log file for this teardown script (prefer /var/log but fall back to project dir)
SCRIPT_DIR="$(cd "$(dirname "${0}")" && pwd)"
LOG_FILE="/var/log/musicconvert-teardown.log"
if ! touch "$LOG_FILE" >/dev/null 2>&1; then
  LOG_FILE="$SCRIPT_DIR/teardown.log"
fi

# Redirect all stdout/stderr to the log (and still show it on stdout)
exec > >(tee -a "$LOG_FILE") 2>&1

# teardown_service.sh
# Safely stops and removes the MusicConvert systemd service, venv and job outputs.
# Prompts before deleting anything. Does NOT remove system packages installed via apt.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="musicconvert"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "This will stop and remove the '${SERVICE_NAME}' systemd service, and optionally"
echo "delete the virtualenv and job output directories under the project directory." 
echo

read -p "Project directory (absolute path) [default: ${SCRIPT_DIR}]: " INPUT_DIR
INPUT_DIR=${INPUT_DIR:-${SCRIPT_DIR}}

if [ ! -d "$INPUT_DIR" ]; then
  echo "Directory not found: $INPUT_DIR"
  exit 1
fi

cd "$INPUT_DIR"

# Stop and disable systemd service if present
if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
  echo "Stopping ${SERVICE_NAME}..."
  sudo systemctl stop "${SERVICE_NAME}" || true
  echo "Disabling ${SERVICE_NAME}..."
  sudo systemctl disable "${SERVICE_NAME}" || true
else
  echo "Systemd unit ${SERVICE_NAME}.service not found; skipping stop/disable."
fi

# Remove systemd unit file if exists
if [ -f "$SERVICE_FILE" ]; then
  read -p "Remove systemd unit file $SERVICE_FILE ? (y/N): " REMOVE_UNIT
  REMOVE_UNIT=${REMOVE_UNIT:-N}
  if [[ "$REMOVE_UNIT" =~ ^[Yy]$ ]]; then
    echo "Removing $SERVICE_FILE (requires sudo)"
    sudo rm -f "$SERVICE_FILE"
    sudo systemctl daemon-reload
    echo "Unit removed and systemd reloaded."
  else
    echo "Keeping unit file."
  fi
else
  echo "No unit file at $SERVICE_FILE"
fi

# Remove virtualenv
VENV_DIR="$INPUT_DIR/venv"
if [ -d "$VENV_DIR" ]; then
  read -p "Remove virtualenv at $VENV_DIR ? (y/N): " RMVENV
  RMVENV=${RMVENV:-N}
  if [[ "$RMVENV" =~ ^[Yy]$ ]]; then
    echo "Removing virtualenv..."
    rm -rf "$VENV_DIR"
    echo "Virtualenv removed."
  else
    echo "Keeping virtualenv."
  fi
else
  echo "No virtualenv found at $VENV_DIR"
fi

# Remove web_output job folders and zips
WEB_OUT="$INPUT_DIR/web_output"
if [ -d "$WEB_OUT" ]; then
  read -p "Remove job outputs in $WEB_OUT ? (y/N): " RMWEB
  RMWEB=${RMWEB:-N}
  if [[ "$RMWEB" =~ ^[Yy]$ ]]; then
    echo "Removing $WEB_OUT ..."
    rm -rf "$WEB_OUT"
    echo "Job outputs removed."
  else
    echo "Keeping job outputs."
  fi
else
  echo "No web_output directory found."
fi

# Optionally remove the whole cloned repo (dangerous)
read -p "Remove the entire project directory $INPUT_DIR ? This deletes EVERYTHING inside. (y/N): " RMALL
RMALL=${RMALL:-N}
if [[ "$RMALL" =~ ^[Yy]$ ]]; then
  echo "Removing project directory..."
  cd /tmp
  rm -rf "$INPUT_DIR"
  echo "Project directory removed."
else
  echo "Project directory preserved."
fi

echo "Teardown complete."

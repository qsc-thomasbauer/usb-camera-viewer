#!/usr/bin/env bash
# Install dependencies (system packages + Python deps via uv) and set up the systemd service.
# Run as your normal user from anywhere: ./install.sh   (uses sudo where needed)
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=usb-viewer
UNIT="/etc/systemd/system/${SERVICE}.service"
PORT="${PORT:-8080}"

if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user (not with sudo); it calls sudo itself." >&2
    exit 1
fi

echo "==> Installing system packages (ffmpeg, v4l-utils)"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends ffmpeg v4l-utils python3 curl

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
    echo "==> Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

echo "==> Installing Python dependencies with uv"
cd "$APP_DIR"
# Use the system Python instead of downloading one; it is plenty on a Pi.
uv sync --python /usr/bin/python3 --no-python-downloads
PYTHON="$APP_DIR/.venv/bin/python"
"$PYTHON" -c "import flask, pyudev" # fail early if the venv is broken

echo "==> Writing $UNIT"
sudo tee "$UNIT" >/dev/null <<UNIT_EOF
[Unit]
Description=USB webcam viewer
After=network-online.target
Wants=network-online.target

[Service]
# Runs as root so stuck cameras can be reset at the USB level.
ExecStart=$PYTHON $APP_DIR/app.py
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PORT=$PORT
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT_EOF

echo "==> Enabling and (re)starting $SERVICE"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
sudo systemctl restart "$SERVICE"

sleep 2
if systemctl is-active --quiet "$SERVICE"; then
    IP="$(hostname -I | awk '{print $1}')"
    echo "==> Done: http://${IP:-localhost}:$PORT  (logs: journalctl -u $SERVICE -f)"
else
    echo "!! $SERVICE failed to start:" >&2
    sudo journalctl -u "$SERVICE" -n 30 --no-pager >&2
    exit 1
fi

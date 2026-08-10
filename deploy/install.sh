#!/bin/bash
# One-shot installer for Raspberry Pi OS (Bookworm) on a Pi 5.
#
#   cd ~/cycletime && bash deploy/install.sh
#
# Creates the venv, installs deps, registers the systemd service and sets the
# dashboard to open fullscreen at login. Safe to re-run.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$USER}"
VENV="$PROJECT_DIR/.venv"

echo "==> Installing cycle-time tracker from $PROJECT_DIR (user: $SERVICE_USER)"

# ---------------------------------------------------------------- system deps
echo "==> System packages"
sudo apt-get update -qq
# python3-venv for the venv; v4l-utils to inspect what the webcam offers;
# libgl1/libglib2.0-0 are OpenCV's runtime shared libs even in the headless
# build; chromium for the kiosk display.
sudo apt-get install -y -qq \
  python3-venv python3-dev v4l-utils curl \
  libgl1 libglib2.0-0 \
  chromium-browser || sudo apt-get install -y -qq chromium

# ------------------------------------------------------------------- camera
# Reading /dev/video* requires membership of the video group.
if ! id -nG "$SERVICE_USER" | grep -qw video; then
  echo "==> Adding $SERVICE_USER to the video group (log out and back in to take effect)"
  sudo usermod -aG video "$SERVICE_USER"
fi

echo "==> Cameras detected:"
v4l2-ctl --list-devices 2>/dev/null || echo "    (none found — check the USB cable)"

# --------------------------------------------------------------- python env
echo "==> Python environment"
python3 -m venv "$VENV" 2>/dev/null || true
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

mkdir -p "$PROJECT_DIR/data"

# ------------------------------------------------------------------ service
echo "==> systemd service"
sudo tee /etc/systemd/system/cycletime.service >/dev/null <<UNIT
[Unit]
Description=Conveyor cycle-time tracker
After=multi-user.target dev-video0.device
Wants=dev-video0.device

[Service]
Type=simple
User=$SERVICE_USER
Group=video
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV/bin/python $PROJECT_DIR/run.py
Restart=always
RestartSec=5
StartLimitIntervalSec=0
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable cycletime
sudo systemctl restart cycletime

# -------------------------------------------------------------------- kiosk
echo "==> Kiosk autostart"
chmod +x "$PROJECT_DIR/deploy/kiosk.sh"

# The desktop session, not systemd, starts the browser — it needs a display.
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/cycletime-kiosk.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Cycle Time Dashboard
Exec=$PROJECT_DIR/deploy/kiosk.sh
X-GNOME-Autostart-enabled=true
DESKTOP

# ------------------------------------------------------------------- report
echo
echo "==> Done."
sudo systemctl --no-pager --lines=0 status cycletime || true
echo
echo "  Dashboard : http://localhost:8000/"
echo "  Aim ROI   : http://localhost:8000/setup"
echo "  Logs      : journalctl -fu cycletime"
echo "  Camera fmt: v4l2-ctl --list-formats-ext"
echo
echo "  Next: open /setup, drag a box over the belt, and save."
echo "  Reboot to confirm the service and kiosk both come back on their own."

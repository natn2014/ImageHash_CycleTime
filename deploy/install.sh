#!/bin/bash
# One-shot installer for Raspberry Pi OS (Bookworm) on a Pi 5.
#
#   git clone https://github.com/natn2014/ImageHash_CycleTime.git ~/cycletime
#   cd ~/cycletime && bash deploy/install.sh
#
# Safe to re-run — this is also the update path:
#   cd ~/cycletime && git pull && bash deploy/install.sh
#
# Sets the timezone, installs dependencies, creates the venv, registers the
# systemd service, and configures both screens to come up at login.
#
# Override the timezone if you are not in Thailand:
#   CYCLETIME_TZ=Asia/Singapore bash deploy/install.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$USER}"
# Autostart must land in the *desktop user's* home, not root's. Running this
# script with sudo would otherwise write to /root/.config/autostart, where no
# desktop session ever looks, and the screens would stay blank after reboot.
USER_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
VENV="$PROJECT_DIR/.venv"
WANT_TZ="${CYCLETIME_TZ:-Asia/Bangkok}"

echo "==> Installing cycle-time tracker"
echo "    source : $PROJECT_DIR"
echo "    user   : $SERVICE_USER ($USER_HOME)"

# ------------------------------------------------------------------ timezone
# The shift roster works entirely in LOCAL time: which team gets credit for a
# cycle, and which production day it lands on, both come from the wall clock.
# A Pi left on UTC would file every cycle against the wrong shift — 7 hours out
# in Thailand — and the mistake is invisible until someone checks the numbers.
CURRENT_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"
echo "==> Timezone (currently: $CURRENT_TZ)"
if [ "$CURRENT_TZ" != "$WANT_TZ" ]; then
  echo "    setting to $WANT_TZ  (override with CYCLETIME_TZ=...)"
  sudo timedatectl set-timezone "$WANT_TZ"
else
  echo "    already correct"
fi
# Without a real-time clock the Pi boots at epoch until NTP syncs, so early
# cycles would be attributed to 1970. Enable NTP and warn if it has not synced.
sudo timedatectl set-ntp true 2>/dev/null || true
if ! timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
  echo "    !! clock not yet synced to NTP — a Pi with no network and no RTC will"
  echo "       mis-date cycles until it syncs. Check 'timedatectl' after boot."
fi

# ---------------------------------------------------------------- system deps
echo "==> System packages"
sudo apt-get update -qq
# git                present on the Desktop image, absent on Lite. Installed
#                    here so the update path this script prints at the end
#                    ("git pull") is guaranteed to work.
# python3-venv       the virtualenv
# v4l-utils          inspect what the webcam actually offers
# libgl1/libglib2.0-0 OpenCV runtime shared libs, needed even by the headless build
# wlr-randr          REQUIRED for dual display: kiosk.sh uses it to place the
#                    7" DSI and the 40" HDMI side by side. Without it both
#                    browser windows pile onto one screen.
# x11-xserver-utils  provides xset, used to stop blanking on an X session
sudo apt-get install -y -qq \
  git python3-venv python3-dev v4l-utils curl \
  libgl1 libglib2.0-0 \
  wlr-randr x11-xserver-utils
sudo apt-get install -y -qq chromium-browser 2>/dev/null \
  || sudo apt-get install -y -qq chromium

# ------------------------------------------------------------------- camera
# Reading /dev/video* requires membership of the video group.
if ! id -nG "$SERVICE_USER" | grep -qw video; then
  echo "==> Adding $SERVICE_USER to the video group (log out and back in to take effect)"
  sudo usermod -aG video "$SERVICE_USER"
fi

echo "==> Cameras detected:"
v4l2-ctl --list-devices 2>/dev/null || echo "    (none found — check the USB cable)"

# ------------------------------------------------------------------ displays
# Reported, not configured: placement happens in kiosk.sh at session start,
# because there is no Wayland session running while this script executes.
echo "==> Displays detected:"
if command -v wlr-randr >/dev/null && wlr-randr >/dev/null 2>&1; then
  wlr-randr | grep -E '^[A-Za-z]' | sed 's/^/    /'
else
  echo "    (cannot query now — no Wayland session. Will be detected at login.)"
  echo "    Expect one DSI-* (7\" panel) and one HDMI-* (40\" board)."
fi

# --------------------------------------------------------------- python env
echo "==> Python environment"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
# Not quiet: on a Pi the OpenCV wheel is large and this step can run for
# several minutes. Silence here looks like a hang.
echo "    installing dependencies (OpenCV is large — this can take a few minutes)"
"$VENV/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

mkdir -p "$PROJECT_DIR/data"

# ------------------------------------------------------------------- config
# config.json is deliberately untracked: the Setup page and the Shifts tab
# write to it, so tracking it would make every `git pull` collide with the
# operator's own ROI and roster. Seed it once from the template, then never
# touch it again — re-running this installer must not wipe a tuned line.
if [ ! -f "$PROJECT_DIR/config.json" ]; then
  cp "$PROJECT_DIR/config.example.json" "$PROJECT_DIR/config.json"
  echo "==> Created config.json from config.example.json"
else
  echo "==> Keeping existing config.json (your ROI and shift settings are safe)"
fi

# Verify the stack actually imports before declaring success — a wheel that
# installed but cannot load (missing libGL, wrong arch) fails at 3am otherwise.
echo "==> Verifying imports"
"$VENV/bin/python" - <<'PYCHECK'
import cv2, numpy, fastapi, uvicorn
print(f"    cv2 {cv2.__version__} · numpy {numpy.__version__} · fastapi {fastapi.__version__}")
PYCHECK

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
echo "==> Kiosk autostart (7\" dashboard + 40\" scoreboard)"
chmod +x "$PROJECT_DIR/deploy/kiosk.sh"

# Stop the screens blanking mid-shift. Under Wayland xset does nothing, so use
# raspi-config, which sets it the way the OS actually honours.
sudo raspi-config nonint do_blanking 1 2>/dev/null \
  || echo "    (raspi-config not available — kiosk.sh still tries xset at login)"

# The desktop session starts the browsers, not systemd: they need a display,
# which systemd's multi-user target knows nothing about.
#
# Exec runs kiosk.sh through /bin/bash rather than invoking it directly: a
# .desktop launcher execs the Exec= target as-is, with no shell in between, so
# it needs the +x bit set on kiosk.sh itself. `git pull` resets file mode to
# whatever's tracked (644 - git doesn't track the exec bit this repo relies
# on), silently dropping it again on every update. Routing through bash means
# a lost +x bit is cosmetic, not a boot-time failure with no error anywhere.
AUTOSTART_DIR="$USER_HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/cycletime-kiosk.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Cycle Time Screens
Exec=/bin/bash $PROJECT_DIR/deploy/kiosk.sh
X-GNOME-Autostart-enabled=true
DESKTOP
# If this ran under sudo the file is owned by root and the session may skip it.
sudo chown -R "$SERVICE_USER": "$AUTOSTART_DIR" 2>/dev/null || true

# ------------------------------------------------------------------- report
echo
echo "==> Done."
sudo systemctl --no-pager --lines=0 status cycletime || true
echo
echo "  7\"  dashboard : http://localhost:8000/"
echo "  40\" scoreboard: http://localhost:8000/board"
echo "  Aim the camera: http://localhost:8000/setup"
echo "  Shift calendar: http://localhost:8000/shifts"
echo
echo "  Logs      : journalctl -fu cycletime"
echo "  Camera fmt: v4l2-ctl --list-formats-ext   (confirm MJPG is offered)"
echo "  Time check: timedatectl                   (must show $WANT_TZ)"
echo
echo "  Next steps:"
echo "    1. Open /setup and drag a box over the belt, then Save."
echo "    2. Open /shifts -> Pattern and match 'Next 14 days' to the real roster."
echo "    3. Reboot and confirm BOTH screens come back on their own."
echo
echo "  To update later:  cd $PROJECT_DIR && git pull && bash deploy/install.sh"

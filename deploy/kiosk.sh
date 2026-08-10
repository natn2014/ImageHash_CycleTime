#!/bin/bash
# Launch both screens: the 7" DSI control panel and the 40" HDMI scoreboard.
#
# Started by the desktop autostart entry install.sh writes, not by systemd: it
# needs a running Wayland session, which systemd's multi-user target has no
# concept of.
#
# THE GOTCHA: each Chromium instance needs its own --user-data-dir. A second
# Chromium sharing a profile does not open a second window — it hands the URL
# to the already-running instance and exits, leaving the 40" blank. This is the
# single most likely thing to go wrong here.

set -u

BASE="http://localhost:8000"
DASH_URL="$BASE/"
BOARD_URL="$BASE/board"

DSI_W=800
DSI_H=480
HDMI_W=1920
HDMI_H=1080

# The tracker owns the camera and the database; give it a moment to bind the
# port so the first paint isn't a connection-refused page.
for _ in $(seq 1 60); do
  if curl -sf -o /dev/null "$BASE/api/health"; then break; fi
  sleep 1
done

# Clear the crash flags, or Chromium shows a "didn't shut down correctly"
# infobar over the display after every power cut — which on a factory line is
# most restarts.
for profile in "$HOME/.config/cycletime-dsi" "$HOME/.config/cycletime-hdmi"; do
  prefs="$profile/Default/Preferences"
  if [ -f "$prefs" ]; then
    sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/' "$prefs" 2>/dev/null || true
    sed -i 's/"exited_cleanly":false/"exited_cleanly":true/' "$prefs" 2>/dev/null || true
  fi
done

# Blank screen and screensaver off — both displays must stay lit all shift.
xset s off -dpms 2>/dev/null || true
wlr-randr 2>/dev/null | grep -q . && HAVE_RANDR=1 || HAVE_RANDR=0

# --------------------------------------------------------------- outputs
# Names are detected rather than hard-coded: DSI enumerates variously as
# DSI-1 or DSI-2 depending on kernel and overlay.
DSI_OUT=""
HDMI_OUT=""
if [ "$HAVE_RANDR" = "1" ]; then
  OUTPUTS=$(wlr-randr 2>/dev/null | grep -E '^[A-Za-z]' | awk '{print $1}')
  for out in $OUTPUTS; do
    case "$out" in
      DSI*|dsi*) [ -z "$DSI_OUT" ] && DSI_OUT="$out" ;;
      HDMI*|hdmi*) [ -z "$HDMI_OUT" ] && HDMI_OUT="$out" ;;
    esac
  done
  echo "detected outputs: DSI='${DSI_OUT:-none}' HDMI='${HDMI_OUT:-none}'"

  # Lay the two outputs side by side so a window's x-position selects which
  # screen it lands on: DSI occupies 0..799, HDMI starts at 800.
  if [ -n "$DSI_OUT" ]; then
    wlr-randr --output "$DSI_OUT" --on --pos 0,0 2>/dev/null || true
  fi
  if [ -n "$HDMI_OUT" ]; then
    wlr-randr --output "$HDMI_OUT" --on --pos "${DSI_W},0" 2>/dev/null || true
  fi
fi

BROWSER=$(command -v chromium-browser || command -v chromium)
if [ -z "$BROWSER" ]; then
  echo "chromium not found: sudo apt install -y chromium-browser" >&2
  exit 1
fi

launch() {
  local url="$1" profile="$2" x="$3" y="$4" w="$5" h="$6"
  # --password-store=basic keeps Chromium off gnome-keyring/libsecret. The Pi
  # autologins, so PAM never unlocks the login keyring, and Chromium asking the
  # secret service for its encryption key pops an "unlock your login keyring"
  # dialog over the kiosk that nobody on the line can dismiss. Basic keeps the
  # key in the profile instead — no prompt, and nothing here stores passwords.
  "$BROWSER" \
    --kiosk \
    --app="$url" \
    --user-data-dir="$HOME/.config/$profile" \
    --window-position="$x,$y" \
    --window-size="$w,$h" \
    --password-store=basic \
    --no-first-run \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI \
    --check-for-update-interval=31536000 \
    --overscroll-history-navigation=0 \
    --disable-pinch \
    &
}

# 40" scoreboard first: it is the screen people actually look at, so it should
# be up even if the 7" panel has a problem.
if [ -n "$HDMI_OUT" ] || [ "$HAVE_RANDR" = "0" ]; then
  launch "$BOARD_URL" cycletime-hdmi "$DSI_W" 0 "$HDMI_W" "$HDMI_H"
  sleep 3
fi

launch "$DASH_URL" cycletime-dsi 0 0 "$DSI_W" "$DSI_H"

# Keep this script alive so the desktop session treats it as the running app;
# killing it takes both browsers down together.
wait

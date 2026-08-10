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

# Fallbacks only. The real sizes are read off the compositor below when
# wlr-randr is available — assuming 800 wide is what makes the two windows
# overlap on a panel that is not the original 7" 800x480.
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
# Report an output's LOGICAL size — the coordinate space windows are placed in,
# which is the current mode divided by the scale, with the axes swapped on a
# rotated panel. The physical mode is the wrong number to use: a 1280x720 panel
# at scale 2 occupies 640x360 of layout, and a 7" Touch Display 2 is a portrait
# 720x1280 that reports 1280x720 once rotated. Echoes "W H", nothing on failure.
out_logical_size() {
  wlr-randr 2>/dev/null | awk -v want="$1" '
    /^[A-Za-z]/  { inblk = ($1 == want); next }
    !inblk       { next }
    /current/    { split($1, m, "x"); w = m[1]; h = m[2] }
    /Scale:/     { s = $2 }
    /Transform:/ { t = $2 }
    END {
      if (w == "") exit 1
      if (s == "" || s + 0 <= 0) s = 1
      # Quarter-turns swap the axes. Matched loosely because the spelling
      # varies by version and by what wrote the config: "90", "270",
      # "flipped-90", or the words the Pi Screen Configuration tool uses.
      if (t ~ /(^|-)(90|270)$/ || t == "left" || t == "right") {
        tmp = w; w = h; h = tmp
      }
      printf "%d %d\n", int(w / s), int(h / s)
    }'
}

# Report where an output currently sits in the layout. Echoes "X Y".
out_position() {
  wlr-randr 2>/dev/null | awk -v want="$1" '
    /^[A-Za-z]/ { inblk = ($1 == want); next }
    !inblk      { next }
    /Position:/ { split($2, p, ","); print p[1], p[2]; exit }'
}

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

  # Take the measured sizes over the defaults at the top of this file. The DSI
  # width in particular is load-bearing: it is the seam between the two screens,
  # and guessing it too small overlaps the board onto the panel.
  if [ -n "$DSI_OUT" ] && read -r _w _h < <(out_logical_size "$DSI_OUT"); then
    DSI_W="$_w"; DSI_H="$_h"
  fi
  if [ -n "$HDMI_OUT" ] && read -r _w _h < <(out_logical_size "$HDMI_OUT"); then
    HDMI_W="$_w"; HDMI_H="$_h"
  fi
  echo "logical sizes: DSI=${DSI_W}x${DSI_H} HDMI=${HDMI_W}x${HDMI_H} (seam at x=${DSI_W})"

  # Lay the two outputs side by side so a window's x-position selects which
  # screen it lands on: the DSI occupies 0..DSI_W-1, the HDMI starts at DSI_W.
  #
  # Errors are printed, not swallowed. A refused --pos is the difference between
  # two clean screens and two screens sharing coordinates, and silence here sent
  # us looking at the browsers when the fault was one layer down.
  if [ -n "$DSI_OUT" ]; then
    wlr-randr --output "$DSI_OUT" --on --pos 0,0 \
      || echo "!! could not position $DSI_OUT" >&2
  fi
  if [ -n "$HDMI_OUT" ]; then
    wlr-randr --output "$HDMI_OUT" --on --pos "${DSI_W},0" \
      || echo "!! could not position $HDMI_OUT" >&2
  fi

  # Read the layout back. The compositor is free to ignore the request above —
  # and on Raspberry Pi OS a saved screen configuration (Control Centre ->
  # Screens, restored by kanshi or wayfire.ini) can re-apply itself and put the
  # outputs back on top of each other. Overlapping outputs cannot be fixed by
  # any window placement, so say so plainly rather than letting it look like a
  # browser problem.
  if read -r dx dy < <(out_position "$DSI_OUT") \
     && read -r hx hy < <(out_position "$HDMI_OUT"); then
    echo "layout now: $DSI_OUT at ${dx},${dy}  $HDMI_OUT at ${hx},${hy}"
    if [ "$hx" -lt "$((dx + DSI_W))" ] && [ "$((hx + HDMI_W))" -gt "$dx" ]; then
      echo "!! OUTPUTS OVERLAP: $HDMI_OUT starts at x=$hx but $DSI_OUT runs to" >&2
      echo "   x=$((dx + DSI_W - 1)). The two screens share coordinates, so the" >&2
      echo "   windows cannot be separated until this is fixed." >&2
      echo "   Fix it in Control Centre -> Screens: drag the HDMI clear of the" >&2
      echo "   DSI and press Apply. That layout is saved and survives reboot." >&2
    fi
  fi
fi

BROWSER=$(command -v chromium-browser || command -v chromium)
if [ -z "$BROWSER" ]; then
  echo "chromium not found: sudo apt install -y chromium-browser" >&2
  exit 1
fi

# Escape hatch for the placement problem below. Empty (the default) lets
# Chromium pick its own backend. Setting CYCLETIME_OZONE=x11 runs both windows
# through XWayland, where --window-position is obeyed literally and the two
# screens separate without any compositor rule:
#
#   CYCLETIME_OZONE=x11 ~/cycletime/deploy/kiosk.sh
#
# Try it before writing window rules; if Chromium refuses to start with it,
# unset it again and use the rules.
OZONE="${CYCLETIME_OZONE:-}"

launch() {
  local url="$1" profile="$2" x="$3" y="$4" w="$5" h="$6" class="$7"
  # --class sets the window's app_id. The two windows MUST carry different ones:
  # under Wayland --window-position is only a hint, so placement often has to be
  # enforced by a compositor rule, and a rule matching plain "chromium-browser"
  # would catch both windows and drag the 7" dashboard onto the 40" with the
  # board. See "The two screens" in the README for the rules themselves.
  #
  # --password-store=basic keeps Chromium off gnome-keyring/libsecret. The Pi
  # autologins, so PAM never unlocks the login keyring, and Chromium asking the
  # secret service for its encryption key pops an "unlock your login keyring"
  # dialog over the kiosk that nobody on the line can dismiss. Basic keeps the
  # key in the profile instead — no prompt, and nothing here stores passwords.
  "$BROWSER" \
    --kiosk \
    --app="$url" \
    --class="$class" \
    --user-data-dir="$HOME/.config/$profile" \
    --window-position="$x,$y" \
    --window-size="$w,$h" \
    ${OZONE:+--ozone-platform="$OZONE"} \
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
  launch "$BOARD_URL" cycletime-hdmi "$DSI_W" 0 "$HDMI_W" "$HDMI_H" cycletime-board
  sleep 3
fi

launch "$DASH_URL" cycletime-dsi 0 0 "$DSI_W" "$DSI_H" cycletime-dash

# Keep this script alive so the desktop session treats it as the running app;
# killing it takes both browsers down together.
wait

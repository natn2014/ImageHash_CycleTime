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

# Chromium derives a toplevel app_id from the --app= URL, and these are the
# values it produces for the two URLs above — used by wait_for_window() to tell
# the windows apart. Confirmed with `wlrctl toplevel list` on Chromium 149:
#
#   chrome-localhost__board-Default: Cycle Time Board
#   chrome-localhost__-Default: Cycle Time
#
# Re-check with that command if a Chromium update ever changes the scheme.
DASH_APP_ID="chrome-localhost__-Default"
BOARD_APP_ID="chrome-localhost__board-Default"

# Fallbacks only. The real sizes are read off the compositor below when
# wlr-randr is available — assuming 800 wide is what makes the two windows
# overlap on a panel that is not the original 7" 800x480.
#
# If the measurement gets it wrong, override it from the environment rather
# than editing these lines: this file is tracked, and a local edit here will
# collide on the next `git pull`.
#
#   CYCLETIME_DSI_W=1280 CYCLETIME_DSI_H=720 deploy/kiosk.sh
DSI_W=1280
DSI_H=720
HDMI_W=1920
HDMI_H=1080

# ------------------------------------------------------------ wait for ready
# THE GOTCHA THAT PUT "localhost refused to connect" ON THE WALL: a Chromium
# error page is a DEAD END. There is no JS on net::ERR_CONNECTION_REFUSED to
# retry the load, and --kiosk hides the reload button, so one early launch
# leaves both screens showing an error until somebody finds a keyboard. Opening
# late costs nothing; opening early costs the whole shift. So every gate below
# errs towards waiting.
#
# Why the old 60s budget was not enough, measured on this Pi:
#
#   08:39:04  power on
#   08:40:23  kiosk.sh ran out of budget and launched Chromium
#   08:40:37  systemd started cycletime.service
#   08:40:39  uvicorn bound :8000          <-- 95s after boot, 16s TOO LATE
#
# The desktop session (and therefore this script) starts long before
# multi-user.target finishes bringing up the tracker, so the wait has to
# outlast the whole rest of boot, not just a few seconds of Python startup.

# Gate 1, the hard one: the port must answer before a browser may open.
#
# Polled against "/" rather than /api/health because health is CAMERA-gated —
# it returns 503 until the capture thread opens /dev/video0, and forever if the
# camera is unplugged. That is the wrong question here: "/" answers 200 as soon
# as uvicorn is listening, which is exactly when a page can load.
READY_TIMEOUT="${CYCLETIME_READY_TIMEOUT:-600}"
waited=0
until curl -sf -o /dev/null --max-time 2 "$BASE/"; do
  if [ "$waited" -ge "$READY_TIMEOUT" ]; then
    echo "!! $BASE not answering after ${waited}s — opening anyway, expect an" >&2
    echo "   error page. Check: systemctl status cycletime" >&2
    break
  fi
  sleep 2
  waited=$((waited + 2))
done
# Say which of the two outcomes happened, and ask the port rather than inferring
# it from the counter — the loop can also exit on a success that lands in the
# same round as the timeout. "tracker answering" printed after a timeout would
# send the next person debugging this to the browsers instead of the service,
# which is the wrong layer.
if curl -sf -o /dev/null --max-time 2 "$BASE/"; then
  echo "tracker answering after ${waited}s"
fi

# Gate 2, the soft one: wait for the camera and tracker to finish warming up so
# the first paint has real numbers on it instead of dashes. Bounded, and we
# carry on when it expires — a dead camera must still leave a readable board
# rather than two blank screens.
HEALTH_TIMEOUT="${CYCLETIME_HEALTH_TIMEOUT:-120}"
waited=0
until curl -sf -o /dev/null --max-time 2 "$BASE/api/health"; do
  if [ "$waited" -ge "$HEALTH_TIMEOUT" ]; then
    echo "!! /api/health still not ok after ${waited}s (camera not opening?) —" >&2
    echo "   opening the screens anyway. Check: journalctl -u cycletime" >&2
    break
  fi
  sleep 2
  waited=$((waited + 2))
done

# Final settle. The two gates above already prove the app is up and warm, so
# this is pure margin for the first data poll to land; lower it if you want the
# screens up sooner:  CYCLETIME_SETTLE=10 deploy/kiosk.sh
SETTLE="${CYCLETIME_SETTLE:-60}"
echo "app ready; settling ${SETTLE}s before opening the screens"
sleep "$SETTLE"

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

# wlrctl lets us warp the pointer before each launch — see warp_to() below for
# why that, not --window-position, is what actually controls which output a
# kiosk window lands on. Optional: `sudo apt install wlrctl`.
command -v wlrctl >/dev/null 2>&1 && HAVE_WLRCTL=1 || HAVE_WLRCTL=0
HAVE_POSITIONS=0

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
fi

# Applied after the measurement so an explicit override always wins, including
# when wlr-randr is unavailable and the measurement never ran.
DSI_W="${CYCLETIME_DSI_W:-$DSI_W}"
DSI_H="${CYCLETIME_DSI_H:-$DSI_H}"
HDMI_W="${CYCLETIME_HDMI_W:-$HDMI_W}"
HDMI_H="${CYCLETIME_HDMI_H:-$HDMI_H}"
echo "logical sizes: DSI=${DSI_W}x${DSI_H} HDMI=${HDMI_W}x${HDMI_H} (seam at x=${DSI_W})"

if [ "$HAVE_RANDR" = "1" ]; then

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
    HAVE_POSITIONS=1
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

# Only a fallback for browsers that ignore the pointer trick below (or when
# wlrctl isn't installed). Empty (the default) lets Chromium pick its own
# backend. Setting CYCLETIME_OZONE=x11 runs both windows through XWayland:
#
#   CYCLETIME_OZONE=x11 ~/cycletime/deploy/kiosk.sh
OZONE="${CYCLETIME_OZONE:-}"

# THE OTHER GOTCHA: --window-position/--window-size are hints a Wayland
# compositor is free to ignore, and --kiosk's fullscreen request carries no
# target output of its own. On labwc (and most wlroots compositors) a new
# fullscreen window lands on whichever output currently has the *pointer*,
# not whatever position was requested. Without moving the pointer first,
# both browsers land on the same output regardless of any flag here — this
# was confirmed on labwc 0.9.7 / Chromium 149, where neither
# --window-position nor a labwc <windowRules> MoveTo rule had any effect on
# a --kiosk window's output placement. Requires wlrctl (`sudo apt install
# wlrctl`); skipped when it's missing, in which case placement is left to
# the compositor's default (usually wrong on a dual-output kiosk).
#
# The double move is deliberate: wlrctl only moves the pointer *relatively*,
# so the first move (a large negative offset) clamps it into the top-left
# corner of the whole layout, giving the second move a known start point to
# land the pointer inside the target output from.
warp_to() {
  [ "$HAVE_WLRCTL" = "1" ] || return 0
  wlrctl pointer move -100000 -100000 >/dev/null 2>&1
  wlrctl pointer move "$1" "$2" >/dev/null 2>&1
}

# THE RACE THAT PUT BOTH PAGES ON THE 7" PANEL: the pointer decides which
# output a --kiosk window lands on, and the compositor reads it when the window
# is MAPPED — not when Chromium is exec'd. The two events are far apart. A cold
# Chromium on a Pi 5 that is still finishing boot, with the tracker warming up
# on the same CPU, can take much longer than the 3s this code used to sleep
# before its window appears. The sequence that failed was:
#
#   warp to HDMI -> exec board -> sleep 3 -> warp to DSI -> exec dashboard
#                                                  ^
#            board window still had not mapped, so when it finally did, the
#            pointer was already on the DSI and BOTH windows went there.
#
# So wait for the window to actually exist rather than guessing at a sleep.
# The app_id to match is set by Chromium from the --app= URL (NOT from --class,
# which native Wayland ignores); read the live values with `wlrctl toplevel
# list`, which prints "app_id: title" per window.
wait_for_window() {
  local app_id="$1" timeout="${2:-90}" waited=0
  # No wlrctl means no way to observe the map, and also no warp to race with —
  # a fixed sleep is all that is left, but make it a realistic one.
  if [ "$HAVE_WLRCTL" != "1" ]; then
    sleep 15
    return 0
  fi
  until wlrctl toplevel list 2>/dev/null | grep -q "^$app_id:"; do
    if [ "$waited" -ge "$timeout" ]; then
      echo "!! window '$app_id' never appeared after ${waited}s; placement of" >&2
      echo "   the next window may be wrong. Check: wlrctl toplevel list" >&2
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  # Mapped, but the fullscreen surface can still be settling onto the output;
  # warping the pointer out from under it too early re-creates the race.
  sleep 2
  echo "window '$app_id' mapped after ${waited}s"
}

launch() {
  local url="$1" profile="$2" x="$3" y="$4" w="$5" h="$6" class="$7"
  # --class sets the window's app_id under X11/XWayland (WM_CLASS). It does
  # NOT do so under native Wayland — Chromium's Ozone/Wayland backend doesn't
  # set the toplevel app-id from it — so don't rely on it for compositor
  # window rules there; match on window title instead if you need that.
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
  if [ "$HAVE_POSITIONS" = "1" ]; then
    warp_to "$((hx + HDMI_W / 2))" "$((hy + HDMI_H / 2))"
  fi
  launch "$BOARD_URL" cycletime-hdmi "$DSI_W" 0 "$HDMI_W" "$HDMI_H" cycletime-board
  # Must not warp the pointer away until this window has actually landed on the
  # HDMI — see wait_for_window(). This is what keeps both pages off the 7".
  wait_for_window "$BOARD_APP_ID" 90 || true
fi

if [ "$HAVE_POSITIONS" = "1" ]; then
  warp_to "$((dx + DSI_W / 2))" "$((dy + DSI_H / 2))"
fi
launch "$DASH_URL" cycletime-dsi 0 0 "$DSI_W" "$DSI_H" cycletime-dash
# Not to gate anything — nothing launches after this — but so the log says
# whether the 7" panel's window ever appeared.
wait_for_window "$DASH_APP_ID" 90 || true

# Keep this script alive so the desktop session treats it as the running app;
# killing it takes both browsers down together.
wait

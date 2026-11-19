#!/bin/bash
# Update a Pi that already runs the tracker (installed with deploy/install.sh).
#
#   cd ~/cycletime && bash deploy/update.sh            # update, restart service
#   cd ~/cycletime && bash deploy/update.sh --reboot   # ...then reboot, so both
#                                                      # screens load the new UI
#   cd ~/cycletime && bash deploy/update.sh --full     # also re-run install.sh
#
# What it does, in order:
#   1. Refuses to run if tracked files were edited on the Pi (a pull would
#      either fail or silently mix versions).
#   2. Backs up config.json and the cycle database to data/backups/<time>/.
#   3. git pull --ff-only (never creates a merge commit on the Pi).
#   4. Re-installs Python packages only if requirements.txt changed.
#   5. Restarts the cycletime service and waits until it answers.
#   6. If anything after the pull fails, rolls the code back to the previous
#      commit and restarts the old version, so the line display keeps working.
#
# config.json is untracked and is never overwritten: ROI, cycle target and the
# shift roster survive the update. A config from an older version simply lacks
# newer keys, which load with their defaults (e.g. shifts.pattern = "3x8").

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_DIR/.venv"
BRANCH="${CYCLETIME_BRANCH:-main}"
PORT="${CYCLETIME_PORT:-8000}"

DO_REBOOT=0
DO_FULL=0
for arg in "$@"; do
  case "$arg" in
    --reboot) DO_REBOOT=1 ;;
    --full)   DO_FULL=1 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

cd "$PROJECT_DIR"
echo "==> Updating cycle-time tracker in $PROJECT_DIR"

if [ ! -x "$VENV/bin/python" ]; then
  echo "!! No virtualenv at $VENV — this Pi was never installed." >&2
  echo "   Run the full installer instead:  bash deploy/install.sh" >&2
  exit 1
fi

# ------------------------------------------------------------ 1. clean tree
# Untracked files (config.json, data/) are fine; edits to tracked files are not.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "!! Tracked files were changed on this Pi:" >&2
  git status --short --untracked-files=no | sed 's/^/     /' >&2
  echo "   Save them elsewhere, then discard with:  git checkout -- ." >&2
  exit 1
fi

OLD_REV="$(git rev-parse HEAD)"
echo "    current : $(git log -1 --format='%h %s' "$OLD_REV")"

# ---------------------------------------------------------------- 2. backup
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$PROJECT_DIR/data/backups/$STAMP"
mkdir -p "$BACKUP_DIR"
[ -f config.json ] && cp config.json "$BACKUP_DIR/config.json"

DB_PATH="$("$VENV/bin/python" - <<'PY' 2>/dev/null || true
import sys; sys.path.insert(0, "src")
from cycletime import config
print(config.load().store.resolved_path())
PY
)"
if [ -n "$DB_PATH" ] && [ -f "$DB_PATH" ]; then
  # sqlite's online backup API: consistent even while the service is writing.
  "$VENV/bin/python" - "$DB_PATH" "$BACKUP_DIR/$(basename "$DB_PATH")" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
with dst: src.backup(dst)
dst.close(); src.close()
PY
fi
echo "==> Backup saved to data/backups/$STAMP"
# Keep the ten most recent backups.
ls -1dt "$PROJECT_DIR"/data/backups/*/ 2>/dev/null | tail -n +11 | xargs -r rm -rf

# ------------------------------------------------------------------ 3. pull
echo "==> Fetching origin/$BRANCH"
git fetch --quiet origin "$BRANCH"
if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ]; then
  echo "    already up to date"
  UP_TO_DATE=1
else
  UP_TO_DATE=0
  echo "    incoming:"
  git log --format='      %h %s' "HEAD..origin/$BRANCH"
  git merge --ff-only --quiet "origin/$BRANCH" || {
    echo "!! Cannot fast-forward: this Pi has local commits." >&2
    echo "   Inspect with: git log --oneline origin/$BRANCH..HEAD" >&2
    exit 1
  }
fi
NEW_REV="$(git rev-parse HEAD)"

# ------------------------------------------------------- rollback on failure
rollback() {
  echo >&2
  echo "!! Update failed — rolling back to $(git log -1 --format=%h "$OLD_REV")" >&2
  git reset --quiet --hard "$OLD_REV"
  if ! git diff --quiet "$OLD_REV" "$NEW_REV" -- requirements.txt; then
    "$VENV/bin/pip" install --quiet -r requirements.txt || true
  fi
  sudo systemctl restart cycletime || true
  echo "   Old version restored. Backup: data/backups/$STAMP" >&2
  echo "   Logs: journalctl -u cycletime -n 50" >&2
  exit 1
}
[ "$OLD_REV" != "$NEW_REV" ] && trap rollback ERR

# ---------------------------------------------------------- 4. dependencies
if [ "$DO_FULL" = 1 ]; then
  echo "==> Running full installer (--full)"
  trap - ERR
  exec bash "$PROJECT_DIR/deploy/install.sh"
fi

if ! git diff --quiet "$OLD_REV" "$NEW_REV" -- requirements.txt; then
  echo "==> requirements.txt changed — installing packages (can take minutes)"
  "$VENV/bin/pip" install -r requirements.txt
else
  echo "==> Python packages unchanged"
fi

echo "==> Verifying the new code imports"
"$VENV/bin/python" - <<'PY'
import sys; sys.path.insert(0, "src")
import cv2, fastapi, uvicorn                     # noqa: F401
from cycletime import api, config, tracker       # noqa: F401
cfg = config.load()
print(f"    config ok · shift pattern {cfg.shifts.pattern} · starts {list(cfg.shifts.starts)}")
PY

if ! git diff --quiet "$OLD_REV" "$NEW_REV" -- deploy/install.sh deploy/kiosk.sh deploy/cycletime.service; then
  echo "    note: deploy scripts changed — consider  bash deploy/update.sh --full"
fi

# --------------------------------------------------------------- 5. restart
echo "==> Restarting service"
sudo systemctl restart cycletime

ok=0
for _ in $(seq 1 60); do
  if curl -fsS "http://localhost:$PORT/api/shifts/config" >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 1
done
if [ "$ok" != 1 ]; then
  echo "!! Service did not answer on port $PORT within 60 s" >&2
  false   # triggers rollback when code changed
fi
trap - ERR

echo
echo "==> Done: $(git log -1 --format='%h %s')"
if [ "$UP_TO_DATE" = 1 ]; then
  echo "    (code unchanged; service restarted)"
fi

# The kiosk browsers keep the old page in memory until they reload. A reboot
# is the path kiosk.sh is built and tested for, so that is the one offered.
if [ "$DO_REBOOT" = 1 ]; then
  echo "==> Rebooting in 5 s so both screens load the new UI (Ctrl+C to cancel)"
  sleep 5
  sudo reboot
else
  echo "    The screens still show the old page until reloaded."
  echo "    Reboot now so both screens pick it up:  sudo reboot"
fi

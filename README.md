# Conveyor Cycle-Time Tracker

Watches a conveyor with a USB camera, shows how much your cycle time
**fluctuates**, and scores your shift teams against each other — live, on the
factory floor, with no network and no cloud.

A camera looks down at the belt. You drag a box over the spot product passes
through. Every time a product crosses that box the time is recorded and stamped
with the team on shift. Two screens:

- **7" touchscreen** — run chart with control limits, distribution histogram,
  camera setup, and the shift calendar.
- **40" wall board** — live seconds-per-piece for the team on shift, diff
  against target, and day / week / month team rankings with the best on top.

### 40" wall board — `/board`

![40-inch scoreboard: the team on shift, its latest cycle time, the diff against target, pieces made against the shift target, and day / week / month team rankings, updating live as products pass](docs/board.gif)

### 7" touchscreen — dashboard `/`

![7-inch dashboard: run chart with mean and target lines and the cycle-time distribution, updating as each product passes, then switching to the table of recent cycles](docs/dashboard.gif)

### 7" touchscreen — camera setup `/setup`

![Setup page: the live camera view with the detection box, a product crossing it and the occupancy readout; a new box is dragged out, shown as unsaved, then undone](docs/setup.gif)

### 7" touchscreen — shift calendar `/shifts`

![Shift calendar: switching the pattern from 3 Teams × 8 h to 3 Teams × 12 h and 2 Teams × 12 h redraws the month, then the Pattern tab shows the four pattern buttons and the team boxes changing with each](docs/shifts.gif)

<sub>Recorded from the app running against a synthetic conveyor camera and seeded history.</sub>

## What you need

| | |
|---|---|
| Computer | Raspberry Pi 5, 8 GB (a Pi 4 works too) |
| Camera | Any USB webcam (UVC). 640×480 is plenty |
| Control screen | Official 7" DSI touchscreen — 800×480, or a Touch Display 2 (720×1280 portrait, run rotated) |
| Wall screen | Any HDMI display, 1920×1080 (40" here) |
| Lighting | Even light on the box you draw — see [Aiming the camera](#aiming-the-camera) |

No PLC, no sensors, no wiring into the machine.

> **คู่มือภาษาไทยสำหรับพนักงานหน้างาน: [user-manual.html](user-manual.html)**
> Thai-language operator manual — open it in a browser, or print to PDF to share.
> This README is the technical/install reference; the manual is the day-to-day one.

---

## Install on the Pi

Raspberry Pi OS **Bookworm** (64-bit, **Desktop**) on a Pi 5. The Desktop image
is required — the two kiosk browsers need a desktop session, which the Lite
image has no way to provide.

Run as the normal desktop user — **not** with `sudo`; the script escalates only
where it needs to.

```bash
# git ships with the Desktop image, so this is normally a no-op. Harmless
# either way, and it saves a confusing "git: command not found" if this Pi
# was flashed with the Lite image.
sudo apt update && sudo apt install -y git

git clone https://github.com/natn2014/ImageHash_CycleTime.git ~/cycletime
cd ~/cycletime
bash deploy/install.sh
```

The installer sets the timezone, installs dependencies, creates the virtualenv,
verifies the OpenCV stack actually imports, registers a systemd service that
starts at boot, and configures both screens to come up at login. It prints the
cameras and displays it found so you can confirm the hardware is seen.

Allow 5–15 minutes — the OpenCV wheel is large on a Pi.

Then:

1. Open **http://localhost:8000/setup** → drag a box across the belt → **Save ROI**
2. Open **http://localhost:8000/shifts** → **Pattern** → adjust the anchor date
   until **Next 14 days** matches the real roster
3. Let product run, and check the numbers against a stopwatch

```bash
journalctl -fu cycletime      # watch cycles being detected
sudo systemctl restart cycletime
timedatectl                   # must show your local timezone, not UTC
```

### Timezone — read this one

**The shift roster runs entirely on local time.** Which team gets credit for a
cycle, and which production day it belongs to, both come from the Pi's wall
clock. A Pi left on UTC files every cycle against the wrong shift — 7 hours out
in Thailand — and nothing on screen looks broken until someone checks the
numbers.

The installer sets `Asia/Bangkok` automatically. Elsewhere:

```bash
CYCLETIME_TZ=Asia/Singapore bash deploy/install.sh
```

A Pi has no battery-backed clock, so it boots at epoch until NTP syncs. On a
line with no network, fit an RTC module or expect the first few minutes after a
power cut to be mis-dated.

### Updating

On a Pi that is already installed, use `update.sh`. It backs up
`config.json` and the database to `data/backups/`, pulls with `--ff-only`,
reinstalls Python packages only when `requirements.txt` changed, restarts the
service, and rolls back to the previous commit if the new version fails to
start:

```bash
cd ~/cycletime && bash deploy/update.sh --reboot
```

`--reboot` reboots at the end so both kiosk screens load the new UI; leave it
off to only restart the service. `--full` re-runs `install.sh` after the pull
(use it when `deploy/` changed). A Pi installed before `update.sh` existed
needs one plain pull first to get the script:

```bash
cd ~/cycletime && git pull --ff-only && bash deploy/update.sh --reboot
```

`install.sh` is idempotent too, so `git pull && bash deploy/install.sh` still
works as a full re-install.

`config.json` and `data/cycles.db` are **not tracked by git**, so your ROI,
shift roster and recorded history all survive an update untouched.

`config.example.json` is the tracked template. The installer copies it to
`config.json` on a fresh install and never overwrites an existing one — this is
why a tuned line can be updated without being re-tuned.

### The two screens

| URL | Screen | Shows |
|---|---|---|
| `/` | 7" DSI | Run chart, histogram, live stats |
| `/board` | 40" HDMI | Team scoreboard |
| `/setup` | 7" DSI | Camera aiming, detection thresholds, target cycle time |
| `/shifts` | 7" DSI | Shift calendar and rotation pattern |

`deploy/kiosk.sh` lays the outputs side by side with `wlr-randr` — the DSI at
x=0, the HDMI immediately to its right — and starts two Chromium instances
positioned onto each.

The seam between them is **measured, not assumed**. The script reads each
output's logical size from `wlr-randr` (current mode ÷ scale, axes swapped when
the panel is rotated) and places the HDMI at exactly the DSI's width. This
matters on a rotated panel: a Touch Display 2 is a portrait 720×1280 that
occupies 1280×720 of layout when set to orientation *left*, so a hardcoded x=800
would drop the board 480 px on top of the dashboard. The script prints what it
measured at startup:

```
detected outputs: DSI='DSI-1' HDMI='HDMI-A-1'
logical sizes: DSI=1280x720 HDMI=1920x1080 (seam at x=1280)
```

**Each browser needs its own `--user-data-dir`, and the script sets one.** A
second Chromium sharing a profile does not open a second window — it hands the
URL to the running instance and exits, leaving the 40" blank. If you edit that
script, keep the separate profiles.

Each window is launched with its own app-id — `cycletime-board` for the 40" and
`cycletime-dash` for the 7" — so the two can be told apart by name.

**If both pages land on one screen** (usually the 40"), here's the actual
mechanism, worked out the hard way on a Pi 5 running labwc — the compositor
recent Raspberry Pi OS images use, including on Pi 5 (`echo $XDG_CURRENT_DESKTOP`).
It is not simply "`--window-position` is a hint and got ignored":

- `--kiosk` makes Chromium request true Wayland fullscreen. That request
  carries no target output of its own — the protocol has no field for it —
  so the compositor picks one itself.
- On labwc (and most wlroots compositors), that pick is "whichever output
  currently has the pointer." Nothing in `--window-position`, `--window-size`,
  or a compositor window rule changes this: they all affect a floating
  window's geometry, but a *fullscreen* window's geometry is just "the whole
  output," so there is nothing for them to change.
- Both browsers launch close together with the pointer stationary, so without
  intervention they request fullscreen while the pointer sits over the same
  output and both land there, stacked.

We verified this by testing every documented alternative and watching it fail
identically: `--window-position` alone, an XWayland run (`CYCLETIME_OZONE=x11`),
and a labwc `<windowRules>` `MoveTo` action keyed off the window title (`identifier`
doesn't even match — Chromium's Wayland/Ozone backend doesn't set the toplevel
app-id from `--class`, only XWayland's WM_CLASS gets it). None of them moved a
`--kiosk` window to a different output than the one the pointer was already on.

**The fix that actually works: move the pointer, not the window.** `kiosk.sh`
does this itself via `wlrctl pointer move` (`sudo apt install wlrctl` — the
installer does this for you) — warping the pointer into the target output's
bounds immediately before launching each browser, so each one's fullscreen
request lands where intended. If `wlrctl` isn't installed, `kiosk.sh` skips the
warp and placement is left to the compositor's default (usually wrong on a
dual-output kiosk) — install it and the two screens separate with no further
configuration. `CYCLETIME_OZONE=x11` still exists as a fallback for other
compositors where this doesn't apply, but it did not fix the labwc case above.

## Try it on Windows first

Everything runs on a normal PC with any webcam — useful for learning the
controls before you go near the line.

```bash
pip install -r requirements.txt
python run.py
```

Open http://localhost:8000/setup, drag a box somewhere on your desk, and wave
your hand through it. Each pass logs a cycle.

---

## Aiming the camera

**This is the part that decides whether the numbers are any good.** The
detection is brightness-difference based, so it is fast and needs no training —
but it believes anything that changes inside the box is product.

Do:
- Point the camera **down at the belt**, as square-on as you can
- Draw the box just wider than one product, well inside the belt edges
- Add a small fixed LED bar if the area is dim
- Watch the **Occupancy** meter on the setup page while product runs: it should
  sit near 0 % on an empty belt and jump well past the red mark as product passes

Avoid:
- Sunlight from a window, or anything that moves the shadows
- Operators, forklifts or hands crossing through the box
- Including the belt edge or a reflective guard rail in the box

### Tuning

Only touch these if the counts are wrong. All four are on the setup page and
save instantly.

Tap any number field and an on-screen keypad opens, with that field's own range
shown above the keys and the decimal key disabled where only whole numbers make
sense. The 7" panel has no keyboard, Chromium in kiosk mode raises no soft one,
and a number input's spinner arrows are smaller than a fingertip — so the pad
is the only way these get filled in on the line.

| Setting | Raise it when | Lower it when |
|---|---|---|
| **Enter ratio** (0.15) | Shadows/noise trigger false counts | Small product isn't detected |
| **Exit ratio** (0.07) | Product isn't seen as "gone" between pieces | The box flickers empty mid-product |
| **Pixel threshold** (25) | The image is noisy or grainy | Product barely differs from the belt |
| **Dwell** (0.3 s) | Brief flickers get counted | Fast product is missed |

Exit must stay below enter — the gap between them is what stops one product
being counted twice.

**Prove it before you trust it:** hand-count 20 consecutive products with a
stopwatch and compare against the dashboard. Expect one round of tuning.

---

## Reading the dashboard

**Run chart** — every cycle in sequence. This is where fluctuation shows up.

- **x̄** solid line — your average cycle
- **UCL / LCL** dashed red — control limits. Points outside are *not* normal
  variation; something specific happened
- **target** dotted green — your goal (set `cycle.target_s` in `config.json`)
- **Red dots** — out-of-control cycles, drawn larger as well as coloured

A healthy line looks like random scatter tight around x̄. Watch for a drift up
(tooling wearing), a step change (shift or material change), or a repeating
sawtooth (something happening every Nth piece).

**Distribution** — the shape of the variation. One clean peak is a stable
process. **Two peaks means the line is really running two different processes**
— often an operator intervention on some pieces but not others. That single
insight is usually the first thing this tool finds.

**Fluct %** in the header is the coefficient of variation. It is scale-free, so
it stays comparable when you change product or line speed. Lower is better.

**Table** button switches to a text view of every cycle, including stoppages.
**CSV** downloads the whole history for Excel.

### Stoppages

Any gap longer than `cycle.max_valid_s` (default 5 minutes) is recorded as a
*stoppage*, not a cycle. It is excluded from the chart, the average and the
control limits — otherwise one tea break rescales the chart and hides all the
real variation. Stoppages still appear in the header chip, the table and the CSV.

---

## Shift teams

**The roster is calculated, not typed in** — you pick a shift pattern and set it
up once, and the calendar fills itself forever. You only tap a day when
something is unusual.

![Shift calendar and pattern selector](docs/shifts.gif)

### Shift patterns

Choose one of four crewing patterns. The pattern sets how long a shift is,
how many start times and teams the rota needs, and the per-shift piece target
on the 40" board (a 12-hour shift has a target 1.5× that of an 8-hour one).

| Pattern | Shifts per day | Default starts | Teams | How it rotates |
|---|---|---|---|---|
| **3 Teams × 8 h** (default) | 3 | 08:00 / 16:00 / 00:00 | A B C | Each team moves to the next shift every period |
| **3 Teams × 12 h** | 2 | 08:00 / 20:00 | A B C | Two teams work, one is **off**; the off team comes back next period |
| **2 Teams × 12 h** | 2 | 08:00 / 20:00 | A B | The teams swap day and night every period |
| **2 Teams × 8 h** | 2 | 08:00 / 16:00 | A B | The teams swap shifts; the remaining 8 h have **no team** |

Two places to change it:

- **Calendar** — the dropdown beside the month name. It asks for confirmation,
  then applies the pattern at once with its default start times, keeping your
  team letters where they fit.
- **Pattern tab** — the four buttons at the top. The start-time fields and team
  boxes reshape to match; nothing is stored until **Save pattern**.

Under **2 Teams × 8 h**, products made during the unstaffed 8 hours are still
recorded, but count toward no team. Changing pattern does not re-attribute
cycles already recorded — see [If the calendar was wrong](#if-the-calendar-was-wrong).

### Setting the pattern (do this once)

**Shifts → Pattern**:

| Field | Default | Meaning |
|---|---|---|
| Shift pattern | 3 Teams × 8 h | See the table above |
| Shift 1/2(/3) start | per pattern | Each shift runs 8 or 12 hours from its start |
| Teams | A,B(,C) | One per shift in rotation order; with 3 Teams × 12 h the third box is the team **off** on the anchor date |
| Rotate every | 7 days | How often teams move to the next shift |
| Rotation anchor | a date | A day on which the first team box is on shift 1 |

**Do not count weeks to work out the anchor.** Change the date and watch the
**Next 14 days** preview beside it until it matches who is really on shift.
That is the whole point of the preview.

Shift order follows the order you enter the times, not the clock. Listing
`08:00, 16:00, 00:00` means shift 1 is the 08:00 day shift and shift 3 is the
00:00 night shift — which is how the plant talks about them.

### Overriding a day

**Shifts → Calendar**. Each cell shows the team on each shift that day (three
under 3 × 8 h, two under the other patterns). Tap a day,
then tap a team for any shift. Overridden slots are marked in amber and
underlined. **Back to rotation** clears the day.

Choose **—** to record that *nobody* was on that shift — a holiday, or an idle
line. That is different from leaving it alone: `—` means no team, while an
untouched day follows the rotation.

### Which day does a shift count toward?

A shift's cycles count to the day the **shift started**. With 08:00/16:00/00:00
no shift crosses midnight, so this changes nothing. It matters for any shift
that runs past midnight — the 20:00 night shift of a 12-hour pattern, or a
22:00 start: cycles recorded at 02:00 count toward the *previous* day, keeping
one shift's performance in one bucket.

### If the calendar was wrong

Attribution is stamped when the cycle is recorded, so fixing the calendar later
does not silently rewrite history. When you *do* want history corrected — a
shift swap entered a day late — fix the calendar, then:

```bash
curl -X POST http://localhost:8000/api/shifts/recompute \
     -H 'Content-Type: application/json' \
     -d '{"start":"2026-08-01","end":"2026-08-07"}'
```

Omit the body to re-stamp everything.

---

## Reading the 40" board

- **TEAM x: N sec** — the team on shift now, and the latest cycle time.
- **Diff bar** — green while within `diff_tolerance_s` of target, red beyond.
  The arrow and the signed number say the same thing as the colour, so it still
  reads in a monochrome photo.
- **จำนวน bar** — pieces the team on shift has made, against the target for a
  full shift, with **เหลืออีก** counting down what is left. It turns green on the
  last figure once the target is met.
- **CT / จำนวน panels** — the same work date read two ways: cycle time per team
  on the left (**lowest on top**), pieces made per team on the right (**most on
  top**). Low wins on time, high wins on output, so the two panels rank in
  opposite directions by design.
- **Month** — average per team, **best cycle time on top**, with an explicit
  rank number. A team that has not run shows `—` and sorts last, never first.
- **Week** — W1 = days 1–7, W2 = 8–14, W3 = 15–21, W4 = 22–end of month. The
  best cell in each column is highlighted.
- **NOW** tag marks the team currently on shift.

Stoppages are excluded from every average **and from the piece counts** — a
stoppage is not a product, so it neither drags an average down nor counts
toward the shift target.

### Where the จำนวน target comes from

Nobody enters it. It is derived from the target cycle time and the length of a
shift, which the roster already knows:

```
shift seconds / target cycle time, rounded DOWN to a memorable figure

 8 h at 14 s/piece  ->  28800 / 14  =  2057  ->  2000 pcs
12 h at 14 s/piece  ->  43200 / 14  =  3085  ->  3000 pcs
```

The step scales with the size of the number — hundreds in the thousands, tens
in the hundreds, exact below that — so a long cycle time is not distorted by a
rounding rule meant for a short one. A flat hundred would floor 480 (a 60 s
cycle) to 400, discarding 17% of an achievable target, and would floor anything
under 100 to zero, which the board would then show as a met target with nothing
made.

The shift length comes from the selected [shift pattern](#shift-patterns) —
8 h or 12 h — the same figure the roster uses for its shift windows, so the two
cannot drift apart. Rounding down rather than
to nearest is deliberate: 2057 is not a number anyone carries in their head or
writes on a whiteboard, and never rounding up keeps the target honest — it is
never set above what the cycle time can actually deliver.

Set it on the **Setup** page under **Cycle target** — type a cycle time on the
keypad and the panel previews the piece target it produces before you commit
to it. The 40" board picks the new figure up on its next refresh; nothing
needs restarting.

If the board loses contact with the tracker it dims and shows **NO DATA**
rather than leaving a stale number on the wall to be believed.

---

## Configuration

`config.json`, next to `run.py`. The setup page writes `roi` and `detector`,
the Shifts tab writes `shifts`; everything else is edited by hand.

```jsonc
{
  "station":  { "name": "LAYUP2" },
  "camera":   { "index": 0, "width": 640, "height": 480, "fps": 15, "fourcc": "MJPG" },
  "roi":      { "x": 200, "y": 150, "w": 240, "h": 180 },
  "detector": { "enter_ratio": 0.15, "exit_ratio": 0.07, "diff_threshold": 25,
                "bg_alpha": 0.02, "min_present_s": 0.3 },
  "cycle":    { "max_valid_s": 300, "target_s": 14.0, "diff_tolerance_s": 1.0 },
  "shifts":   { "pattern": "3x8",   // 3x8 | 3x12 | 2x12 | 2x8
                "starts": ["08:00", "16:00", "00:00"], "teams": ["A", "B", "C"],
                "rotation_days": 7, "rotation_anchor": "2026-08-03",
                "rotation_direction": 1 },
  "store":    { "db_path": "data/cycles.db", "retain_days": 90 },
  "server":   { "host": "0.0.0.0", "port": 8000 }
}
```

Set **`station.name`** to the line name shown on the board — this is the only
change needed to label a second unit `LAYUP1`.
Set **`cycle.target_s`** to your takt time — it draws the target line and feeds
the board's diff bar. **`diff_tolerance_s`** is how far over target the bar
stays green.
Keep **`"fourcc": "MJPG"`** — on the default YUYV many webcams silently drop to
about 5 fps.

A bad value here is repaired rather than rejected: an unparseable shift time or
a team count that does not match the number of shifts falls back to the default
so the line display still boots.

Command-line flags (`--camera 1`, `--port 8080`) override the file for one run
without changing it.

## Data

Every cycle goes to `data/cycles.db` (SQLite), pruned to `retain_days`
automatically. Roughly 3 000 cycles/day is under 1 MB/month.

```
GET /api/export.csv                                   # everything
GET /api/export.csv?start=2026-08-01&end=2026-08-07   # a date range
```

The CSV carries both UTC and local time plus `team`, `work_date` and
`shift_slot`, so per-team analysis works straight in a pivot table.

---

## How it works

```
capture thread          detector thread              web server
──────────────          ───────────────              ──────────
grab newest frame  ───▶  ROI brightness diff   ───▶   SQLite
stamp it with            vs frozen background         dashboard + board
time.monotonic()         state machine + dwell        SSE push per cycle
                         stamp team from roster
```

Four decisions carry most of the accuracy:

**Frames are timestamped at grab, on a monotonic clock.** A webcam buffers
frames; reading at processing speed drifts further behind real time every
frame. A dedicated thread always holds the newest frame, and a monotonic clock
means an NTP correction mid-shift cannot invent a cycle.

**The background freezes while the box is occupied.** A conventional background
subtractor keeps learning, so a product that stops on the belt gets absorbed
into the background and the next movement fires a phantom count. Freezing means
a stopped line reads as one long cycle — the truth. It still learns while the
box is empty, which absorbs lighting drift across a shift. If the whole scene
changes while occupied (someone hits the lights), a guard forces a relearn
rather than wedging.

**Control limits use the I-MR method**, `σ̂ = MR̄ / 1.128`, not a raw standard
deviation. A raw σ is inflated by the very outliers you are hunting, widening
the limits until nothing ever signals.

**The team is stamped onto the cycle when it is recorded**, not joined from the
roster when the board is drawn. Recorded history therefore reflects what the
schedule said when the product was actually made, and editing next week's
calendar cannot silently move last month's numbers. `/api/shifts/recompute` is
the deliberate way to change that.

---

## Testing

```bash
python -m pytest tests/ -q
```

98 tests, no hardware needed.

`tests/make_synthetic_video.py` renders a synthetic conveyor with products at
exactly known intervals; the detector must recover those intervals to within
one frame period. To watch the scene:

```bash
python tests/make_synthetic_video.py --intervals 8,12,9,15,11 --out belt.mp4
```

`tests/test_shifts.py` covers the roster as pure arithmetic — rotation forward
and backward, shift boundaries to the second, the shift-start production-day
rule under a midnight-crossing pattern, and the difference between "no override"
and "nobody on shift". `tests/test_team_store.py` covers the database migration
running repeatedly against the same file, which is what happens on every deploy.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| **NO CAMERA** | `v4l2-ctl --list-devices`. Check the cable; try `--camera 1`. On the Pi the user must be in the `video` group (log out and back in after install) |
| **Nothing is counted** | Occupancy never passes the red mark — lower **Enter ratio** or **Pixel threshold**, or make the box smaller so product fills more of it |
| **Counts double** | Widen the gap between **Enter** and **Exit**, or raise **Dwell** |
| **Random counts on an empty belt** | Something moves in the box — shadow, person, sunlight. Re-aim first; raise **Pixel threshold** second |
| **Stuck on PRODUCT** | Lighting changed, or the box includes something that moved permanently. It self-recovers after `max_valid_s`; re-save the ROI to force it now |
| **Chart is empty but cycles log** | All gaps are being flagged as stoppages — raise `cycle.max_valid_s` |
| **Camera runs slow** | Confirm MJPG: `v4l2-ctl --list-formats-ext` |
| **Screen blanks** | `deploy/kiosk.sh` disables blanking; make sure it's in `~/.config/autostart` |
| **"Unlock your login keyring" blocks the screens at boot** | Chromium is asking gnome-keyring for its encryption key, which autologin never unlocked. `kiosk.sh` passes `--password-store=basic` to avoid it. If a prompt still appears, clear the stored keyring: `rm -f ~/.local/share/keyrings/*.keyring` and reboot |
| **40" stays blank / both screens show the same page** | The two Chromium instances are sharing a profile. Each needs its own `--user-data-dir` — see `deploy/kiosk.sh` |
| **Both pages open on one screen** | Check `wlr-randr` positioned the outputs at all first. If it did, `wlrctl` is probably missing (`sudo apt install wlrctl`) — see "The two screens" above for why that, not `--window-position`, is what actually separates them |
| **Neither screen shows anything after a reboot, but the tracker/camera is running fine** | Two independent things to check. (1) `ls -l deploy/kiosk.sh` — if it's not `-rwxr-xr-x`, a `git pull` reset the exec bit and the autostart entry silently failed to run it (`bash deploy/install.sh` fixes this and makes it durable). (2) `kiosk.sh` waits up to 60s for `/api/health` before showing anything; some desktop sessions kill an autostart entry that takes that long to show a window, assuming it hung. The autostart entry `install.sh` writes backgrounds `kiosk.sh` with `setsid` so it returns instantly regardless — if you're running an older installed entry, re-run `bash deploy/install.sh` to pick that up |
| **Wrong team on the board** | Check **Shifts → Pattern** preview against reality and adjust the anchor date. Past data is corrected with `/api/shifts/recompute` |
| **Board shows "NO TEAM SCHEDULED"** | That shift is set to `—` on the calendar. Tap the day and pick a team |
| **A team shows `—` all month** | It has recorded no cycles — check the rotation covers it, and that the camera was running on its shifts |

## Limits

This counts **passes, not good parts**. Two products touching may read as one;
it cannot tell a good piece from a reject. It measures the gap between products
at one point on the belt — which is exactly what cycle-time fluctuation means,
but it is not a quality system.

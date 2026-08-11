"""Runtime orchestration: camera -> detector -> store -> subscribers.

Owns the background detection thread and the shared state the HTTP layer reads.
Keeping it separate from api.py avoids a circular import and keeps the FastAPI
module free of threading concerns.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timezone

from . import config as config_mod
from .camera import CaptureThread
from .config import Config, Roi, ShiftsConfig
from .detector import CycleEvent, TripLineDetector
from .shifts import Roster
from .store import CycleStore, utcnow_iso

log = logging.getLogger(__name__)

# Ignore detections for this long after start or an ROI change, then relearn
# the background from a settled frame. A USB webcam auto-exposes over roughly
# its first second, so the frame the background was seeded from is darker than
# everything that follows - without the reseed the ROI reads as permanently
# changed and the detector wedges. Three seconds costs nothing at 5-30 s cycles.
WARMUP_S = 3.0

# How often to drop rows past the retention window.
PRUNE_INTERVAL_S = 6 * 3600

# A jump this large between consecutive frames means the camera dropped out and
# came back. Whatever it is now pointing at, and at whatever exposure, the old
# background is worthless - so warm up again.
CAMERA_GAP_S = 2.0


class Tracker:
    def __init__(self, cfg: Config, config_path=None):
        self.cfg = cfg
        self.config_path = config_path or config_mod.DEFAULT_CONFIG_PATH
        self.store = CycleStore(cfg.store.resolved_path())
        self.camera = CaptureThread(cfg.camera)
        self.detector = TripLineDetector(cfg.detector, cfg.roi, cfg.cycle.max_valid_s)
        self.roster = self._build_roster(cfg.shifts)

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue] = set()

        # None means "no warm-up window open yet". The window is started by the
        # first frame that arrives, never by the clock: opening a USB camera can
        # itself take several seconds, and a window that expires before any
        # frame is seen would leave the background seeded from the camera's
        # first, unexposed frame - the exact thing warm-up exists to avoid.
        self._warmup_until: float | None = None
        self._warmup_reseeded = False
        self._last_frame_t: float | None = None
        self._last_prune = 0.0
        self.started_at_iso = utcnow_iso()
        self.last_cycle: dict | None = None
        # When the last product was detected, on the monotonic clock, so the
        # board can run a stopwatch for the cycle in progress. Monotonic and not
        # wall time: this Pi has no RTC and steps its clock by whatever NTP says
        # once the network is up, which would otherwise jump the counter.
        self.last_edge_mono: float | None = None

    # ---------------------------------------------------------------- roster

    def _build_roster(self, shifts: ShiftsConfig) -> Roster:
        """A Roster wired to look overrides up in the database.

        The lookup returns NO_OVERRIDE when no row exists, so the rotation
        applies; a stored NULL team returns None, meaning a deliberate "nobody
        on this shift" that must NOT fall back to the rotation.
        """
        def lookup(work_date: str, slot: int):
            found = self.store.get_overrides(work_date, work_date)
            return found.get((work_date, slot), Roster.NO_OVERRIDE)

        return Roster(
            starts=list(shifts.starts),
            teams=list(shifts.teams),
            rotation_days=shifts.rotation_days,
            rotation_anchor=shifts.rotation_anchor,
            rotation_direction=shifts.rotation_direction,
            override_lookup=lookup,
        )

    def update_shifts(self, shifts: ShiftsConfig) -> ShiftsConfig:
        """Apply a new shift pattern from the Pattern tab and persist it."""
        cfg = config_mod.validate(replace(self.cfg, shifts=shifts))
        with self._lock:
            self.roster = self._build_roster(cfg.shifts)
        self.cfg = cfg
        config_mod.update_file(self.config_path, shifts=cfg.shifts)
        log.info("shift pattern updated: starts=%s teams=%s every %dd",
                 cfg.shifts.starts, cfg.shifts.teams, cfg.shifts.rotation_days)
        return cfg.shifts

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.camera.start()
        self._restart_warmup()
        self._thread = threading.Thread(target=self._run, name="detector", daemon=True)
        self._thread.start()
        removed = self.store.prune(self.cfg.store.retain_days)
        self._last_prune = time.monotonic()
        if removed:
            log.info("pruned %d rows older than %d days", removed, self.cfg.store.retain_days)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.camera.stop()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the server's event loop so the detector thread can push to it."""
        self._loop = loop

    # ------------------------------------------------------------- detection

    def _restart_warmup(self) -> None:
        """Arm a fresh warm-up; the next frame to arrive opens the window."""
        self._warmup_until = None
        self._warmup_reseeded = False

    def _warming_up(self, t_mono: float) -> bool:
        return self._warmup_until is None or t_mono < self._warmup_until

    def _run(self) -> None:
        seq = 0
        while not self._stop.is_set():
            frame, t_mono, seq = self.camera.wait_for_frame(seq, timeout=1.0)
            if frame is None:
                continue

            # A gap in the frame stream means the camera dropped and came back.
            if (self._last_frame_t is not None
                    and t_mono - self._last_frame_t > CAMERA_GAP_S):
                log.info("camera stream resumed after %.1fs gap; warming up again",
                         t_mono - self._last_frame_t)
                self._restart_warmup()
            self._last_frame_t = t_mono

            if self._warmup_until is None:
                self._warmup_until = t_mono + WARMUP_S

            if not self._warmup_reseeded:
                if t_mono < self._warmup_until:
                    continue        # still settling; don't even build a background
                # Settled: learn the background from this frame onward.
                self._warmup_reseeded = True
                with self._lock:
                    self.detector.reseed()
                log.info("warm-up complete, learning background")
                continue

            with self._lock:
                event = self.detector.process(frame, t_mono)

            if event is not None:
                self._record(event)

            now = time.monotonic()
            if now - self._last_prune > PRUNE_INTERVAL_S:
                self._last_prune = now
                self.store.prune(self.cfg.store.retain_days)

    def _record(self, event: CycleEvent) -> None:
        """Persist a detection and push it to any connected dashboards.

        The very first detection after start has no preceding edge, so there is
        no interval to record - it only establishes the reference point for the
        next one.
        """
        # Before the early return: the first product starts no interval but it
        # does start the *next* one, which is what the live stopwatch counts.
        self.last_edge_mono = time.monotonic()

        if event.cycle_s is None:
            log.info("first product detected; timing starts from here")
            return

        # Attribute to whoever is on shift right now. Resolved at write time so
        # the row records what the roster said when the product was made.
        assignment = self.roster.current()

        row = self.store.insert_cycle(
            event.cycle_s, event.is_stoppage,
            team=assignment.team,
            work_date=assignment.work_date,
            slot=assignment.slot,
        )
        self.last_cycle = row
        log.info("cycle %.2fs  team=%s%s", row["cycle_s"], assignment.team or "-",
                 " (stoppage)" if row["is_stoppage"] else "")
        self._broadcast({"type": "cycle", "cycle": row})

    def _broadcast(self, payload: dict) -> None:
        """Hand a message to every SSE subscriber, from the detector thread.

        call_soon_threadsafe is the only sanctioned way to touch asyncio objects
        from another thread; without it the queues would corrupt under load.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        for queue in list(self._subscribers):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except RuntimeError:  # pragma: no cover - loop shutting down
                pass

    # ------------------------------------------------------------ subscribers

    def subscribe(self) -> asyncio.Queue:
        # Bounded: a dashboard on a wedged connection must not grow unbounded.
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    # ----------------------------------------------------------------- config

    def update_roi(self, roi: Roi) -> Roi:
        """Apply a new ROI from the setup page and persist it."""
        frame, _, _ = self.camera.latest()
        if frame is not None:
            h, w = frame.shape[:2]
            roi = roi.clamped(w, h)

        with self._lock:
            self.detector.set_roi(roi)
        self.cfg = replace(self.cfg, roi=roi)
        self._restart_warmup()
        config_mod.update_file(self.config_path, roi=roi)
        log.info("ROI updated to x=%d y=%d w=%d h=%d", roi.x, roi.y, roi.w, roi.h)
        return roi

    def update_detector(self, det_cfg) -> None:
        cfg = config_mod.validate(replace(self.cfg, detector=det_cfg))
        with self._lock:
            self.detector.set_config(cfg.detector)
        self.cfg = cfg
        config_mod.update_file(self.config_path, detector=cfg.detector)

    # ----------------------------------------------------------------- status

    def _current_shift_dict(self) -> dict:
        a = self.roster.current()
        return {
            "team": a.team, "slot": a.slot,
            "work_date": a.work_date, "slot_start": a.slot_start,
        }

    def _since_last_edge(self) -> float | None:
        """Seconds since the last detection, or None if there has not been one.

        Prefers the monotonic edge recorded by the detector thread. After a
        restart that is gone while the cycles themselves are still on disk, so
        it falls back to the stored timestamp — less precise, and only as good
        as the clock, but it beats a board that counts from zero every deploy.
        """
        if self.last_edge_mono is not None:
            return round(time.monotonic() - self.last_edge_mono, 3)

        last = self.last_cycle
        if last is None:
            recent = self.store.recent(1)
            last = recent[0] if recent else None
        if not last or not last.get("ts_utc"):
            return None
        try:
            then = datetime.fromisoformat(last["ts_utc"])
        except ValueError:
            return None
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return round(max(0.0, (datetime.now(timezone.utc) - then).total_seconds()), 3)

    def board(self) -> dict:
        """Everything the 40" scoreboard renders, in one payload.

        Assembled server-side so the board stays a dumb renderer - it does no
        date arithmetic and no ranking of its own, which keeps the day, week
        and month panels guaranteed consistent with each other.
        """
        from .shifts import month_bounds, week_bounds

        today = date.today()
        shift = self.roster.current()
        teams = list(self.cfg.shifts.teams)

        def ranked(start: str, end: str) -> list[dict]:
            """Teams best (lowest average) first, as the mockup specifies."""
            stats = self.store.averages_by_team(start, end)
            rows = [{"team": t, **stats.get(t, {"avg": None, "n": 0})} for t in teams]
            # Teams with no cycles sort last, never first: a team that did not
            # run is not the best performer.
            rows.sort(key=lambda r: (r["avg"] is None, r["avg"] if r["avg"] is not None else 0))
            for i, r in enumerate(rows):
                r["rank"] = i + 1 if r["avg"] is not None else None
            return rows

        m_start, m_end = month_bounds(today.year, today.month)
        weeks = []
        for w in range(1, 5):
            w_start, w_end = week_bounds(today.year, today.month, w)
            weeks.append({
                "week": w,
                "teams": {t: s["avg"] for t, s in
                          self.store.averages_by_team(w_start, w_end).items()},
            })

        # last_cycle only exists in memory, so a service restart mid-shift
        # would blank the board even though the cycle is on disk. Fall back to
        # the most recent stored row.
        last = self.last_cycle
        if last is None:
            recent = self.store.recent(1)
            last = recent[0] if recent else None

        target = self.cfg.cycle.target_s
        live_s = last["cycle_s"] if last and not last.get("is_stoppage") else None

        return {
            "station": self.cfg.station.name,
            "now": datetime.now().isoformat(timespec="seconds"),
            "work_date": shift.work_date,
            "shift": {"team": shift.team, "slot": shift.slot, "start": shift.slot_start},
            "live": {
                "cycle_s": live_s,
                "target_s": target,
                "diff_s": None if live_s is None else round(live_s - target, 2),
                "tolerance_s": self.cfg.cycle.diff_tolerance_s,
                # Seconds since the last product passed. The board counts on
                # from this locally at 10 Hz; sending it on every poll is what
                # re-anchors that counter, so a reloaded page or a missed SSE
                # event cannot leave it drifting.
                "since_s": self._since_last_edge(),
            },
            "teams": teams,
            "day": ranked(shift.work_date, shift.work_date),
            "weeks": weeks,
            "month": {
                "label": today.strftime("%B").upper(),
                "rows": ranked(m_start, m_end),
            },
            "camera_connected": self.camera.connected,
        }

    def status(self) -> dict:
        frame, frame_t, _ = self.camera.latest()
        h, w = (frame.shape[:2] if frame is not None else (0, 0))
        age = (time.monotonic() - frame_t) if frame_t else None
        return {
            "camera_connected": self.camera.connected,
            "camera_error": self.camera.last_error,
            "frames_captured": self.camera.frames_captured,
            "frame_age_s": round(age, 2) if age is not None else None,
            "frame_width": w,
            "frame_height": h,
            "detector_state": self.detector.state.value,
            "detector_ready": self.detector.ready,
            "occupancy": round(self.detector.occupancy, 4),
            "detections": self.detector.total_detections,
            # A climbing count here means the scene keeps changing under the
            # frozen background - usually unstable lighting over the ROI.
            "stuck_resets": self.detector.stuck_resets,
            "warming_up": self._warming_up(time.monotonic()),
            "roi": {"x": self.cfg.roi.x, "y": self.cfg.roi.y, "w": self.cfg.roi.w, "h": self.cfg.roi.h},
            "target_s": self.cfg.cycle.target_s,
            "station": self.cfg.station.name,
            "shift": self._current_shift_dict(),
            "started_at": self.started_at_iso,
            "last_cycle": self.last_cycle,
            "total_rows": self.store.count(),
            "today_count": self.store.count_today(),
            "session_count": self.store.count_since(self.started_at_iso),
        }

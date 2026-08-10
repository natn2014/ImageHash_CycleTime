"""Virtual trip-line detector.

Pure logic: frames and timestamps go in, cycle events come out. It owns no
camera, no database and no clock of its own, which is what lets the test suite
drive it frame-by-frame from a synthetic video on a machine with no hardware.

Method
------
Grey-scale frame differencing against a running-average background, evaluated
over the ROI only:

    occupancy = fraction of ROI pixels where |roi - background| > diff_threshold

    EMPTY    --occupancy > enter_ratio, held min_present_s--> OCCUPIED
    OCCUPIED --occupancy < exit_ratio,  held min_present_s--> EMPTY

The EMPTY -> OCCUPIED edge is one product passing the line. The cycle time is
the interval between two consecutive rising edges.

Three details do the real work:

1. The background updates *only while EMPTY*. A conventional subtractor (MOG2
   and friends) keeps learning, so a product that stops on the belt is slowly
   absorbed into the background; the ROI then reads "empty" and the next motion
   fires a phantom cycle. Freezing the update while occupied makes a stopped
   line read as one long cycle, which is the truth. Learning while empty still
   soaks up slow lighting drift over a shift.

2. Hysteresis (enter_ratio > exit_ratio) plus a min_present_s dwell. A product
   with a hole, a gap or a glossy band would otherwise flicker the state
   machine and count itself two or three times.

3. Gaps beyond max_valid_s are flagged as stoppages rather than cycles, so a
   tea break does not enter the statistics or rescale the chart.

The freeze in (1) has one failure mode, and it is guarded: if the whole scene
changes *while* the ROI reads occupied - a webcam still auto-exposing at
startup, someone switching on the overhead lights - the background is frozen
against an image that no longer exists, occupancy pins high, and the state
machine wedges OCCUPIED forever with no further detections. `stuck_reset_s`
catches that: an occupancy that outlasts any plausible product forces the
background to relearn. See reseed().
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np

from .config import DetectorConfig, Roi


class State(str, Enum):
    EMPTY = "empty"
    OCCUPIED = "occupied"


@dataclass(frozen=True)
class CycleEvent:
    """One product crossed the line."""

    # Monotonic timestamp of the frame that tripped the line.
    t_mono: float
    # Seconds since the previous product. None for the very first detection,
    # where there is no preceding edge to measure from.
    cycle_s: float | None
    is_stoppage: bool
    occupancy: float


class TripLineDetector:
    def __init__(
        self,
        cfg: DetectorConfig,
        roi: Roi,
        max_valid_s: float = 300.0,
        stuck_reset_s: float | None = None,
    ):
        self.cfg = cfg
        self.roi = roi
        self.max_valid_s = max_valid_s
        # Default the wedge guard to the stoppage threshold: an ROI occupied
        # for longer than that is a stopped line, not a passing product.
        self.stuck_reset_s = max_valid_s if stuck_reset_s is None else stuck_reset_s
        self.reset()

    # ------------------------------------------------------------------ setup

    def reset(self) -> None:
        """Clear all learned state. Called on start and on any ROI change."""
        self._bg: np.ndarray | None = None
        self._state = State.EMPTY
        # Monotonic time at which the current candidate transition first
        # appeared; None when the reading agrees with the committed state.
        self._pending_since: float | None = None
        self._state_since: float | None = None
        self._last_edge_t: float | None = None
        self._occupancy = 0.0
        self.total_detections = 0
        self.stuck_resets = 0

    def reseed(self) -> None:
        """Relearn the background from the next frame, keeping cycle timing.

        Used after the warm-up window (a USB webcam's auto-exposure settles over
        roughly the first second, so the very first frame is a poor reference)
        and by the wedge guard. Unlike reset() this preserves the last-edge
        reference and the counters, so nothing already measured is lost.
        """
        self._bg = None
        self._state = State.EMPTY
        self._pending_since = None
        self._state_since = None
        self._occupancy = 0.0

    def set_roi(self, roi: Roi) -> None:
        """Re-aim the trip-line and relearn the background from scratch.

        The old background belongs to different pixels, so keeping it would
        produce a burst of false detections right after the operator saves.
        """
        self.roi = roi
        self.reset()

    def set_config(self, cfg: DetectorConfig) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> State:
        return self._state

    @property
    def occupancy(self) -> float:
        return self._occupancy

    @property
    def ready(self) -> bool:
        """True once a background exists, i.e. detections are meaningful."""
        return self._bg is not None

    # ---------------------------------------------------------------- process

    def _crop(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        r = self.roi.clamped(w, h)
        patch = frame[r.y:r.y + r.h, r.x:r.x + r.w]
        if patch.ndim == 3:
            patch = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        # Blur before differencing: sensor noise on a cheap webcam otherwise
        # trips single pixels over the threshold and inflates occupancy.
        return cv2.GaussianBlur(patch, (5, 5), 0)

    def process(self, frame: np.ndarray, t_mono: float) -> CycleEvent | None:
        """Feed one frame. Returns a CycleEvent on a rising edge, else None.

        `t_mono` must be the time the frame was *captured*, not the time it was
        processed, or queueing jitter leaks straight into the measurement.
        """
        gray = self._crop(frame).astype(np.float32)

        if self._bg is None:
            # First frame seeds the background; nothing to compare against yet.
            self._bg = gray.copy()
            return None

        if self._bg.shape != gray.shape:
            # Resolution changed under us (camera reconnect); restart cleanly.
            self._bg = gray.copy()
            return None

        diff = cv2.absdiff(gray, self._bg)
        changed = int(np.count_nonzero(diff > self.cfg.diff_threshold))
        self._occupancy = changed / float(gray.size)

        if self._is_wedged(t_mono):
            # The background no longer describes the scene. Relearn from this
            # frame rather than sitting occupied forever detecting nothing.
            self.stuck_resets += 1
            self.reseed()
            self._bg = gray.copy()
            self._state_since = t_mono
            return None

        event = self._advance(t_mono)

        if self._state is State.EMPTY:
            # Learn the belt only when nothing is on it. See module docstring.
            cv2.accumulateWeighted(gray, self._bg, self.cfg.bg_alpha)

        return event

    def _is_wedged(self, t_mono: float) -> bool:
        """True when the ROI has read occupied for longer than any product can."""
        if self._state is not State.OCCUPIED or self._state_since is None:
            return False
        return (t_mono - self._state_since) > self.stuck_reset_s

    def _advance(self, t_mono: float) -> CycleEvent | None:
        """Run the debounced state machine for the current occupancy."""
        occ = self._occupancy
        cfg = self.cfg

        if self._state_since is None:
            self._state_since = t_mono

        if self._state is State.EMPTY:
            candidate = occ > cfg.enter_ratio
        else:
            candidate = occ < cfg.exit_ratio

        if not candidate:
            # Reading agrees with the committed state; drop any pending change.
            self._pending_since = None
            return None

        if self._pending_since is None:
            self._pending_since = t_mono
            return None

        if (t_mono - self._pending_since) < cfg.min_present_s:
            return None  # still inside the dwell window

        # Dwell satisfied: commit the transition.
        self._pending_since = None
        self._state_since = t_mono

        if self._state is State.EMPTY:
            self._state = State.OCCUPIED
            return self._emit(t_mono)

        self._state = State.EMPTY
        return None

    def _emit(self, t_mono: float) -> CycleEvent:
        """Build the event for a committed rising edge.

        The edge is timed at `_pending_since`... deliberately not. We time it at
        the commit instant, and because every edge is delayed by the same
        min_present_s dwell, that constant cancels out of the *interval* between
        two edges. Cycle times stay exact; only the absolute timestamp carries
        the fixed offset.
        """
        self.total_detections += 1

        cycle_s: float | None = None
        is_stoppage = False
        if self._last_edge_t is not None:
            cycle_s = t_mono - self._last_edge_t
            is_stoppage = cycle_s > self.max_valid_s
        self._last_edge_t = t_mono

        return CycleEvent(
            t_mono=t_mono,
            cycle_s=cycle_s,
            is_stoppage=is_stoppage,
            occupancy=self._occupancy,
        )

"""Detector tests driven by the synthetic conveyor scene.

The contract under test: whatever intervals the scene releases products at,
the detector must report as cycle times, to within the frame period.
"""

from __future__ import annotations

import pytest

from cycletime.config import DetectorConfig, Roi
from cycletime.detector import State, TripLineDetector

from make_synthetic_video import ROI, frames

FPS = 15
FRAME_PERIOD = 1.0 / FPS

DEFAULTS = DetectorConfig(
    enter_ratio=0.15, exit_ratio=0.07, diff_threshold=25,
    bg_alpha=0.02, min_present_s=0.3,
)


def run_scene(intervals, cfg=DEFAULTS, roi=None, max_valid_s=300.0, fps=FPS):
    """Feed a synthetic scene through the detector; return the events."""
    roi = roi or Roi(*ROI)
    det = TripLineDetector(cfg, roi, max_valid_s)
    events = []
    for frame, t in frames(list(intervals), fps=fps):
        ev = det.process(frame, t)
        if ev is not None:
            events.append(ev)
    return det, events


# --------------------------------------------------------------- accuracy

@pytest.mark.parametrize("intervals", [
    [8.0, 12.0, 9.0, 15.0, 11.0],
    [6.0, 6.0, 6.0],
    [5.0, 30.0, 5.0],          # the widest spread the brief calls for
])
def test_recovers_known_intervals(intervals):
    _, events = run_scene(intervals)

    # One event per product: the releases are the intervals plus the first one.
    assert len(events) == len(intervals) + 1

    # The first event has no predecessor, so it carries no interval.
    assert events[0].cycle_s is None

    measured = [ev.cycle_s for ev in events[1:]]
    for got, want in zip(measured, intervals):
        # Both edges quantise to a frame boundary, so a full frame period is
        # the worst honest error.
        assert got == pytest.approx(want, abs=FRAME_PERIOD), \
            f"expected {want}s, measured {got:.3f}s"


def test_no_double_counting_single_product():
    """One product must trip the line exactly once, never twice."""
    _, events = run_scene([])
    assert len(events) == 1


def test_empty_belt_produces_nothing():
    """A belt with no product must not generate detections from noise alone."""
    det = TripLineDetector(DEFAULTS, Roi(*ROI))
    events = []
    # frames() with no intervals still renders one product, so drive the
    # detector with the lead-in portion only.
    for frame, t in frames([], fps=FPS):
        if t > 2.5:
            break
        ev = det.process(frame, t)
        if ev is not None:
            events.append(ev)
    assert events == []
    assert det.state is State.EMPTY


# -------------------------------------------------------------- stoppages

def test_long_gap_flagged_as_stoppage():
    """A gap past max_valid_s is a stoppage, not a cycle."""
    _, events = run_scene([5.0, 30.0], max_valid_s=20.0)
    intervals = events[1:]
    assert intervals[0].cycle_s == pytest.approx(5.0, abs=FRAME_PERIOD)
    assert intervals[0].is_stoppage is False
    assert intervals[1].cycle_s == pytest.approx(30.0, abs=FRAME_PERIOD)
    assert intervals[1].is_stoppage is True


# ------------------------------------------------------------ robustness

def test_state_returns_to_empty_after_product_clears():
    det, _ = run_scene([7.0])
    assert det.state is State.EMPTY
    assert det.occupancy < DEFAULTS.exit_ratio


def test_dwell_rejects_a_brief_flicker():
    """A blip shorter than min_present_s must not count as a product.

    With a 2 s dwell the products in this scene are present for well under
    that, so nothing should trip - this is what protects against a shadow or a
    reflection flicking across the ROI.
    """
    cfg = DetectorConfig(**{**DEFAULTS.__dict__, "min_present_s": 2.0})
    _, events = run_scene([8.0, 8.0], cfg=cfg)
    assert events == []


def test_roi_change_resets_learned_state():
    det, events = run_scene([8.0])
    assert det.total_detections > 0
    det.set_roi(Roi(x=10, y=10, w=100, h=100))
    assert det.total_detections == 0
    assert det.ready is False
    assert det.state is State.EMPTY


def test_roi_outside_frame_is_clamped():
    """A stale ROI from a resolution change must not crash the detector."""
    _, events = run_scene([8.0], roi=Roi(x=600, y=440, w=400, h=400))
    assert isinstance(events, list)   # no exception is the assertion


def test_recovers_when_lighting_changes_while_occupied():
    """A scene-wide brightness jump must not wedge the detector forever.

    This is the failure the background freeze can cause: the ROI trips, the
    background stops learning, and if the whole scene then changes (webcam
    auto-exposure at startup, someone hitting the light switch) occupancy pins
    high against a background that no longer exists. Without the wedge guard
    the state machine sits OCCUPIED and never counts another product.
    """
    import numpy as np

    det = TripLineDetector(DEFAULTS, Roi(*ROI), max_valid_s=300.0, stuck_reset_s=5.0)

    dark = np.full((480, 640, 3), 40, dtype=np.uint8)
    bright = np.full((480, 640, 3), 200, dtype=np.uint8)

    det.process(dark, 0.0)                    # seeds the background dark
    for i in range(1, 40):                    # scene jumps bright and stays
        det.process(bright, i * 0.2)

    assert det.state is State.EMPTY, "detector stayed wedged after the light change"
    assert det.stuck_resets >= 1
    assert det.occupancy < DEFAULTS.enter_ratio

    # And it must still detect a real product afterwards.
    t = 8.0
    events = []
    for frame, ft in frames([], fps=FPS):
        ev = det.process(frame, t + ft)
        if ev is not None:
            events.append(ev)
    assert len(events) == 1


def test_detector_ignores_area_outside_roi():
    """Product passing outside the ROI must not trip the line.

    The ROI here sits above the belt lane the products travel in, so the scene
    is identical but nothing should be detected.
    """
    _, events = run_scene([8.0, 8.0], roi=Roi(x=10, y=10, w=120, h=60))
    assert events == []

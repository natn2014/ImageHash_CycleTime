"""Config tests — validation repair, ROI clamping, partial persistence."""

from __future__ import annotations

import json

from cycletime import config as cfgmod
from cycletime.config import Config, DetectorConfig, Roi, load, update_file, validate


# ---------------------------------------------------------------- validation

def test_exit_ratio_forced_below_enter():
    """Without a hysteresis band the state machine oscillates on noise."""
    bad = Config(detector=DetectorConfig(enter_ratio=0.1, exit_ratio=0.4))
    assert validate(bad).detector.exit_ratio < 0.1


def test_out_of_range_values_are_repaired_not_rejected():
    """A bad config must not stop the line display from booting."""
    bad = Config(detector=DetectorConfig(diff_threshold=9999, bg_alpha=5.0))
    good = validate(bad).detector
    assert good.diff_threshold == 254
    assert good.bg_alpha == 1.0


def test_missing_file_falls_back_to_defaults(tmp_path):
    cfg = load(tmp_path / "nope.json")
    assert cfg.camera.width == 640


def test_unknown_keys_are_ignored(tmp_path):
    """A config from another version must still boot."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "camera": {"index": 2, "future_option": "???"},
        "unknown_section": {"a": 1},
    }))
    assert load(path).camera.index == 2


# ------------------------------------------------------------------- ROI

def test_roi_clamped_into_frame():
    r = Roi(x=600, y=400, w=900, h=900).clamped(640, 480)
    assert (r.x, r.y, r.w, r.h) == (0, 0, 640, 480)


def test_roi_clamp_keeps_a_usable_minimum():
    r = Roi(x=10, y=10, w=1, h=1).clamped(640, 480)
    assert r.w >= 8 and r.h >= 8


# ----------------------------------------------------------- persistence

def test_update_file_writes_only_the_given_section(tmp_path):
    """Regression: a --port override must never be baked into config.json.

    Persisting the whole in-memory config would turn a one-off command-line
    flag into the permanent default.
    """
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "server": {"host": "0.0.0.0", "port": 8000},
        "roi": {"x": 1, "y": 1, "w": 10, "h": 10},
    }))

    update_file(path, roi=Roi(x=100, y=80, w=300, h=220))
    raw = json.loads(path.read_text())

    assert raw["roi"] == {"x": 100, "y": 80, "w": 300, "h": 220}
    assert raw["server"]["port"] == 8000, "unrelated section was overwritten"


def test_update_file_preserves_unknown_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"custom_note": "site A", "roi": {"x": 0, "y": 0, "w": 8, "h": 8}}))
    update_file(path, roi=Roi(x=5, y=5, w=50, h=50))
    assert json.loads(path.read_text())["custom_note"] == "site A"


def test_update_file_recovers_from_a_corrupt_file(tmp_path):
    """A truncated file from a power cut must not block a save."""
    path = tmp_path / "config.json"
    path.write_text("{ this is not json")
    update_file(path, roi=Roi(x=1, y=2, w=30, h=40))
    assert json.loads(path.read_text())["roi"]["w"] == 30


def test_update_file_leaves_no_temp_file(tmp_path):
    path = tmp_path / "config.json"
    update_file(path, detector=DetectorConfig())
    assert list(tmp_path.iterdir()) == [path]


def test_saved_roi_round_trips_through_load(tmp_path):
    path = tmp_path / "config.json"
    update_file(path, roi=Roi(x=12, y=34, w=56, h=78))
    r = load(path).roi
    assert (r.x, r.y, r.w, r.h) == (12, 34, 56, 78)

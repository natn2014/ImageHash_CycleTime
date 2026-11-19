"""Cycle target tests — the piece target is derived, never entered.

The rule these pin down is the one an operator relies on without being told:
change the target cycle time on the Setup page and the 40" board's จำนวน
target follows, with no restart and no second number to keep in step.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from cycletime import config as config_mod
from cycletime.api import create_app
from cycletime.tracker import Tracker


@pytest.fixture
def client(tmp_path):
    cfg = config_mod.Config()
    cfg = replace(cfg, store=replace(cfg.store, db_path=str(tmp_path / "cycles.db")))
    tracker = Tracker(config_mod.validate(cfg), config_path=tmp_path / "config.json")
    # The camera is never started: nothing here needs a frame.
    return TestClient(create_app(tracker))


# ------------------------------------------------------------ the formula

@pytest.mark.parametrize("target_s, pieces", [
    # Thousands: floored to the hundred. The worked example is 28800 / 14 =
    # 2057 -> 2000.
    (14.0, 2000),
    (15.0, 1900),
    (20.0, 1400),
    (7.5, 3800),
    # Hundreds: floored to the ten. A flat hundred here would turn 480 into
    # 400 and throw away 17% of an achievable target.
    (30.0, 960),
    (60.0, 480),
    (200.0, 140),
    # Below a hundred: exact. Flooring these to the hundred gave zero.
    (300.0, 96),
    (600.0, 48),
])
def test_piece_target_rounds_down_to_a_memorable_figure(client, target_s, pieces):
    r = client.post("/api/cycle", json={"target_s": target_s})
    assert r.status_code == 200
    assert r.json()["pieces"]["target"] == pieces


def test_piece_target_is_never_rounded_up(client):
    """A target above what the cycle time can deliver is not a target."""
    for target_s in [0.1, 1, 3, 7, 13.7, 14, 22, 47.5, 99, 300, 600]:
        client.post("/api/cycle", json={"target_s": target_s})
        pieces = client.get("/api/cycle").json()["pieces"]["target"]
        assert pieces <= 8 * 3600 / target_s, f"{target_s}s claimed {pieces}"


def test_no_usable_target_produces_a_zero_piece_target(client):
    """Zero pieces reads on the board as a met target, green, with nothing made.

    Flooring to a flat hundred did exactly that for any cycle time above 288 s,
    because 28800 / 289 = 99 floors to 0.
    """
    for target_s in [280, 288, 289, 300, 400, 500, 600]:
        client.post("/api/cycle", json={"target_s": target_s})
        board = client.get("/api/board").json()["quantity"]
        assert board["target"] > 0, f"{target_s}s gave a zero target"
        assert board["remaining"] > 0, f"{target_s}s reads as already met"


def test_shift_length_is_eight_hours_by_default(client):
    """The default pattern is 3 teams x 8 h."""
    assert client.get("/api/cycle").json()["pieces"]["shift_hours"] == 8.0


@pytest.mark.parametrize("pattern,hours,pieces", [
    ("3x8", 8, 2000),     # 28800 / 14 = 2057 -> 2000
    ("2x8", 8, 2000),
    ("3x12", 12, 3000),   # 43200 / 14 = 3085 -> 3000
    ("2x12", 12, 3000),
])
def test_shift_length_follows_the_selected_pattern(tmp_path, pattern, hours, pieces):
    cfg = config_mod.Config()
    cfg = replace(cfg, store=replace(cfg.store, db_path=str(tmp_path / "c.db")))
    tracker = Tracker(config_mod.validate(cfg), config_path=tmp_path / "config.json")
    tracker.update_shifts(replace(tracker.cfg.shifts, pattern=pattern))
    got = tracker.shift_piece_target()
    assert got["shift_hours"] == hours
    assert got["target"] == pieces


# --------------------------------------------------------- the wiring

def test_new_target_reaches_the_board_without_a_restart(client):
    client.post("/api/cycle", json={"target_s": 20.0})
    board = client.get("/api/board").json()
    assert board["live"]["target_s"] == 20.0
    assert board["quantity"]["target"] == 1400


def test_setup_page_and_board_report_the_same_piece_target(client):
    """One formula, two readers: the Setup preview must not drift from the wall."""
    client.post("/api/cycle", json={"target_s": 17.0})
    setup = client.get("/api/cycle").json()["pieces"]["target"]
    board = client.get("/api/board").json()["quantity"]["target"]
    assert setup == board


def test_target_is_persisted(client, tmp_path):
    """A target that only lives in memory is lost at the next power cut."""
    client.post("/api/cycle", json={"target_s": 12.5})
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["cycle"]["target_s"] == 12.5


def test_saving_a_target_leaves_the_other_cycle_settings_alone(client):
    """The Setup page posts one field; it must not blank the rest of the section."""
    before = client.get("/api/cycle").json()
    client.post("/api/cycle", json={"target_s": 9.0})
    after = client.get("/api/cycle").json()
    assert after["max_valid_s"] == before["max_valid_s"]
    assert after["diff_tolerance_s"] == before["diff_tolerance_s"]


# ------------------------------------------------------------ rejection

@pytest.mark.parametrize("bad", [0, -5, "abc"])
def test_unusable_targets_are_refused(client, bad):
    """Zero would leave the board with no piece target and a meaningless bar."""
    assert client.post("/api/cycle", json={"target_s": bad}).status_code == 400


def test_a_refused_target_changes_nothing(client):
    before = client.get("/api/cycle").json()["target_s"]
    client.post("/api/cycle", json={"target_s": 0})
    assert client.get("/api/cycle").json()["target_s"] == before

"""Roster tests — rotation, overrides, slot boundaries, midnight attribution."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from cycletime.config import Config, ShiftsConfig, validate
from cycletime.shifts import (
    Roster,
    month_bounds,
    parse_hhmm,
    week_bounds,
    week_of_month,
)

STARTS = ["08:00", "16:00", "00:00"]
TEAMS = ["A", "B", "C"]
ANCHOR = "2026-08-03"          # a Monday


def roster(**kw) -> Roster:
    opts = dict(starts=STARTS, teams=TEAMS, rotation_days=7,
                rotation_anchor=ANCHOR, rotation_direction=1)
    opts.update(kw)
    return Roster(**opts)


def d(s: str) -> date:
    return date.fromisoformat(s)


# ---------------------------------------------------------------- rotation

def test_anchor_week_reads_teams_in_order():
    r = roster()
    assert [r.scheduled_team(d(ANCHOR), s) for s in range(3)] == ["A", "B", "C"]


def test_each_week_advances_teams_one_slot():
    r = roster()
    assert [r.scheduled_team(d("2026-08-10"), s) for s in range(3)] == ["C", "A", "B"]
    assert [r.scheduled_team(d("2026-08-17"), s) for s in range(3)] == ["B", "C", "A"]


def test_rotation_returns_to_the_start_after_three_periods():
    r = roster()
    assert ([r.scheduled_team(d("2026-08-24"), s) for s in range(3)]
            == [r.scheduled_team(d(ANCHOR), s) for s in range(3)])


def test_rotation_holds_for_every_day_within_a_period():
    """The roster changes weekly, not daily."""
    r = roster()
    week = [f"2026-08-0{n}" for n in range(3, 10)]
    assert len({tuple(r.scheduled_team(d(day), s) for s in range(3)) for day in week}) == 1


def test_rotation_extends_backwards_before_the_anchor():
    """Dates before the anchor must resolve, not crash or wrap oddly."""
    r = roster()
    assert [r.scheduled_team(d("2026-07-27"), s) for s in range(3)] == ["B", "C", "A"]


def test_direction_reverses_the_rotation():
    fwd = roster(rotation_direction=1).scheduled_team(d("2026-08-10"), 0)
    rev = roster(rotation_direction=-1).scheduled_team(d("2026-08-10"), 0)
    assert fwd == "C" and rev == "B"


def test_custom_rotation_period():
    r = roster(rotation_days=14)
    assert r.scheduled_team(d("2026-08-10"), 0) == "A"   # still period 0
    assert r.scheduled_team(d("2026-08-17"), 0) == "C"   # period 1


# ------------------------------------------------------------ slot order

def test_slot_order_follows_config_not_the_clock():
    """"08:00, 16:00, 00:00" means shift 1 is the 08:00 day shift.

    Sorting by clock time would silently make the 00:00 night shift slot 0 and
    hand it to whichever team the rotation puts first.
    """
    r = roster()
    assert r.start_labels == ["08:00", "16:00", "00:00"]
    assert r.resolve(datetime(2026, 8, 3, 9, 0)).slot == 0
    assert r.resolve(datetime(2026, 8, 3, 1, 0)).slot == 2


# -------------------------------------------------------------- boundaries

def test_slot_boundary_is_half_open():
    """07:59:59 and 08:00:00 must land in different shifts."""
    r = roster()
    assert r.resolve(datetime(2026, 8, 3, 7, 59, 59)).slot == 2
    assert r.resolve(datetime(2026, 8, 3, 8, 0, 0)).slot == 0


def test_every_minute_of_a_day_resolves_to_a_shift():
    """The three slots must tile 24 hours with no gap and no overlap."""
    r = roster()
    for minute in range(0, 24 * 60, 7):
        ts = datetime(2026, 8, 3) + __import__("datetime").timedelta(minutes=minute)
        assert r.resolve(ts).slot in (0, 1, 2)


def test_timezone_aware_input_is_accepted():
    r = roster()
    aware = datetime.fromisoformat("2026-08-03T09:00:00+07:00")
    assert r.resolve(aware).slot == 0


# ------------------------------------------------------- production day

def test_no_shift_crosses_midnight_under_the_configured_times():
    """With 08/16/00 the production day equals the calendar day."""
    r = roster()
    for hour in (0, 7, 8, 15, 16, 23):
        a = r.resolve(datetime(2026, 8, 5, hour, 30))
        assert a.work_date == "2026-08-05"


def test_late_start_pattern_attributes_after_midnight_to_the_previous_day():
    """A 22:00 shift's 02:00 hours belong to the day the shift started."""
    r = roster(starts=["06:00", "14:00", "22:00"])
    a = r.resolve(datetime(2026, 8, 8, 2, 0))
    assert a.work_date == "2026-08-07"
    assert a.slot == 2


def test_late_start_pattern_evening_stays_on_its_own_day():
    r = roster(starts=["06:00", "14:00", "22:00"])
    assert r.resolve(datetime(2026, 8, 7, 23, 0)).work_date == "2026-08-07"


# --------------------------------------------------------------- overrides

def make_override_lookup(table: dict):
    def lookup(work_date, slot):
        return table.get((work_date, slot), Roster.NO_OVERRIDE)
    return lookup


def test_override_beats_the_rotation():
    r = roster(override_lookup=make_override_lookup({("2026-08-03", 0): "C"}))
    assert r.team_for(d("2026-08-03"), 0) == "C"
    assert r.scheduled_team(d("2026-08-03"), 0) == "A"    # rotation unchanged


def test_override_affects_only_its_own_slot_and_day():
    r = roster(override_lookup=make_override_lookup({("2026-08-03", 0): "C"}))
    assert r.team_for(d("2026-08-03"), 1) == "B"
    assert r.team_for(d("2026-08-04"), 0) == "A"


def test_null_override_means_nobody_not_fall_back():
    """An explicit "no team" must not silently revert to the rotation.

    Absence of a row and a stored NULL are different states: the first means
    "follow the pattern", the second means "the line is idle this shift".
    """
    r = roster(override_lookup=make_override_lookup({("2026-08-03", 1): None}))
    assert r.team_for(d("2026-08-03"), 1) is None
    assert r.scheduled_team(d("2026-08-03"), 1) == "B"


def test_resolve_carries_the_override_through():
    r = roster(override_lookup=make_override_lookup({("2026-08-03", 0): "C"}))
    assert r.resolve(datetime(2026, 8, 3, 9, 0)).team == "C"


def test_day_assignments_flag_overridden_slots():
    r = roster(override_lookup=make_override_lookup({("2026-08-03", 2): "A"}))
    slots = r.day_assignments(d("2026-08-03"))
    assert [s["overridden"] for s in slots] == [False, False, True]
    assert slots[2]["scheduled"] == "C" and slots[2]["team"] == "A"


# ---------------------------------------------------------------- calendar

def test_month_assignments_cover_every_day():
    assert len(roster().month_assignments(2026, 8)) == 31
    assert len(roster().month_assignments(2026, 2)) == 28


def test_month_assignments_handle_december_rollover():
    days = roster().month_assignments(2026, 12)
    assert len(days) == 31 and days[-1]["date"] == "2026-12-31"


def test_preview_length_and_shape():
    rows = roster().preview(d("2026-08-03"), 14)
    assert len(rows) == 14
    assert rows[0]["teams"] == ["A", "B", "C"]


# ------------------------------------------------------- week/month maths

@pytest.mark.parametrize("day,expected", [
    (1, 1), (7, 1), (8, 2), (14, 2), (15, 3), (21, 3), (22, 4), (28, 4), (31, 4),
])
def test_week_of_month_blocks(day, expected):
    assert week_of_month(date(2026, 8, day)) == expected


def test_week_four_absorbs_the_month_tail():
    """Days 29-31 must fall in week 4, not a phantom fifth column."""
    start, end = week_bounds(2026, 8, 4)
    assert start == "2026-08-22" and end == "2026-08-31"


def test_month_bounds_december():
    assert month_bounds(2026, 12) == ("2026-12-01", "2026-12-31")


def test_month_bounds_february_leap():
    assert month_bounds(2024, 2) == ("2024-02-01", "2024-02-29")


# ------------------------------------------------------------- validation

def test_bad_time_string_is_rejected():
    with pytest.raises(ValueError):
        parse_hhmm("8am")


def test_config_repairs_an_unusable_shift_pattern():
    """A typo in the Pattern tab must not stop the line display from booting."""
    bad = Config(shifts=ShiftsConfig(starts=("nope", "16:00", "00:00")))
    assert validate(bad).shifts.starts == ShiftsConfig().starts


def test_config_repairs_a_team_count_mismatch():
    bad = Config(shifts=ShiftsConfig(teams=("A", "B")))
    assert validate(bad).shifts.teams == ("A", "B", "C")


def test_config_rejects_duplicate_team_names():
    bad = Config(shifts=ShiftsConfig(teams=("A", "A", "B")))
    assert validate(bad).shifts.teams == ("A", "B", "C")


def test_config_repairs_a_bad_anchor_date():
    bad = Config(shifts=ShiftsConfig(rotation_anchor="not-a-date"))
    assert validate(bad).shifts.rotation_anchor == ShiftsConfig().rotation_anchor

"""Store tests for team attribution: migration, aggregates, overrides."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from cycletime.shifts import Roster
from cycletime.store import CycleStore


@pytest.fixture()
def store(tmp_path):
    return CycleStore(tmp_path / "team.db")


def add(store, cycle_s, team, work_date, slot=0, stoppage=False):
    return store.insert_cycle(cycle_s, stoppage, team=team,
                              work_date=work_date, slot=slot)


# ---------------------------------------------------------------- migration

def test_migration_is_idempotent(tmp_path):
    """The service restarts against an existing database on every deploy."""
    path = tmp_path / "m.db"
    CycleStore(path)
    CycleStore(path)          # must not raise "duplicate column name"
    CycleStore(path)

    with sqlite3.connect(path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cycles)")}
    assert {"team", "work_date", "slot"} <= cols


def test_migration_upgrades_a_pre_release_database(tmp_path):
    """A database written before teams existed must upgrade, not be wiped."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE cycles (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts_utc TEXT NOT NULL, cycle_s REAL NOT NULL,"
            " is_stoppage INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("INSERT INTO cycles (ts_utc, cycle_s) VALUES ('2026-08-01T00:00:00+00:00', 11.0)")

    store = CycleStore(path)
    rows = store.recent(10)

    assert len(rows) == 1, "existing data must survive the migration"
    assert rows[0]["cycle_s"] == 11.0
    assert rows[0]["team"] is None      # legacy row simply has no team


# --------------------------------------------------------------- averages

def test_averages_group_by_team(store):
    add(store, 10.0, "A", "2026-08-05")
    add(store, 12.0, "A", "2026-08-05")
    add(store, 20.0, "B", "2026-08-05")

    avgs = store.averages_by_team("2026-08-05", "2026-08-05")

    assert avgs["A"]["avg"] == 11.0 and avgs["A"]["n"] == 2
    assert avgs["B"]["avg"] == 20.0


def test_averages_exclude_stoppages(store):
    add(store, 10.0, "A", "2026-08-05")
    add(store, 600.0, "A", "2026-08-05", stoppage=True)
    assert store.averages_by_team("2026-08-05", "2026-08-05")["A"]["avg"] == 10.0


def test_averages_exclude_unattributed_rows(store):
    """A cycle with no team belongs to nobody's score."""
    add(store, 10.0, "A", "2026-08-05")
    add(store, 99.0, None, "2026-08-05")
    avgs = store.averages_by_team("2026-08-05", "2026-08-05")
    assert list(avgs) == ["A"] and avgs["A"]["avg"] == 10.0


def test_averages_respect_the_date_range(store):
    add(store, 10.0, "A", "2026-08-05")
    add(store, 30.0, "A", "2026-08-20")
    assert store.averages_by_team("2026-08-01", "2026-08-07")["A"]["avg"] == 10.0


def test_averages_use_work_date_not_timestamp(store):
    """A night shift's post-midnight cycles must score against its own day."""
    store.insert_cycle(10.0, False, ts_utc="2026-08-06T19:00:00+00:00",
                       team="C", work_date="2026-08-05", slot=2)
    assert "C" in store.averages_by_team("2026-08-05", "2026-08-05")
    assert store.averages_by_team("2026-08-06", "2026-08-06") == {}


def test_averages_empty_range_returns_nothing(store):
    assert store.averages_by_team("2026-01-01", "2026-01-31") == {}


# -------------------------------------------------------------- overrides

def test_set_and_get_override(store):
    store.set_override("2026-08-05", 1, "C")
    assert store.get_overrides("2026-08-05", "2026-08-05") == {("2026-08-05", 1): "C"}


def test_override_upserts_rather_than_duplicating(store):
    store.set_override("2026-08-05", 1, "C")
    store.set_override("2026-08-05", 1, "A")
    assert store.get_overrides("2026-08-05", "2026-08-05") == {("2026-08-05", 1): "A"}


def test_null_override_is_stored_and_distinguishable(store):
    """Stored NULL means "nobody"; a missing row means "use the rotation"."""
    store.set_override("2026-08-05", 0, None)
    overrides = store.get_overrides("2026-08-05", "2026-08-05")
    assert ("2026-08-05", 0) in overrides
    assert overrides[("2026-08-05", 0)] is None


def test_clear_override_by_slot_and_by_day(store):
    store.set_override("2026-08-05", 0, "A")
    store.set_override("2026-08-05", 1, "B")

    assert store.clear_override("2026-08-05", 0) == 1
    assert len(store.get_overrides("2026-08-05", "2026-08-05")) == 1

    assert store.clear_override("2026-08-05") == 1
    assert store.get_overrides("2026-08-05", "2026-08-05") == {}


def test_roster_reads_overrides_through_the_store(store):
    """The wiring the tracker uses: store rows must reach the roster."""
    store.set_override("2026-08-03", 0, "C")

    def lookup(work_date, slot):
        return store.get_overrides(work_date, work_date).get(
            (work_date, slot), Roster.NO_OVERRIDE)

    r = Roster(["08:00", "16:00", "00:00"], ["A", "B", "C"],
               7, "2026-08-03", 1, override_lookup=lookup)

    assert r.resolve(datetime(2026, 8, 3, 9, 0)).team == "C"     # overridden
    assert r.resolve(datetime(2026, 8, 3, 17, 0)).team == "B"    # rotation


# ------------------------------------------------------------- recompute

def test_recompute_backfills_legacy_rows(store):
    """Rows recorded before the roster existed can be attributed after the fact."""
    store.insert_cycle(10.0, False, ts_utc="2026-08-03T02:00:00+00:00")
    assert store.recent(1)[0]["team"] is None

    r = Roster(["08:00", "16:00", "00:00"], ["A", "B", "C"], 7, "2026-08-03", 1)
    updated = store.recompute_attribution(r.resolve)

    assert updated == 1
    assert store.recent(1)[0]["team"] is not None


def test_recompute_applies_a_corrected_override(store):
    """The escape hatch for a calendar entered a day late."""
    store.insert_cycle(10.0, False, ts_utc="2026-08-03T02:00:00+00:00",
                       team="A", work_date="2026-08-03", slot=0)
    store.set_override("2026-08-03", 0, "C")
    store.set_override("2026-08-03", 1, "C")
    store.set_override("2026-08-03", 2, "C")

    def lookup(work_date, slot):
        return store.get_overrides(work_date, work_date).get(
            (work_date, slot), Roster.NO_OVERRIDE)

    r = Roster(["08:00", "16:00", "00:00"], ["A", "B", "C"],
               7, "2026-08-03", 1, override_lookup=lookup)
    store.recompute_attribution(r.resolve)

    assert store.recent(1)[0]["team"] == "C"


# ------------------------------------------------------------------ csv

def test_csv_includes_team_columns(store):
    add(store, 12.5, "A", "2026-08-05", slot=1)
    lines = store.to_csv().strip().split("\n")

    assert lines[0].endswith("team,work_date,shift_slot")
    assert lines[1].endswith("A,2026-08-05,1")


def test_csv_leaves_unattributed_fields_blank(store):
    store.insert_cycle(9.0, False)
    assert store.to_csv().strip().split("\n")[1].endswith(",,,")

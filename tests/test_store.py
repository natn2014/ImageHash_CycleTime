"""Store tests — persistence, retention pruning, CSV export."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cycletime.store import CycleStore


@pytest.fixture()
def store(tmp_path):
    return CycleStore(tmp_path / "test.db")


def iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_insert_and_read_back(store):
    row = store.insert_cycle(12.345, False)
    assert row["cycle_s"] == 12.345
    assert row["is_stoppage"] is False
    assert store.count() == 1


def test_recent_returns_oldest_first_for_charting(store):
    for v in (1.0, 2.0, 3.0):
        store.insert_cycle(v, False)
    values = [r["cycle_s"] for r in store.recent(10)]
    assert values == [1.0, 2.0, 3.0]


def test_recent_limit_keeps_the_newest(store):
    for v in range(10):
        store.insert_cycle(float(v), False)
    values = [r["cycle_s"] for r in store.recent(3)]
    assert values == [7.0, 8.0, 9.0]


def test_prune_drops_only_old_rows(store):
    store.insert_cycle(10.0, False, ts_utc=iso(100))
    store.insert_cycle(11.0, False, ts_utc=iso(10))
    store.insert_cycle(12.0, False)

    removed = store.prune(retain_days=90)

    assert removed == 1
    assert store.count() == 2
    assert 10.0 not in [r["cycle_s"] for r in store.recent(10)]


def test_query_range_filters_by_timestamp(store):
    store.insert_cycle(10.0, False, ts_utc=iso(5))
    store.insert_cycle(11.0, False, ts_utc=iso(1))
    rows = store.query_range(start=iso(3))
    assert [r["cycle_s"] for r in rows] == [11.0]


def test_survives_reopen(tmp_path):
    """A power cut must not lose recorded cycles."""
    path = tmp_path / "persist.db"
    CycleStore(path).insert_cycle(9.5, False)
    assert CycleStore(path).count() == 1


def test_csv_has_header_and_local_time_column(store):
    store.insert_cycle(12.5, False)
    store.insert_cycle(400.0, True)
    lines = store.to_csv().strip().split("\n")

    assert lines[0] == (
        "id,timestamp_utc,timestamp_local,cycle_seconds,is_stoppage,"
        "team,work_date,shift_slot"
    )
    assert len(lines) == 3
    # These rows carry no team, so the three attribution columns are blank.
    assert lines[1].endswith("12.500,0,,,")
    assert lines[2].endswith("400.000,1,,,")
    # The local column is populated - engineers read shift times, not UTC.
    assert len(lines[1].split(",")[2]) > 0


def test_csv_of_empty_range_is_header_only(store):
    assert store.to_csv().strip().count("\n") == 0

"""SQLite persistence for recorded cycles.

One row per product that crosses the trip-line. Wall-clock time is stored for
humans (display, CSV, pruning); the cycle length itself comes from monotonic
deltas upstream, so an NTP correction mid-shift cannot fabricate a cycle.

The detector thread writes and HTTP handler threads read, so every operation
opens its own short-lived connection. WAL mode lets those readers run without
blocking the writer.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT    NOT NULL,
    cycle_s     REAL    NOT NULL,
    is_stoppage INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cycles_ts ON cycles(ts_utc);

-- Only *exceptions* to the rotation are stored. A row with team NULL means a
-- deliberate "nobody on this shift" (holiday, idle line) and must not fall
-- back to the rotation - which is why absence of a row and a NULL team are
-- different things here.
CREATE TABLE IF NOT EXISTS shift_overrides (
    work_date TEXT    NOT NULL,
    slot      INTEGER NOT NULL,
    team      TEXT,
    PRIMARY KEY (work_date, slot)
);
"""

# Columns added after the first release. Applied one at a time and only when
# absent, because the service restarts against an existing database on every
# deploy - an unconditional ALTER would fail on the second boot.
MIGRATIONS = [
    ("team", "ALTER TABLE cycles ADD COLUMN team TEXT"),
    ("work_date", "ALTER TABLE cycles ADD COLUMN work_date TEXT"),
    ("slot", "ALTER TABLE cycles ADD COLUMN slot INTEGER"),
]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class CycleStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add post-release columns, skipping any that already exist."""
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(cycles)")}
        for column, ddl in MIGRATIONS:
            if column not in existing:
                conn.execute(ddl)
        # Created after the columns exist, so a fresh and an upgraded database
        # end up with identical schemas.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cycles_team ON cycles(work_date, team)"
        )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL trades a fsync per commit for a tiny durability window on
            # power loss. At one write per cycle that is the right trade on an
            # SD card, whose write endurance is the real constraint here.
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------------- writes

    def insert_cycle(
        self,
        cycle_s: float,
        is_stoppage: bool,
        ts_utc: str | None = None,
        team: str | None = None,
        work_date: str | None = None,
        slot: int | None = None,
    ) -> dict:
        """Record one cycle and return the stored row as a dict.

        The team is stamped here rather than joined from the roster at read
        time, so recorded history reflects what the schedule said when the
        product was actually made. A later calendar edit cannot silently
        rewrite last month's numbers; `recompute_attribution` exists for when
        that is genuinely what you want.
        """
        ts = ts_utc or utcnow_iso()
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO cycles (ts_utc, cycle_s, is_stoppage, team, work_date, slot)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ts, float(cycle_s), 1 if is_stoppage else 0, team, work_date, slot),
            )
            row_id = cur.lastrowid
        return {
            "id": row_id,
            "ts_utc": ts,
            "cycle_s": round(float(cycle_s), 3),
            "is_stoppage": bool(is_stoppage),
            "team": team,
            "work_date": work_date,
            "slot": slot,
        }

    def prune(self, retain_days: int) -> int:
        """Delete rows older than retain_days. Returns rows removed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM cycles WHERE ts_utc < ?", (cutoff,))
            return cur.rowcount or 0

    def clear(self) -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM cycles")
            return cur.rowcount or 0

    # ---------------------------------------------------------------- reads

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict:
        keys = row.keys()
        return {
            "id": row["id"],
            "ts_utc": row["ts_utc"],
            "cycle_s": round(row["cycle_s"], 3),
            "is_stoppage": bool(row["is_stoppage"]),
            # Rows written before the migration have no team; guard on the
            # column existing so an old database still reads cleanly.
            "team": row["team"] if "team" in keys else None,
            "work_date": row["work_date"] if "work_date" in keys else None,
            "slot": row["slot"] if "slot" in keys else None,
        }

    def recent(self, limit: int = 100) -> list[dict]:
        """Most recent `limit` cycles, returned oldest-first for charting."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [self._to_dict(r) for r in reversed(rows)]

    def query_range(self, start: str | None = None, end: str | None = None) -> list[dict]:
        sql = "SELECT * FROM cycles"
        clauses, params = [], []
        if start:
            clauses.append("ts_utc >= ?")
            params.append(start)
        if end:
            clauses.append("ts_utc <= ?")
            params.append(end)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._to_dict(r) for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM cycles").fetchone()["n"]

    def count_today(self) -> int:
        """Cycles since local midnight — the number an operator asks for.

        Deliberately local, not UTC: "today" on the shop floor means the day on
        the wall clock. Unlike a since-process-start count this survives a
        service restart mid-shift.
        """
        midnight = datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return self.count_since(midnight.astimezone(timezone.utc).isoformat())

    def count_since(self, start_iso: str) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM cycles WHERE ts_utc >= ?", (start_iso,)
            ).fetchone()["n"]

    # ------------------------------------------------------------ team stats

    def averages_by_team(self, start_date: str, end_date: str) -> dict[str, dict]:
        """Mean cycle time per team over an inclusive work_date range.

        Stoppages and unattributed rows are excluded: a stoppage is not a
        cycle, and a row with no team belongs to no one's score. Returns
        {team: {"avg": float, "n": int, "min": float, "max": float}}.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT team, AVG(cycle_s) AS avg, COUNT(*) AS n,"
                "       MIN(cycle_s) AS lo, MAX(cycle_s) AS hi"
                "  FROM cycles"
                " WHERE work_date >= ? AND work_date <= ?"
                "   AND is_stoppage = 0 AND team IS NOT NULL"
                " GROUP BY team",
                (start_date, end_date),
            ).fetchall()
        return {
            r["team"]: {
                "avg": round(r["avg"], 2),
                "n": r["n"],
                "min": round(r["lo"], 2),
                "max": round(r["hi"], 2),
            }
            for r in rows
        }

    def recompute_attribution(self, resolve, start_date: str | None = None,
                              end_date: str | None = None) -> int:
        """Re-derive team/work_date/slot for stored rows. Returns rows changed.

        For when the calendar genuinely *was* wrong — a shift swap entered a
        day late, say. Deliberately an explicit action rather than a silent
        read-time join, so nobody's numbers move without someone asking.

        `resolve` takes a local datetime and returns a ShiftAssignment.
        """
        sql = "SELECT id, ts_utc FROM cycles"
        clauses, params = [], []
        if start_date:
            clauses.append("date(ts_utc) >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("date(ts_utc) <= ?")
            params.append(end_date)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        with self._write_lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            updates = []
            for r in rows:
                try:
                    local = datetime.fromisoformat(r["ts_utc"]).astimezone()
                except ValueError:
                    continue
                a = resolve(local.replace(tzinfo=None))
                updates.append((a.team, a.work_date, a.slot, r["id"]))
            conn.executemany(
                "UPDATE cycles SET team=?, work_date=?, slot=? WHERE id=?", updates
            )
            return len(updates)

    # -------------------------------------------------------------- overrides

    def get_overrides(self, start_date: str, end_date: str) -> dict[tuple[str, int], str | None]:
        """Overrides in a date range, keyed by (work_date, slot)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT work_date, slot, team FROM shift_overrides"
                " WHERE work_date >= ? AND work_date <= ?",
                (start_date, end_date),
            ).fetchall()
        return {(r["work_date"], r["slot"]): r["team"] for r in rows}

    def set_override(self, work_date: str, slot: int, team: str | None) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO shift_overrides (work_date, slot, team) VALUES (?, ?, ?)"
                " ON CONFLICT(work_date, slot) DO UPDATE SET team = excluded.team",
                (work_date, int(slot), team),
            )

    def clear_override(self, work_date: str, slot: int | None = None) -> int:
        """Delete overrides, returning the day (or one slot) to the rotation."""
        with self._write_lock, self._connect() as conn:
            if slot is None:
                cur = conn.execute(
                    "DELETE FROM shift_overrides WHERE work_date = ?", (work_date,)
                )
            else:
                cur = conn.execute(
                    "DELETE FROM shift_overrides WHERE work_date = ? AND slot = ?",
                    (work_date, int(slot)),
                )
            return cur.rowcount or 0

    # ---------------------------------------------------------------- export

    def to_csv(self, start: str | None = None, end: str | None = None) -> str:
        """Render a date range as CSV text for the dashboard's export button.

        Includes a local-time column because the people opening this in Excel
        think in shift times, not UTC.
        """
        rows = self.query_range(start, end)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow([
            "id", "timestamp_utc", "timestamp_local", "cycle_seconds", "is_stoppage",
            "team", "work_date", "shift_slot",
        ])
        for r in rows:
            try:
                local = datetime.fromisoformat(r["ts_utc"]).astimezone().isoformat(timespec="seconds")
            except ValueError:
                local = ""
            writer.writerow([
                r["id"], r["ts_utc"], local, f"{r['cycle_s']:.3f}", int(r["is_stoppage"]),
                r.get("team") or "", r.get("work_date") or "",
                "" if r.get("slot") is None else r["slot"],
            ])
        return buf.getvalue()

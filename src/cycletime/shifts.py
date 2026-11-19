"""Shift roster: which team was running at a given moment.

Pure date arithmetic — no database, no HTTP, no clock of its own. Everything it
needs arrives as arguments, which is what makes the rotation and the
midnight-boundary rules directly testable, the same way detector.py is.

The roster is **computed, not stored**. Three teams rotate through three 8-hour
slots on a fixed period, so any date's assignment falls out of arithmetic:

    period = floor((date - anchor) / rotation_days)
    team   = teams[(slot - period * direction) % 3]

Week 0 -> A morning, B afternoon, C night
Week 1 -> C morning, A afternoon, B night   (each team advances one slot)

Only *exceptions* are ever written to the database. That is what keeps the
calendar usable on a touchscreen: it auto-fills forever, and an operator taps a
day only when something unusual happens — a swap, a holiday, a team covering
two shifts.

Production day
--------------
A shift's cycles count to the day the **shift started**. With 08:00/16:00/00:00
no shift crosses midnight, so production day equals calendar day; the general
rule is implemented anyway because start times are user-editable and a
22:00-start pattern must keep working — there, a 02:00 cycle belongs to the
previous day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

SLOTS_PER_DAY = 3
SHIFT_HOURS = 24 // SLOTS_PER_DAY   # 8


@dataclass(frozen=True)
class ShiftPattern:
    """One supported crewing pattern: how many teams, how long a shift.

    `slots` is how many shifts run per day. 3 teams x 12 h runs two slots with
    one team off each day; 2 teams x 8 h runs two slots and leaves 8 h of the
    day unstaffed (timestamps there resolve to "nobody").
    """

    key: str
    label: str
    teams: int
    hours: int
    default_starts: tuple[str, ...]

    @property
    def slots(self) -> int:
        return len(self.default_starts)


PATTERNS: dict[str, ShiftPattern] = {
    p.key: p for p in (
        ShiftPattern("3x8", "3 Teams × 8 h", 3, 8, ("08:00", "16:00", "00:00")),
        ShiftPattern("3x12", "3 Teams × 12 h", 3, 12, ("08:00", "20:00")),
        ShiftPattern("2x12", "2 Teams × 12 h", 2, 12, ("08:00", "20:00")),
        ShiftPattern("2x8", "2 Teams × 8 h", 2, 8, ("08:00", "16:00")),
    )
}
DEFAULT_PATTERN = "3x8"


def get_pattern(key: str | None) -> ShiftPattern:
    """The named pattern, or the 3 x 8 default for anything unknown."""
    return PATTERNS.get(key or "", PATTERNS[DEFAULT_PATTERN])


@dataclass(frozen=True)
class ShiftAssignment:
    """Who was on, and which production day the moment belongs to."""

    work_date: str          # local YYYY-MM-DD, shift-start rule
    slot: int               # 0 | 1 | 2
    team: str | None        # None when nobody is scheduled (idle/holiday)
    slot_start: str         # "08:00" — the slot's start time, for display


def parse_hhmm(value: str) -> time:
    """Parse "HH:MM" into a time, raising a clear error for bad input."""
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"shift start must be HH:MM, got {value!r}") from exc


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def rotate_team(teams: list[str], slot: int, period: int, direction: int) -> str:
    """The team on `slot` after `period` rotations.

    Negative periods (dates before the anchor) work because Python's % always
    returns a non-negative result for a positive modulus — the roster extends
    backwards as naturally as it extends forwards.
    """
    n = len(teams)
    return teams[(slot - period * direction) % n]


class Roster:
    """Resolves timestamps and dates to team assignments.

    `override_lookup` is a callable taking (work_date_iso, slot) and returning
    either a team name, None for "explicitly nobody", or the NO_OVERRIDE
    sentinel when the day is not overridden at all. The three-way distinction
    matters: an override of None means a deliberate "line idle", which must not
    silently fall back to the rotation.
    """

    NO_OVERRIDE = object()

    def __init__(
        self,
        starts: list[str],
        teams: list[str],
        rotation_days: int = 7,
        rotation_anchor: str = "2026-08-03",
        rotation_direction: int = 1,
        override_lookup=None,
        shift_hours: int | None = None,
    ):
        # Slot index follows the order the starts are CONFIGURED in, not clock
        # order. "08:00, 16:00, 00:00" means shift 1 is the 08:00 day shift and
        # shift 3 is the 00:00 night shift, which is how the plant talks about
        # them. Sorting by clock would silently relabel the night shift as
        # shift 1 and hand it to whichever team the rotation puts on slot 0.
        self.starts = [parse_hhmm(s) for s in starts]
        self.start_labels = list(starts)
        # Default keeps the old behaviour: slots tile the day evenly.
        self.shift_hours = shift_hours or (24 // max(1, len(self.starts)))

        self.teams = list(teams)
        self.rotation_days = max(1, int(rotation_days))
        self.anchor = parse_date(rotation_anchor)
        self.direction = 1 if int(rotation_direction) >= 0 else -1
        self._override_lookup = override_lookup

    # ------------------------------------------------------------- rotation

    def period_for(self, work_date: date) -> int:
        """How many rotations have elapsed since the anchor date."""
        return (work_date - self.anchor).days // self.rotation_days

    def scheduled_team(self, work_date: date, slot: int) -> str:
        """The rotation's answer, ignoring any override."""
        return rotate_team(self.teams, slot, self.period_for(work_date), self.direction)

    def team_for(self, work_date: date, slot: int) -> str | None:
        """The effective team: an override if one exists, else the rotation."""
        if self._override_lookup is not None:
            found = self._override_lookup(work_date.isoformat(), slot)
            if found is not self.NO_OVERRIDE:
                return found          # may legitimately be None = idle
        return self.scheduled_team(work_date, slot)

    # ------------------------------------------------------------ resolution

    def slot_window(self, day: date, slot: int) -> tuple[datetime, datetime]:
        """The [start, end) wall-clock window of one slot on one day."""
        start = datetime.combine(day, self.starts[slot])
        return start, start + timedelta(hours=self.shift_hours)

    def resolve(self, ts_local: datetime) -> ShiftAssignment:
        """Which production day and slot a local timestamp belongs to.

        Days D and D-1 are both examined: under a late-start pattern a shift
        beginning on D-1 runs past midnight into D, and the shift-start rule
        says those hours still belong to D-1.
        """
        if ts_local.tzinfo is not None:
            ts_local = ts_local.replace(tzinfo=None)

        day = ts_local.date()
        for candidate in (day, day - timedelta(days=1)):
            for slot in range(len(self.starts)):
                start, end = self.slot_window(candidate, slot)
                if start <= ts_local < end:
                    return ShiftAssignment(
                        work_date=candidate.isoformat(),
                        slot=slot,
                        team=self.team_for(candidate, slot),
                        slot_start=self.start_labels[slot],
                    )

        # Off-shift hours: the 2 x 8 h pattern leaves 8 h of every day
        # unstaffed, and a hand-edited start time can leave a gap. Attribute to
        # the day with no team rather than crashing the detector thread.
        return ShiftAssignment(
            work_date=day.isoformat(), slot=-1, team=None, slot_start="",
        )

    def current(self, now: datetime | None = None) -> ShiftAssignment:
        return self.resolve(now or datetime.now())

    # ---------------------------------------------------------------- calendar

    def day_assignments(self, work_date: date) -> list[dict]:
        """All three slots for one day — what a calendar cell renders."""
        out = []
        for slot in range(len(self.starts)):
            scheduled = self.scheduled_team(work_date, slot)
            team = self.team_for(work_date, slot)
            out.append({
                "slot": slot,
                "start": self.start_labels[slot],
                "team": team,
                "scheduled": scheduled,
                "overridden": team != scheduled,
            })
        return out

    def month_assignments(self, year: int, month: int) -> list[dict]:
        """Every day in a month, for the calendar grid."""
        first, last = (parse_date(d) for d in month_bounds(year, month))
        out = []
        day = first
        while day <= last:
            out.append({"date": day.isoformat(), "slots": self.day_assignments(day)})
            day += timedelta(days=1)
        return out

    def preview(self, start: date, days: int = 14) -> list[dict]:
        """Upcoming roster — lets the anchor be set by matching reality.

        Counting weeks from an abstract anchor date is error-prone; seeing who
        lands on which shift tomorrow is not.
        """
        return [
            {"date": (start + timedelta(days=i)).isoformat(),
             "teams": [self.team_for(start + timedelta(days=i), s)
                       for s in range(len(self.starts))]}
            for i in range(days)
        ]


def week_of_month(day: date) -> int:
    """Which of the four week columns a day belongs to (1-4).

    Plain day-of-month blocks: 1-7, 8-14, 15-21, 22-end. ISO weeks would give
    five partial columns in some months and break the board's fixed four.
    """
    return min(4, (day.day - 1) // 7 + 1)


def month_bounds(year: int, month: int) -> tuple[str, str]:
    """First and last date of a month as ISO strings, inclusive."""
    first = date(year, month, 1)
    last = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def week_bounds(year: int, month: int, week: int) -> tuple[str, str]:
    """Inclusive date range of week column 1-4 within a month."""
    _, month_last = month_bounds(year, month)
    start_day = (week - 1) * 7 + 1
    start = date(year, month, start_day)
    if week == 4:
        return start.isoformat(), month_last
    return start.isoformat(), date(year, month, start_day + 6).isoformat()

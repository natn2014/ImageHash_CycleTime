"""Configuration loading, validation and persistence.

Everything tunable lives in config.json next to the project root. The ROI is the
only section the web UI writes back, so save_roi() rewrites just that key and
leaves any hand-edited comments-by-convention formatting of the rest intact.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path

# Project root is three levels up from this file: src/cycletime/config.py
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config.json"


@dataclass(frozen=True)
class CameraConfig:
    index: int = 0
    width: int = 640
    height: int = 480
    fps: int = 15
    fourcc: str = "MJPG"


@dataclass(frozen=True)
class Roi:
    x: int = 200
    y: int = 150
    w: int = 240
    h: int = 180

    def clamped(self, frame_w: int, frame_h: int) -> "Roi":
        """Fit this ROI inside a frame, guaranteeing at least an 8x8 area.

        A saved ROI can outlive a resolution change (config edited, camera
        swapped), so every consumer clamps rather than trusting the file.
        """
        w = max(8, min(int(self.w), frame_w))
        h = max(8, min(int(self.h), frame_h))
        x = max(0, min(int(self.x), frame_w - w))
        y = max(0, min(int(self.y), frame_h - h))
        return Roi(x=x, y=y, w=w, h=h)


@dataclass(frozen=True)
class DetectorConfig:
    # Occupancy fraction that trips EMPTY -> OCCUPIED. Must exceed exit_ratio;
    # the gap between them is the hysteresis band that stops state chatter.
    enter_ratio: float = 0.15
    exit_ratio: float = 0.07
    # Per-pixel absolute grey difference counted as "changed" (0-255).
    diff_threshold: int = 25
    # Running-average background blend rate, applied only while EMPTY.
    bg_alpha: float = 0.02
    # A candidate state must hold this long before the transition commits.
    min_present_s: float = 0.3


@dataclass(frozen=True)
class StationConfig:
    # Shown as the scoreboard header. One station per Pi; a second unit is
    # labelled by changing this alone.
    name: str = "LAYUP2"


@dataclass(frozen=True)
class ShiftsConfig:
    # Three 8-hour slots covering 24 h. Ordered by clock time inside Roster.
    starts: tuple[str, ...] = ("08:00", "16:00", "00:00")
    teams: tuple[str, ...] = ("A", "B", "C")
    rotation_days: int = 7
    # Any date on which the rotation reads teams[slot] straight through, i.e.
    # A on slot 0. Set it by matching the Pattern preview against reality.
    rotation_anchor: str = "2026-08-03"
    rotation_direction: int = 1


@dataclass(frozen=True)
class CycleConfig:
    # Gaps longer than this are stoppages, not cycles: recorded but excluded
    # from statistics and drawn as a break in the run chart.
    max_valid_s: float = 300.0
    target_s: float = 14.0
    # How far over target the scoreboard's diff bar stays green. A bar that is
    # always green carries no information; a bar that reddens on every 0.1 s of
    # normal variation gets ignored.
    diff_tolerance_s: float = 1.0


@dataclass(frozen=True)
class StoreConfig:
    db_path: str = "data/cycles.db"
    retain_days: int = 90

    def resolved_path(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else ROOT / p


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass(frozen=True)
class Config:
    camera: CameraConfig = CameraConfig()
    roi: Roi = Roi()
    detector: DetectorConfig = DetectorConfig()
    cycle: CycleConfig = CycleConfig()
    store: StoreConfig = StoreConfig()
    server: ServerConfig = ServerConfig()
    station: StationConfig = StationConfig()
    shifts: ShiftsConfig = ShiftsConfig()

    def to_dict(self) -> dict:
        return {
            "station": asdict(self.station),
            "camera": asdict(self.camera),
            "roi": asdict(self.roi),
            "detector": asdict(self.detector),
            "cycle": asdict(self.cycle),
            "shifts": asdict(self.shifts),
            "store": asdict(self.store),
            "server": asdict(self.server),
        }


def _section(raw: dict, key: str, cls):
    """Build a dataclass from raw[key], ignoring unknown keys.

    Tolerating unknown keys means an old config.json from a newer/older build
    still boots instead of crashing the line display at shift start.
    """
    data = raw.get(key) or {}
    if not isinstance(data, dict):
        return cls()
    fields = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in data.items() if k in fields})


def validate(cfg: Config) -> Config:
    """Repair out-of-range values rather than refusing to start."""
    det = cfg.detector
    enter = min(max(float(det.enter_ratio), 0.01), 1.0)
    exit_ = min(max(float(det.exit_ratio), 0.0), 1.0)
    if exit_ >= enter:
        # Without a hysteresis band the state machine oscillates on noise.
        exit_ = enter * 0.5
    det = replace(
        det,
        enter_ratio=enter,
        exit_ratio=exit_,
        diff_threshold=min(max(int(det.diff_threshold), 1), 254),
        bg_alpha=min(max(float(det.bg_alpha), 0.0), 1.0),
        min_present_s=max(float(det.min_present_s), 0.0),
    )
    cam = replace(
        cfg.camera,
        width=max(int(cfg.camera.width), 64),
        height=max(int(cfg.camera.height), 64),
        fps=min(max(int(cfg.camera.fps), 1), 120),
    )
    cyc = replace(
        cfg.cycle,
        max_valid_s=max(float(cfg.cycle.max_valid_s), 1.0),
        target_s=max(float(cfg.cycle.target_s), 0.0),
        diff_tolerance_s=max(float(cfg.cycle.diff_tolerance_s), 0.0),
    )
    store = replace(cfg.store, retain_days=max(int(cfg.store.retain_days), 1))
    return replace(cfg, camera=cam, detector=det, cycle=cyc,
                   store=store, shifts=_validate_shifts(cfg.shifts))


def _validate_shifts(sh: ShiftsConfig) -> ShiftsConfig:
    """Repair a shift config rather than letting it stop the line display.

    A bad time string or a mismatched team count is a typo in the Pattern tab,
    not a reason to refuse to boot — fall back to the defaults for whichever
    part is unusable and keep running.
    """
    from .shifts import parse_date, parse_hhmm   # local: avoids a cycle

    defaults = ShiftsConfig()

    starts = tuple(sh.starts or ())
    try:
        if len(starts) != 3:
            raise ValueError("need exactly three shift starts")
        for s in starts:
            parse_hhmm(s)
    except (ValueError, TypeError):
        starts = defaults.starts

    teams = tuple(str(t).strip() for t in (sh.teams or ()) if str(t).strip())
    # One team per slot: fewer would leave a shift unstaffed by construction,
    # more would never reach the rota.
    if len(teams) != len(starts) or len(set(teams)) != len(teams):
        teams = defaults.teams

    anchor = sh.rotation_anchor
    try:
        parse_date(anchor)
    except (ValueError, TypeError):
        anchor = defaults.rotation_anchor

    return replace(
        sh,
        starts=starts,
        teams=teams,
        rotation_days=max(1, int(sh.rotation_days or 1)),
        rotation_anchor=anchor,
        rotation_direction=1 if int(sh.rotation_direction or 1) >= 0 else -1,
    )


def load(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """Load config.json, falling back to defaults for anything missing."""
    path = Path(path)
    if not path.exists():
        return validate(Config())
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    cfg = Config(
        camera=_section(raw, "camera", CameraConfig),
        roi=_section(raw, "roi", Roi),
        detector=_section(raw, "detector", DetectorConfig),
        cycle=_section(raw, "cycle", CycleConfig),
        store=_section(raw, "store", StoreConfig),
        server=_section(raw, "server", ServerConfig),
        station=_section(raw, "station", StationConfig),
        shifts=_section(raw, "shifts", ShiftsConfig),
    )
    return validate(cfg)


_save_lock = threading.Lock()


def update_file(path: Path | str = DEFAULT_CONFIG_PATH, **sections) -> None:
    """Merge the given sections into config.json, leaving the rest untouched.

    Only the sections the UI can actually change (`roi`, `detector`) are ever
    written. Dumping the whole in-memory config instead would bake command-line
    overrides such as --port and --camera permanently into the file, so a
    one-off `--port 8147` for testing would silently become the new default.
    Merging also preserves any key a future version adds that this one does not
    know about.

    The write is atomic: a factory Pi loses power without warning, and a
    half-written config.json would stop the tracker booting at the next shift.
    """
    path = Path(path)
    with _save_lock:
        raw: dict = {}
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (json.JSONDecodeError, OSError):
                raw = {}   # unreadable file: rebuild from what we're given

        for key, value in sections.items():
            raw[key] = asdict(value) if is_dataclass(value) else value

        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2)
            fh.write("\n")
            fh.flush()
        tmp.replace(path)

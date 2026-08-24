"""HTTP layer: dashboard, ROI setup, live stats, SSE push and CSV export."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import cv2
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .config import ROOT, DetectorConfig, Roi, ShiftsConfig
from .stats import histogram, summarize
from .tracker import Tracker

log = logging.getLogger(__name__)

WEB_DIR = ROOT / "web"

# Preview stream cap. The setup page only needs enough motion to aim a box;
# encoding every frame to JPEG would burn CPU the detector wants.
PREVIEW_FPS = 10
PREVIEW_JPEG_QUALITY = 70


def create_app(tracker: Tracker) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The detector thread pushes SSE messages into this loop.
        tracker.bind_loop(asyncio.get_running_loop())
        yield

    app = FastAPI(title="Cycle Time Tracker", lifespan=lifespan)
    app.state.tracker = tracker

    # THE GOTCHA THAT SURVIVES A REBOOT: nothing served here is worth caching —
    # it is either the UI itself or live numbers — and the panel runs Chromium
    # against a --user-data-dir whose disk cache outlives the power cycle. With
    # no Cache-Control the browser is free to invent its own freshness window
    # from Last-Modified, so an updated page keeps rendering the OLD markup and
    # --kiosk has no reload button to break out of it. Symptom: you edit web/,
    # restart, reboot, and the panel still shows the previous UI.
    #
    # `no-cache` is "store it, but ask me every time" — not "don't store it".
    # The files still carry ETag/Last-Modified, so a revalidation over loopback
    # costs one 304 and no body.
    @app.middleware("http")
    async def always_revalidate(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # ------------------------------------------------------------- pages

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/setup", include_in_schema=False)
    async def setup_page():
        return FileResponse(WEB_DIR / "setup.html")

    @app.get("/board", include_in_schema=False)
    async def board_page():
        """The 40" scoreboard. Separate page, separate CSS, its own scale."""
        return FileResponse(WEB_DIR / "board.html")

    @app.get("/shifts", include_in_schema=False)
    async def shifts_page():
        return FileResponse(WEB_DIR / "shifts.html")

    # ------------------------------------------------------------- board

    @app.get("/api/board")
    async def api_board():
        return tracker.board()

    # ------------------------------------------------------------ shifts

    @app.get("/api/shifts/config")
    async def api_get_shifts():
        s = tracker.cfg.shifts
        return {
            "starts": list(s.starts), "teams": list(s.teams),
            "rotation_days": s.rotation_days,
            "rotation_anchor": s.rotation_anchor,
            "rotation_direction": s.rotation_direction,
            "station": tracker.cfg.station.name,
        }

    @app.post("/api/shifts/config")
    async def api_set_shifts(payload: dict):
        current = tracker.cfg.shifts
        fields = ShiftsConfig.__dataclass_fields__
        merged = {f: getattr(current, f) for f in fields}
        for key, value in payload.items():
            if key in merged:
                merged[key] = tuple(value) if isinstance(value, list) else value
        try:
            applied = tracker.update_shifts(ShiftsConfig(**merged))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "starts": list(applied.starts), "teams": list(applied.teams),
            "rotation_days": applied.rotation_days,
            "rotation_anchor": applied.rotation_anchor,
            "rotation_direction": applied.rotation_direction,
        }

    @app.get("/api/shifts/current")
    async def api_current_shift():
        a = tracker.roster.current()
        return {"team": a.team, "slot": a.slot,
                "work_date": a.work_date, "slot_start": a.slot_start}

    @app.get("/api/shifts/month")
    async def api_shift_month(
        year: int = Query(..., ge=1970, le=2999),
        month: int = Query(..., ge=1, le=12),
    ):
        """Resolved roster for a whole month — what the calendar grid draws."""
        return {
            "year": year, "month": month,
            "teams": list(tracker.cfg.shifts.teams),
            "starts": list(tracker.cfg.shifts.starts),
            "days": tracker.roster.month_assignments(year, month),
        }

    @app.get("/api/shifts/preview")
    async def api_shift_preview(days: int = Query(14, ge=1, le=60)):
        """Upcoming roster, so the anchor can be set by matching reality."""
        return {"days": tracker.roster.preview(date.today(), days)}

    @app.post("/api/shifts/override")
    async def api_set_override(payload: dict):
        """Pin a team to one shift on one day, overriding the rotation.

        A null team is meaningful and preserved: it records "nobody on this
        shift" rather than falling back to the rotation.
        """
        try:
            work_date = str(payload["date"])
            slot = int(payload["slot"])
            datetime.strptime(work_date, "%Y-%m-%d")
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="expected date=YYYY-MM-DD and integer slot")

        team = payload.get("team")
        if team is not None:
            team = str(team)
            if team not in tracker.cfg.shifts.teams:
                raise HTTPException(status_code=400, detail=f"unknown team {team!r}")
        if not 0 <= slot < len(tracker.cfg.shifts.starts):
            raise HTTPException(status_code=400, detail="slot out of range")

        tracker.store.set_override(work_date, slot, team)
        return {"date": work_date, "slots": tracker.roster.day_assignments(
            datetime.strptime(work_date, "%Y-%m-%d").date())}

    @app.delete("/api/shifts/override")
    async def api_clear_override(date_: str = Query(..., alias="date"), slot: int | None = None):
        """Return a day (or one shift) to the automatic rotation."""
        try:
            parsed = datetime.strptime(date_, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="expected date=YYYY-MM-DD")
        removed = tracker.store.clear_override(date_, slot)
        return {"removed": removed, "date": date_,
                "slots": tracker.roster.day_assignments(parsed)}

    @app.post("/api/shifts/recompute")
    async def api_recompute(payload: dict | None = None):
        """Re-stamp stored cycles from the current roster.

        For when the calendar genuinely was wrong. Explicit on purpose — no
        one's historical numbers should move as a side effect of an edit.
        """
        payload = payload or {}
        changed = tracker.store.recompute_attribution(
            tracker.roster.resolve, payload.get("start"), payload.get("end")
        )
        return {"updated": changed}

    # -------------------------------------------------------------- data

    @app.get("/api/status")
    async def api_status():
        return tracker.status()

    @app.get("/api/cycles")
    async def api_cycles(limit: int = Query(100, ge=1, le=5000)):
        return {"cycles": tracker.store.recent(limit)}

    @app.get("/api/stats")
    async def api_stats(limit: int = Query(100, ge=2, le=5000), bins: int = Query(12, ge=2, le=40)):
        """Everything the dashboard needs for one repaint, in a single trip."""
        cycles = tracker.store.recent(limit)
        return {
            "cycles": cycles,
            "summary": summarize(cycles),
            "histogram": histogram(cycles, bins),
            "status": tracker.status(),
        }

    # --------------------------------------------------------------- SSE

    @app.get("/api/events")
    async def api_events(request: Request):
        """Server-sent events: one message per detected cycle.

        Push beats polling here - the chart gains its point the instant the
        product passes, and an idle dashboard costs nothing.
        """

        async def stream():
            queue = tracker.subscribe()
            try:
                yield "retry: 3000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # Comment frame: keeps proxies and the browser from
                        # dropping a connection that is merely idle.
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(payload)}\n\n"
            finally:
                tracker.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --------------------------------------------------------- ROI config

    @app.get("/api/roi")
    async def api_get_roi():
        r = tracker.cfg.roi
        return {"x": r.x, "y": r.y, "w": r.w, "h": r.h}

    @app.post("/api/roi")
    async def api_set_roi(payload: dict):
        try:
            roi = Roi(
                x=int(payload["x"]), y=int(payload["y"]),
                w=int(payload["w"]), h=int(payload["h"]),
            )
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="expected integer x, y, w, h")
        if roi.w < 8 or roi.h < 8:
            raise HTTPException(status_code=400, detail="ROI must be at least 8x8 pixels")
        applied = tracker.update_roi(roi)
        return {"x": applied.x, "y": applied.y, "w": applied.w, "h": applied.h}

    @app.get("/api/detector")
    async def api_get_detector():
        d = tracker.cfg.detector
        return {
            "enter_ratio": d.enter_ratio, "exit_ratio": d.exit_ratio,
            "diff_threshold": d.diff_threshold, "bg_alpha": d.bg_alpha,
            "min_present_s": d.min_present_s,
        }

    @app.post("/api/detector")
    async def api_set_detector(payload: dict):
        current = tracker.cfg.detector
        fields = DetectorConfig.__dataclass_fields__
        merged = {f: getattr(current, f) for f in fields}
        for key, value in payload.items():
            if key in merged:
                merged[key] = value
        try:
            tracker.update_detector(DetectorConfig(**merged))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return await api_get_detector()

    # ------------------------------------------------------------ preview

    def _encode_preview(frame, roi: Roi, state: str, occupancy: float) -> bytes | None:
        """Draw the ROI overlay and JPEG-encode one frame."""
        h, w = frame.shape[:2]
        r = roi.clamped(w, h)
        img = frame.copy()
        # Green while the belt reads empty, amber while a product occupies the
        # line - the operator can confirm the box is aimed right at a glance.
        colour = (0, 200, 0) if state == "empty" else (0, 170, 255)
        cv2.rectangle(img, (r.x, r.y), (r.x + r.w, r.y + r.h), colour, 2)
        cv2.putText(
            img, f"{state}  occ={occupancy:.0%}", (r.x, max(18, r.y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1, cv2.LINE_AA,
        )
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), PREVIEW_JPEG_QUALITY])
        return buf.tobytes() if ok else None

    @app.get("/api/preview.mjpg")
    async def api_preview(request: Request):
        """MJPEG stream for the setup page only.

        Deliberately not used by the dashboard: JPEG-encoding every frame all
        shift long would cost far more CPU than the detection itself.
        """

        async def stream():
            interval = 1.0 / PREVIEW_FPS
            last_seq = -1
            while True:
                if await request.is_disconnected():
                    break
                frame, _, seq = tracker.camera.latest()
                if frame is None or seq == last_seq:
                    await asyncio.sleep(interval)
                    continue
                last_seq = seq
                st = tracker.detector
                # Encoding is CPU-bound; run it off the event loop so SSE and
                # API calls stay responsive while the setup page is open.
                jpeg = await asyncio.to_thread(
                    _encode_preview, frame, tracker.cfg.roi, st.state.value, st.occupancy
                )
                if jpeg:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n"
                    yield f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                    yield jpeg + b"\r\n"
                await asyncio.sleep(interval)

        return StreamingResponse(
            stream(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/snapshot.jpg")
    async def api_snapshot():
        """Single still - used by the setup page to size its drag canvas."""
        frame, _, _ = tracker.camera.latest()
        if frame is None:
            raise HTTPException(status_code=503, detail="no frame available yet")
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            raise HTTPException(status_code=500, detail="encode failed")
        return Response(content=buf.tobytes(), media_type="image/jpeg")

    # ------------------------------------------------------------- export

    @app.get("/api/export.csv")
    async def api_export(start: str | None = None, end: str | None = None):
        csv_text = tracker.store.to_csv(start, end)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        return PlainTextResponse(
            csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="cycles_{stamp}.csv"'},
        )

    @app.get("/api/health")
    async def api_health():
        ok = tracker.camera.connected
        return JSONResponse(
            {"ok": ok, "camera": tracker.camera.connected, "uptime_s": round(time.monotonic(), 1)},
            status_code=200 if ok else 503,
        )

    # Mounted last so the explicit routes above always win.
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app

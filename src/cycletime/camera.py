"""Threaded USB camera capture.

The capture thread exists for one reason: measurement accuracy. A UVC webcam
keeps a driver-side frame queue. If you call read() at processing speed you
consume that queue's *oldest* frame every time, so the lag between reality and
the frame you are timing grows without bound and your cycle timestamps drift.

This thread instead runs flat out, always holding only the newest frame, and
stamps it with time.monotonic() at the moment of grab. Consumers take whatever
is current. Monotonic time is used because it cannot jump: an NTP step mid-shift
would otherwise inject a fabricated cycle into the data.
"""

from __future__ import annotations

import logging
import threading
import time

import cv2
import numpy as np

from .config import CameraConfig

log = logging.getLogger(__name__)

# Wait between reconnect attempts after the camera disappears (unplugged cable,
# USB brown-out). Long enough not to spin the CPU, short enough that the line
# display recovers on its own without anyone being called out.
RECONNECT_DELAY_S = 2.0


class CaptureThread:
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self._cap: cv2.VideoCapture | None = None
        self._frame: np.ndarray | None = None
        self._frame_t: float = 0.0
        self._seq: int = 0
        self._lock = threading.Lock()
        # Signalled on every new frame so consumers can block instead of poll.
        self._new_frame = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.frames_captured = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> "CaptureThread":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._release()

    def _release(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        self.connected = False

    # --------------------------------------------------------------- capture

    def _open(self) -> bool:
        self._release()
        cfg = self.cfg
        # CAP_DSHOW on Windows avoids the ~2 s MSMF startup stall; V4L2 is the
        # right backend on the Pi. ANY lets OpenCV decide elsewhere.
        backends = [cv2.CAP_DSHOW, cv2.CAP_ANY] if hasattr(cv2, "CAP_DSHOW") else [cv2.CAP_ANY]
        if hasattr(cv2, "CAP_V4L2"):
            backends.insert(0, cv2.CAP_V4L2)

        for backend in backends:
            try:
                cap = cv2.VideoCapture(cfg.index, backend)
            except Exception as exc:  # pragma: no cover - backend missing
                log.debug("backend %s unavailable: %s", backend, exc)
                continue
            if not cap.isOpened():
                cap.release()
                continue

            # Order matters: set the pixel format before the resolution, or the
            # driver may renegotiate back to YUYV. Without MJPG many webcams
            # silently fall to ~5 fps at 640x480 on USB bandwidth limits.
            if cfg.fourcc:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*cfg.fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
            cap.set(cv2.CAP_PROP_FPS, cfg.fps)
            # Ask the driver to keep the shallowest queue it will allow. Not all
            # backends honour this, which is why the drain loop exists anyway.
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            ok, _ = cap.read()
            if not ok:
                cap.release()
                continue

            self._cap = cap
            self.connected = True
            self.last_error = None
            log.info(
                "camera %s opened: %dx%d @ %.0f fps (backend %s)",
                cfg.index,
                cap.get(cv2.CAP_PROP_FRAME_WIDTH),
                cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
                cap.get(cv2.CAP_PROP_FPS),
                backend,
            )
            return True

        self.last_error = f"cannot open camera index {cfg.index}"
        log.warning(self.last_error)
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._cap is None and not self._open():
                self._stop.wait(RECONNECT_DELAY_S)
                continue

            ok, frame = self._cap.read()
            t = time.monotonic()

            if not ok or frame is None:
                log.warning("camera read failed; reconnecting")
                self.last_error = "read failed"
                self._release()
                self._stop.wait(RECONNECT_DELAY_S)
                continue

            with self._new_frame:
                self._frame = frame
                self._frame_t = t
                self._seq += 1
                self.frames_captured += 1
                self._new_frame.notify_all()

        self._release()

    # -------------------------------------------------------------- consumers

    def latest(self) -> tuple[np.ndarray | None, float, int]:
        """Current frame, its capture time, and its sequence number."""
        with self._lock:
            if self._frame is None:
                return None, 0.0, 0
            return self._frame, self._frame_t, self._seq

    def wait_for_frame(self, last_seq: int, timeout: float = 1.0):
        """Block until a frame newer than `last_seq` arrives.

        Lets the detector and the MJPEG preview run at true camera rate without
        a polling sleep, and without either processing the same frame twice.
        """
        with self._new_frame:
            if self._seq == last_seq:
                self._new_frame.wait(timeout)
            if self._frame is None or self._seq == last_seq:
                return None, 0.0, last_seq
            return self._frame, self._frame_t, self._seq

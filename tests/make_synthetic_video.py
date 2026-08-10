"""Synthetic conveyor scene with products at exactly known intervals.

This is the test bench. It renders a grey belt with sensor noise and slides
dark products across the ROI at a fixed speed, released at intervals you
specify. Because every product travels identically, the delay between release
and trip is a constant that cancels out of the interval — so whatever the
detector reports for the gaps must equal the intervals asked for, and any
discrepancy is a real bug rather than an artefact of the scene.

Used two ways:

  * `frames()` yields (frame, t_mono) pairs for the test suite. No codec, no
    file, fully deterministic.
  * the CLI writes an MP4 you can watch, for confirming the scene looks like a
    conveyor before trusting a test that passes.

    python tests/make_synthetic_video.py --intervals 8,12,9,15,11 --out belt.mp4
"""

from __future__ import annotations

import argparse

import cv2
import numpy as np

FRAME_W, FRAME_H = 640, 480
ROI = (200, 150, 240, 180)          # x, y, w, h — matches the shipped default
PRODUCT_W, PRODUCT_H = 120, 140
SPEED_PX_S = 300.0                  # constant belt speed
BELT_GREY = 130
PRODUCT_GREY = 40
NOISE_SIGMA = 3.0                   # webcam sensor noise
LEAD_IN_S = 3.0                     # empty belt first, so the background settles


def _belt(rng: np.random.Generator) -> np.ndarray:
    """One empty-belt frame: flat grey plus gaussian sensor noise."""
    frame = np.full((FRAME_H, FRAME_W, 3), BELT_GREY, dtype=np.uint8)
    noise = rng.normal(0, NOISE_SIGMA, (FRAME_H, FRAME_W, 1))
    frame = np.clip(frame.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    # A static belt edge: a real scene is never uniform, and a detector that
    # only works on a blank field would be a false pass.
    cv2.line(frame, (0, 90), (FRAME_W, 90), (95, 95, 95), 3)
    cv2.line(frame, (0, 400), (FRAME_W, 400), (95, 95, 95), 3)
    return frame


def release_times(intervals: list[float]) -> list[float]:
    """Absolute release times: the first product, then each gap in turn."""
    times = [LEAD_IN_S]
    for gap in intervals:
        times.append(times[-1] + gap)
    return times


def frames(intervals: list[float], fps: int = 15, seed: int = 7, tail_s: float = 3.0):
    """Yield (bgr_frame, t_mono) for a belt running the given intervals."""
    rng = np.random.default_rng(seed)
    releases = release_times(intervals)
    # Run until the last product has fully cleared the frame.
    duration = releases[-1] + (FRAME_W + PRODUCT_W) / SPEED_PX_S + tail_s
    y = ROI[1] + (ROI[3] - PRODUCT_H) // 2

    for i in range(int(duration * fps)):
        t = i / fps
        frame = _belt(rng)
        for start in releases:
            if t < start:
                continue
            x = int(-PRODUCT_W + SPEED_PX_S * (t - start))
            if x > FRAME_W:
                continue
            x0, x1 = max(0, x), min(FRAME_W, x + PRODUCT_W)
            if x1 > x0:
                cv2.rectangle(frame, (x0, y), (x1, y + PRODUCT_H), (PRODUCT_GREY,) * 3, -1)
        yield frame, t


def main() -> int:
    p = argparse.ArgumentParser(description="Render a synthetic conveyor clip")
    p.add_argument("--intervals", default="8,12,9,15,11",
                   help="comma-separated seconds between products")
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--out", default="tests/fixture.mp4")
    args = p.parse_args()

    intervals = [float(v) for v in args.intervals.split(",") if v.strip()]
    writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"),
                             args.fps, (FRAME_W, FRAME_H))
    if not writer.isOpened():
        print(f"could not open {args.out} for writing")
        return 1

    count = 0
    for frame, _ in frames(intervals, args.fps):
        writer.write(frame)
        count += 1
    writer.release()
    print(f"wrote {args.out}: {count} frames, {len(intervals) + 1} products, "
          f"intervals {intervals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

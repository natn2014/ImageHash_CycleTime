"""Statistics for the run chart and histogram.

Control limits use the I-MR (individuals & moving-range) method rather than the
raw standard deviation. On a conveyor, the occasional long cycle is exactly the
signal you are hunting; feeding those outliers into a raw sigma inflates the
limits until nothing ever trips. The moving range only sees *consecutive*
differences, so a single long cycle widens the estimate far less. This is the
standard SPC treatment for one-measurement-at-a-time processes.
"""

from __future__ import annotations

import math

# Unbiasing constant d2 for a moving range of n=2 (consecutive pairs).
# sigma_hat = MRbar / d2 is the textbook individuals-chart estimator.
D2_N2 = 1.128


def _finite(values) -> list[float]:
    return [float(v) for v in values if v is not None and math.isfinite(float(v))]


def summarize(cycles: list[dict]) -> dict:
    """Compute run-chart statistics from cycle rows.

    Rows flagged is_stoppage are dropped first: a lunch break is not a cycle,
    and leaving it in would drag the mean and blow out the chart's y-axis.
    """
    values = _finite([c["cycle_s"] for c in cycles if not c.get("is_stoppage")])
    n = len(values)
    stoppages = sum(1 for c in cycles if c.get("is_stoppage"))

    if n == 0:
        return {
            "n": 0, "stoppages": stoppages, "mean": None, "median": None,
            "min": None, "max": None, "sigma": None, "cv_pct": None,
            "ucl": None, "lcl": None, "out_of_control": 0,
        }

    mean = sum(values) / n
    ordered = sorted(values)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2

    if n >= 2:
        moving_ranges = [abs(values[i] - values[i - 1]) for i in range(1, n)]
        sigma = (sum(moving_ranges) / len(moving_ranges)) / D2_N2
    else:
        sigma = 0.0

    ucl = mean + 3 * sigma
    # A cycle time cannot be negative, so the lower limit floors at zero rather
    # than drawing a meaningless line below the axis.
    lcl = max(0.0, mean - 3 * sigma)
    out_of_control = sum(1 for v in values if v > ucl or v < lcl)

    return {
        "n": n,
        "stoppages": stoppages,
        "mean": round(mean, 2),
        "median": round(median, 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "sigma": round(sigma, 3),
        # Coefficient of variation is the single best "how much does it
        # fluctuate?" number: it is scale-free, so it stays comparable when the
        # line changes product or speed.
        "cv_pct": round(100 * sigma / mean, 1) if mean > 0 else None,
        "ucl": round(ucl, 2),
        "lcl": round(lcl, 2),
        "out_of_control": out_of_control,
    }


def histogram(cycles: list[dict], bins: int = 12) -> dict:
    """Bucket cycle times for the distribution chart.

    Bins are computed over the observed range; a bimodal shape here is the
    clearest evidence that the line is really running two different processes
    (e.g. an operator intervention every Nth piece).
    """
    values = _finite([c["cycle_s"] for c in cycles if not c.get("is_stoppage")])
    if not values:
        return {"bins": [], "counts": [], "labels": []}

    lo, hi = min(values), max(values)
    if math.isclose(lo, hi):
        # All identical: render a single centred bar instead of a zero-width bin.
        return {
            "bins": [lo],
            "counts": [len(values)],
            "labels": [f"{lo:.1f}"],
        }

    bins = max(2, int(bins))
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        idx = int((v - lo) / width)
        if idx >= bins:  # the maximum value lands exactly on the top edge
            idx = bins - 1
        counts[idx] += 1

    edges = [lo + i * width for i in range(bins + 1)]
    labels = [f"{edges[i]:.1f}" for i in range(bins)]
    return {"bins": edges, "counts": counts, "labels": labels}

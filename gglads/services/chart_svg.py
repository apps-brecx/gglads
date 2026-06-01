"""Tiny hand-rolled SVG chart helpers — no JS, no client deps.

Two shapes:
  line_chart() — multi-series line on a fixed viewBox; returns dict for the
    template to render (paths, axis ticks, max/min labels, x-axis labels).
  sparkline() — single-series mini path string with min/max points.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Sequence


def _series_max(series: Sequence[Sequence[float]]) -> float:
    m = 0.0
    for s in series:
        for v in s:
            if v > m:
                m = v
    return m or 1.0  # avoid div-by-zero; chart shows a flat line at 0


def line_chart(
    days: list[date],
    series: dict[str, list[float]],
    *,
    width: int = 800,
    height: int = 220,
    pad_left: int = 44,
    pad_right: int = 12,
    pad_top: int = 12,
    pad_bottom: int = 28,
) -> dict:
    """Return a dict the template uses to render an SVG line chart.

    `series` is {label: [value per day]}, all the same length as `days`.
    """
    n = len(days)
    if n == 0 or not series:
        return {"empty": True, "width": width, "height": height}

    inner_w = width - pad_left - pad_right
    inner_h = height - pad_top - pad_bottom
    ymax = _series_max([s for s in series.values()])
    # Round ymax up to a nice 1/2/5 × 10^k tick.
    nice = _nice_ceiling(ymax)
    if nice <= 0:
        nice = 1.0

    def x_of(i: int) -> float:
        if n == 1:
            return pad_left + inner_w / 2
        return pad_left + (i / (n - 1)) * inner_w

    def y_of(v: float) -> float:
        return pad_top + inner_h - (v / nice) * inner_h

    paths: list[dict] = []
    for label, values in series.items():
        pts: list[str] = []
        last_pt: tuple[float, float] | None = None
        for i, v in enumerate(values):
            x = x_of(i)
            y = y_of(float(v))
            pts.append(("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}")
            last_pt = (x, y)
        # Smooth area-fill underneath uses a closed path back to baseline.
        area_pts = list(pts) + [
            f"L{pad_left + inner_w:.1f},{pad_top + inner_h:.1f}",
            f"L{pad_left:.1f},{pad_top + inner_h:.1f}",
            "Z",
        ]
        paths.append({
            "label": label,
            "d": " ".join(pts),
            "area_d": " ".join(area_pts),
            "last_pt": last_pt,
            "last_value": values[-1] if values else 0,
        })

    # 4 y-axis ticks at 0, 25%, 50%, 75%, 100% of nice.
    y_ticks = []
    for i in range(5):
        v = nice * (i / 4)
        y_ticks.append({"value": v, "y": y_of(v)})

    # Up to 6 x-axis labels evenly spaced.
    label_indices: list[int]
    if n <= 6:
        label_indices = list(range(n))
    else:
        step = (n - 1) / 5
        label_indices = sorted({int(round(step * k)) for k in range(6)})
    x_labels = [
        {"x": x_of(i), "text": days[i].strftime("%b %d")}
        for i in label_indices
    ]

    return {
        "empty": False,
        "width": width,
        "height": height,
        "pad_left": pad_left,
        "pad_right": pad_right,
        "pad_top": pad_top,
        "pad_bottom": pad_bottom,
        "inner_w": inner_w,
        "inner_h": inner_h,
        "ymax": nice,
        "paths": paths,
        "y_ticks": y_ticks,
        "x_labels": x_labels,
        "baseline_y": pad_top + inner_h,
    }


def _nice_ceiling(v: float) -> float:
    """Round v up to a 1/2/5 × 10^k tick for nice axis labels."""
    if v <= 0:
        return 1.0
    import math
    exp = math.floor(math.log10(v))
    base = 10 ** exp
    frac = v / base
    if frac <= 1:
        nice_frac = 1
    elif frac <= 2:
        nice_frac = 2
    elif frac <= 5:
        nice_frac = 5
    else:
        nice_frac = 10
    return nice_frac * base


def sparkline(
    values: list[Decimal | float], *, width: int = 80, height: int = 22
) -> dict:
    """Mini path for a tiny inline chart."""
    floats = [float(v) for v in values]
    n = len(floats)
    if n == 0:
        return {"empty": True, "width": width, "height": height}
    vmax = max(floats) or 1.0
    vmin = min(floats)
    span = (vmax - vmin) or 1.0

    def x_of(i: int) -> float:
        if n == 1:
            return width / 2
        return (i / (n - 1)) * (width - 2) + 1

    def y_of(v: float) -> float:
        return height - 1 - ((v - vmin) / span) * (height - 2)

    pts = []
    for i, v in enumerate(floats):
        pts.append(("M" if i == 0 else "L") + f"{x_of(i):.1f},{y_of(v):.1f}")
    last = (x_of(n - 1), y_of(floats[-1]))
    return {
        "empty": False,
        "width": width,
        "height": height,
        "d": " ".join(pts),
        "last_x": last[0],
        "last_y": last[1],
        "last_value": floats[-1],
    }


def donut(slices: list[dict], *, size: int = 140, stroke: int = 22) -> dict:
    """slices: [{label, value, color}] — returns SVG arc segments."""
    total = sum(float(s.get("value", 0)) for s in slices) or 1.0
    radius = (size - stroke) / 2
    cx = size / 2
    cy = size / 2
    circ = 2 * 3.141592653589793 * radius
    out_slices = []
    offset = 0.0
    for s in slices:
        frac = float(s.get("value", 0)) / total
        length = circ * frac
        out_slices.append({
            "label": s["label"],
            "value": s.get("value", 0),
            "color": s.get("color", "#7c9cff"),
            "stroke_dasharray": f"{length:.2f} {circ - length:.2f}",
            "stroke_dashoffset": f"{-offset:.2f}",
            "percent": frac * 100,
        })
        offset += length
    return {
        "size": size,
        "stroke": stroke,
        "radius": radius,
        "cx": cx,
        "cy": cy,
        "circumference": circ,
        "slices": out_slices,
        "total": total,
    }

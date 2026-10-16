"""Latency unit formatting shared by every surface that shows a duration.

Site convention (2026-10-09): a duration below one second stays in
milliseconds ("842ms"); one second or more collapses into seconds with a
single decimal ("191.7s"). The report page, the downloadable JPG and the
authenticity console all call these helpers so the same number never renders
two different ways.
"""

from __future__ import annotations

import math
from typing import Any

DASH = "—"


def format_ms(value: Any, *, space: bool = False, dash: str = DASH) -> str:
    """Render a millisecond duration as "842ms" or "191.7s"."""
    if value is None or value == "":
        return dash
    try:
        number = float(value)
    except (TypeError, ValueError):
        return dash
    if not math.isfinite(number):
        return dash
    sep = " " if space else ""
    if abs(number) >= 1000:
        return f"{number / 1000:.1f}{sep}s"
    return f"{int(round(number))}{sep}ms"

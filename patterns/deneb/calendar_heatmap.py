"""Calendar heatmap Deneb preset.

A year-view of a daily metric: rows = day-of-week, columns = ISO week number,
cell color = the metric. Mirrors GitHub's contribution graph layout.

Required fields:
* ``date_field``  -- temporal field (one row per day).
* ``value_field`` -- numeric field (count or measure).

Optional:
* ``low_color``  / ``high_color`` -- color ramp endpoints.
* ``cell_size`` (default 14)      -- side length of each day cell in px.
"""
from __future__ import annotations

from typing import Any

_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


def build(
    date_field: str,
    value_field: str,
    low_color: str = "#EBEDF0",
    high_color: str = "#118DFF",
    cell_size: int = 14,
) -> dict[str, Any]:
    """Return a Vega-Lite spec for a year calendar heatmap."""
    return {
        "$schema": _SCHEMA,
        "data": {"name": "dataset"},
        "mark": {
            "type": "rect",
            "stroke": "white",
            "strokeWidth": 1,
        },
        "encoding": {
            "x": {
                "field": date_field,
                "type": "ordinal",
                "timeUnit": "week",
                "title": None,
                "axis": {"labelFontSize": 9, "ticks": False, "domain": False},
            },
            "y": {
                "field": date_field,
                "type": "ordinal",
                "timeUnit": "day",
                "sort": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                "title": None,
                "axis": {"labelFontSize": 9, "ticks": False, "domain": False},
            },
            "color": {
                "field": value_field,
                "type": "quantitative",
                "scale": {"range": [low_color, high_color]},
                "legend": {"title": None, "labelFontSize": 9},
            },
            "tooltip": [
                {"field": date_field, "type": "temporal",
                 "format": "%b %d, %Y"},
                {"field": value_field, "type": "quantitative",
                 "format": ",.0f"},
            ],
        },
        "config": {
            "view": {"strokeWidth": 0, "step": cell_size},
            "axis": {"domain": False},
        },
    }

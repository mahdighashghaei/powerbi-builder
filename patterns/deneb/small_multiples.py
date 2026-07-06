"""Small multiples Deneb preset.

A faceted bar chart — one mini chart per ``facet_field`` value, all sharing the
same X axis. Useful for comparing a metric across many groups in a single
viewport (e.g. Sales by month, faceted by Region).

Required fields:
* ``facet_field``    -- nominal field that splits the small multiples.
* ``category_field`` -- nominal/ordinal X-axis field (e.g. Month).
* ``value_field``    -- quantitative Y value (column or measure).

Optional:
* ``columns``     -- how many facets per row before wrapping (default 3).
* ``bar_color``   -- single color used across all facets.
"""
from __future__ import annotations

from typing import Any

_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


def build(
    facet_field: str,
    category_field: str,
    value_field: str,
    columns: int = 3,
    bar_color: str = "#118DFF",
) -> dict[str, Any]:
    """Return a Vega-Lite spec for a small-multiples bar chart."""
    return {
        "$schema": _SCHEMA,
        "data": {"name": "dataset"},
        "facet": {
            "field": facet_field,
            "type": "nominal",
            "columns": columns,
            "header": {
                "labelFontSize": 11,
                "labelAnchor": "start",
                "title": None,
            },
        },
        "spec": {
            "width": 160,
            "height": 90,
            "mark": {"type": "bar", "color": bar_color},
            "encoding": {
                "x": {
                    "field": category_field,
                    "type": "nominal",
                    "axis": {"labelAngle": 0, "labelFontSize": 9,
                             "title": None},
                },
                "y": {
                    "field": value_field,
                    "type": "quantitative",
                    "axis": {"title": None, "labelFontSize": 9},
                },
            },
        },
    }

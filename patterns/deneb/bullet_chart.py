"""Bullet chart Deneb preset.

A horizontal "actual vs target" view, faceted by a category. Each row shows:

* a soft grey bar = Value (actual)
* a black tick   = Target
* numeric labels on both ends

Top-N filter (rank ≤ ``top_n``) is applied so the chart stays legible when many
categories are present.

Required fields:
* ``category_field`` -- nominal field that splits the rows.
* ``value_field``    -- actual value (measure or column).
* ``target_field``   -- target value (measure or column).

Optional:
* ``top_n``         -- limit rows by descending value (default 10).
* ``bar_color``     -- soft bar color (default #d4dae0).
* ``target_color``  -- target tick color (default black).
"""
from __future__ import annotations

from typing import Any

_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


def build(
    category_field: str,
    value_field: str,
    target_field: str,
    top_n: int = 10,
    bar_color: str = "#d4dae0",
    target_color: str = "#000000",
) -> dict[str, Any]:
    """Return a Vega-Lite spec for a faceted bullet chart."""
    return {
        "$schema": _SCHEMA,
        "data": {"name": "dataset"},
        "transform": [
            {"window": [{"op": "rank", "as": "rank"}],
             "sort": [{"field": value_field, "order": "descending"}]},
            {"filter": f"datum.rank <= {top_n}"},
        ],
        "facet": {
            "row": {
                "field": category_field,
                "type": "nominal",
                "sort": {"field": value_field, "order": "descending"},
                "header": {
                    "labelAngle": 0,
                    "title": "",
                    "labelAlign": "left",
                    "labelPadding": 5,
                },
            }
        },
        "spec": {
            "layer": [
                # Background bar = Value
                {
                    "mark": {
                        "type": "bar", "color": bar_color,
                        "size": 10, "opacity": 1,
                    },
                    "encoding": {
                        "x": {"field": value_field, "type": "quantitative"}
                    },
                },
                # Target tick
                {
                    "mark": {
                        "type": "tick", "color": target_color, "size": 25,
                    },
                    "encoding": {
                        "x": {"field": target_field, "type": "quantitative"}
                    },
                },
                # Value label (left of bar)
                {
                    "mark": {"type": "text", "align": "right", "dx": -5},
                    "encoding": {
                        "x": {"value": -15},
                        "text": {"field": value_field, "type": "quantitative",
                                 "format": ",.0f"},
                        "color": {"value": "#5d5d5d"},
                    },
                },
                # Target label (above tick)
                {
                    "mark": {"type": "text", "align": "center", "dy": -20},
                    "encoding": {
                        "x": {"field": target_field, "type": "quantitative"},
                        "text": {"field": target_field, "type": "quantitative",
                                 "format": ",.0f"},
                        "color": {"value": "#5d5d5d"},
                        "size": {"value": 9},
                    },
                },
            ]
        },
    }

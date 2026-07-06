"""Spark line Deneb preset.

A compact line trend with no axes — meant for inline use inside a card or table
cell. Shows the overall shape of a measure over time without the visual weight
of full axes / gridlines.

Required fields:
* ``date_field``  -- temporal field (a date column).
* ``value_field`` -- numeric field (column or measure).

Optional:
* ``line_color`` (default #118DFF)
* ``end_point``  -- if True, draw a small filled dot at the latest value.
"""
from __future__ import annotations

from typing import Any

_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


def build(
    date_field: str,
    value_field: str,
    line_color: str = "#118DFF",
    end_point: bool = True,
) -> dict[str, Any]:
    """Return a Vega-Lite spec for an axis-less spark line."""
    layer: list[dict[str, Any]] = [
        {
            "mark": {
                "type": "line",
                "color": line_color,
                "strokeWidth": 2,
                "interpolate": "monotone",
            },
            "encoding": {
                "x": {
                    "field": date_field, "type": "temporal",
                    "axis": None,
                },
                "y": {
                    "field": value_field, "type": "quantitative",
                    "axis": None,
                },
            },
        }
    ]
    if end_point:
        layer.append({
            "transform": [
                {"window": [{"op": "rank", "as": "_r"}],
                 "sort": [{"field": date_field, "order": "descending"}]},
                {"filter": "datum._r == 1"},
            ],
            "mark": {"type": "point", "filled": True,
                     "color": line_color, "size": 60},
            "encoding": {
                "x": {"field": date_field, "type": "temporal", "axis": None},
                "y": {"field": value_field, "type": "quantitative",
                      "axis": None},
            },
        })

    return {
        "$schema": _SCHEMA,
        "data": {"name": "dataset"},
        "config": {"view": {"stroke": "transparent"}},
        "layer": layer,
    }

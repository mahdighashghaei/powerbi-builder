"""KPI card Deneb preset.

Renders a big-number value with a small title above and a delta % vs target
below. Color flips between positive (blue) and negative (orange) based on the
sign of (value - target) / target.

Required fields:
* ``value_field``  -- the measure showing the current value.
* ``target_field`` -- the measure showing the comparison target (typically a
                      Prior-Year or budget measure).

Optional:
* ``title``        -- header text (default "Value vs Target").
* ``positive_color`` / ``negative_color`` -- override color condition.
"""
from __future__ import annotations

from typing import Any

_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


def build(
    value_field: str,
    target_field: str,
    title: str = "Value vs Target",
    positive_color: str = "#118DFF",
    negative_color: str = "#E66C37",
) -> dict[str, Any]:
    """Return a Vega-Lite spec for a KPI card."""
    return {
        "$schema": _SCHEMA,
        "data": {"name": "dataset"},
        "transform": [
            {"calculate": f"datum['{value_field}']", "as": "value"},
            {"calculate": f"datum['{target_field}']", "as": "target"},
            {"calculate": "(datum.value - datum.target) / datum.target",
             "as": "change"},
        ],
        "layer": [
            # Title (top)
            {
                "mark": {"type": "text", "fontSize": 14, "align": "center"},
                "encoding": {
                    "text": {"value": title},
                    "color": {"value": "#666666"},
                    "y": {"value": 30},
                },
            },
            # Big number (middle)
            {
                "mark": {
                    "type": "text", "fontSize": 48,
                    "fontWeight": "bold", "align": "center",
                },
                "encoding": {
                    "text": {"field": "value", "type": "quantitative",
                             "format": ",.0f"},
                    "color": {
                        "condition": {"test": "datum.change >= 0",
                                      "value": positive_color},
                        "value": negative_color,
                    },
                    "y": {"value": 90},
                },
            },
            # Delta % (bottom)
            {
                "mark": {"type": "text", "fontSize": 16, "align": "center"},
                "encoding": {
                    "text": {"field": "change", "type": "quantitative",
                             "format": "+.1%"},
                    "color": {
                        "condition": {"test": "datum.change >= 0",
                                      "value": positive_color},
                        "value": negative_color,
                    },
                    "y": {"value": 140},
                },
            },
        ],
    }

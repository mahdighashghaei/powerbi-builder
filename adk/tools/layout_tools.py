"""ADK tool: plan_page_layout — smart visual layout planning.

Wraps utils/layout_engine.py so the agent can request auto-positions
for a set of visual specs before calling write_pbir_page.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.layout_engine import (
    PAGE_H,
    PAGE_W,
    build_layout,
    _is_card,
    _is_chart,
    _is_slicer,
    _is_table,
)


def plan_page_layout(
    visual_specs: list[dict],
    page_width: int = PAGE_W,
    page_height: int = PAGE_H,
) -> dict:
    """Compute smart positions for a set of visuals on a report page.

    Args:
        visual_specs: list of {"id": str, "type": visualType} dicts.
            type is one of: card, kpi, slicer, columnChart, barChart,
            lineChart, donutChart, scatterChart, matrix, tableEx, etc.
        page_width:   page width in pixels (default 1280)
        page_height:  page height in pixels (default 720)

    Returns:
        {"positions": {id: {"x","y","width","height","z","tabOrder"}},
         "page_width": int, "page_height": int,
         "zones": {"cards": [...], "charts": [...], "tables": [...], "slicers": [...]}}

    Usage in write_pbir_page:
        Each visual in page_def.visuals should have its position fields
        set from the returned positions dict before calling write_pbir_page.

    Example::

        layout = plan_page_layout([
            {"id": "v-card-sales", "type": "card"},
            {"id": "v-chart",      "type": "columnChart"},
            {"id": "v-slicer",     "type": "slicer"},
            {"id": "v-table",      "type": "tableEx"},
        ])
        # layout["positions"]["v-card-sales"] -> {"x":10,"y":10,"width":310,"height":110,...}
    """
    positions = build_layout(visual_specs, page_width, page_height)

    # Classify into zones for informational output. Reuse the canonical
    # predicates from utils.layout_engine so the visual-type sets live in one
    # place (was duplicated here, risking drift if either set changed).
    zones: dict[str, list[str]] = {
        "cards": [], "charts": [], "tables": [], "slicers": [], "other": [],
    }

    for s in visual_specs:
        vid, vtype = s["id"], s["type"]
        if _is_card(vtype):      zones["cards"].append(vid)
        elif _is_chart(vtype):   zones["charts"].append(vid)
        elif _is_table(vtype):   zones["tables"].append(vid)
        elif _is_slicer(vtype):  zones["slicers"].append(vid)
        else:                    zones["other"].append(vid)

    return {
        "positions":   positions,
        "page_width":  page_width,
        "page_height": page_height,
        "zones":       {k: v for k, v in zones.items() if v},
        "count":       len(positions),
    }

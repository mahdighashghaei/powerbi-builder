"""ADK tools for SVG measure patterns (sparkline / progress bar / rating stars).

Exposes:

* ``list_svg_patterns``      — enumerate available SVG measure presets.
* ``suggest_svg_measures``   — build one or more SVG measures from preset
                                names + per-preset kwargs. The result is a
                                ``measures`` list you pass straight to
                                ``write_tmdl_measures``.

These complement ``adk.tools.dax_pattern_tools.suggest_dax_measures``:
that one returns numeric DAX measures (YTD, RANKX, ratios), this one returns
DAX measures that produce SVG data URIs Power BI renders inline.

IMPORTANT: each generated measure carries ``dataCategory: "ImageUrl"`` —
``write_tmdl_measures`` writes that into the TMDL so Desktop draws the
returned string as an image.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from patterns.svg import build_svg_measure, list_svg_patterns as _list_patterns  # noqa: E402


def list_svg_patterns() -> dict:
    """Return the available SVG measure preset keys + descriptions."""
    patterns = _list_patterns()
    return {"patterns": patterns, "count": len(patterns)}


def suggest_svg_measures(specs: list[dict[str, Any]]) -> dict:
    """Build a list of SVG DAX measures from per-spec configurations.

    Args:
        specs: each entry is one measure to generate, e.g.::

            {
                "preset": "sparkline",
                "measure_name": "Sales Sparkline",
                "table": "SampleData",
                "axis_table": "Date",
                "axis_column": "Date",
                "value_measure": "Total Sales",
            }

            {
                "preset": "progress_bar",
                "measure_name": "Sales vs Target",
                "table": "SampleData",
                "value_measure": "Total Sales",
                "target_measure": "Sales Target",
            }

            {
                "preset": "rating_stars",
                "measure_name": "CSAT Stars",
                "table": "Survey",
                "score_measure": "Avg CSAT",
                "max_score": 5,
            }

    Returns:
        ``{"ok": True, "measures": [...], "count": N}`` on success, or
        ``{"ok": False, "errors": [...]}`` if a spec is malformed. The
        ``measures`` list is the exact shape ``write_tmdl_measures`` expects
        (with the ``dataCategory: "ImageUrl"`` field already set).
    """
    measures: list[dict[str, Any]] = []
    for i, spec in enumerate(specs):
        preset = spec.get("preset")
        if not preset:
            return {
                "ok": False,
                "errors": [f"specs[{i}] missing 'preset' key"],
            }
        # Forward every key EXCEPT 'preset' so the caller's dict is never
        # mutated (spec.pop would destroy the input).
        rest = {k: v for k, v in spec.items() if k != "preset"}
        measure = build_svg_measure(preset, **rest)
        measures.append(measure)
    return {"ok": True, "measures": measures, "count": len(measures)}


__all__ = ["list_svg_patterns", "suggest_svg_measures"]

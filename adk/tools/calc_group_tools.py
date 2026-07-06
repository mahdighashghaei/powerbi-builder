"""ADK tools for calculation groups."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_server.server import PbipToolbox
from patterns.dax.calc_groups import (
    time_intelligence,
    period_comparison,
    currency_conversion,
)

_PRESETS = {
    "time_intelligence": time_intelligence,
    "period_comparison": period_comparison,
    "currency_conversion": currency_conversion,
}


def list_calc_group_presets() -> dict:
    """List available pre-built calculation group presets.

    Returns a dict with keys:
        presets: {name: description}
        usage: short usage note
    """
    return {
        "presets": {
            "time_intelligence": (
                "8 items: Current, YTD, QTD, MTD, PY, YoY Chg, YoY %, MAT"
            ),
            "period_comparison": (
                "4 items: Actual, Prior Period, Change, Change %"
            ),
            "currency_conversion": (
                "2 items: Local, USD (pass rate_measure to customise)"
            ),
        },
        "usage": (
            "Call write_calc_group(output_dir, preset='time_intelligence') "
            "or pass a custom group_def dict."
        ),
    }


def write_calc_group(
    output_dir: str,
    preset: str = "",
    group_def: dict | None = None,
    date_col: str = "'Date'[Date]",
    output_root: str = "./output",
) -> dict:
    """Write a calculation group TMDL file.

    Args:
        output_dir:  semantic model definition dir,
                     e.g. \"MyProject.SemanticModel/definition\"
        preset:      one of list_calc_group_presets() keys, e.g. \"time_intelligence\"
        group_def:   custom group definition dict (overrides preset)
        date_col:    fully-qualified date column for time presets,
                     default \"'Date'[Date]\"
        output_root: base output folder (default \"./output\")

    Returns write_tmdl_calc_group ToolResult as dict.
    """
    if group_def is None:
        if not preset:
            return {"ok": False, "errors": ["Either preset or group_def is required"]}
        factory = _PRESETS.get(preset)
        if factory is None:
            return {
                "ok": False,
                "errors": [
                    f"Unknown preset '{preset}'. "
                    f"Available: {list(_PRESETS)}"
                ],
            }
        # pass date_col only to presets that accept it
        import inspect
        sig = inspect.signature(factory)
        kwargs = {"date_col": date_col} if "date_col" in sig.parameters else {}
        group_def = factory(**kwargs)

    tb = PbipToolbox(output_root)
    return tb.write_tmdl_calc_group(output_dir, group_def).as_dict()

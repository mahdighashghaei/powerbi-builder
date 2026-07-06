"""ADK tools for naming convention (Phase 5.2)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.tmdl_parser import read_semantic_model  # noqa: E402
from validators.naming import (  # noqa: E402
    normalize_measure,
    pascal_case,
    plan_renames,
    suggest_folder,
    title_case,
)


def normalize_name(name: str, style: str = "title") -> dict:
    """Normalise a single name to PascalCase or Title Case.

    Args:
        name:  raw name (e.g. "total_sales_py")
        style: "title" (default) → "Total Sales PY"
               "pascal"          → "TotalSalesPY"
    """
    return {
        "input":  name,
        "style":  style,
        "result": normalize_measure(name, style=style),
        "pascal": pascal_case(name),
        "title":  title_case(name),
    }


def suggest_display_folder(
    measure_name: str,
    base_expression: str = "",
    base_folder: str = "Measures",
) -> dict:
    """Suggest a displayFolder hierarchy for a single measure.

    Returns:
        {"folder": "Measures\\YoY", "reason_keywords": [...]}
    """
    folder = suggest_folder(
        measure_name,
        base_expression=base_expression or None,
        default=base_folder,
    )
    return {"folder": folder, "name": measure_name, "base_folder": base_folder}


def plan_naming_for_pbip(
    pbip_dir: str,
    style: str = "title",
    base_folder: str = "Measures",
) -> dict:
    """Read a PBIP's SemanticModel and propose renames + folder assignments.

    Args:
        pbip_dir:    path to PBIP root or *.SemanticModel folder
        style:       "title" | "pascal"
        base_folder: top-level folder, e.g. "Measures" or "_Metrics"

    Returns the planner output enriched with model_path.
    """
    root = Path(pbip_dir)
    if not root.is_dir():
        return {
            "ok": False,
            "errors": [f"path not found or not a directory: {pbip_dir}"],
            "renames": [],
            "folders": [],
        }

    sm_dir: Path | None = None
    if root.name.endswith(".SemanticModel"):
        sm_dir = root
    else:
        for child in root.iterdir():
            if child.is_dir() and child.name.endswith(".SemanticModel"):
                sm_dir = child
                break

    if sm_dir is None:
        return {
            "ok": False,
            "errors": [f"no *.SemanticModel folder under {root}"],
            "renames": [],
            "folders": [],
        }

    model = read_semantic_model(sm_dir)
    plan = plan_renames(model, style=style, base_folder=base_folder)
    plan["ok"] = True
    plan["model_path"] = str(sm_dir)
    return plan


__all__ = [
    "normalize_name",
    "suggest_display_folder",
    "plan_naming_for_pbip",
]

"""ADK tools for lineage analysis (Phase 5.3)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.tmdl_parser import read_semantic_model  # noqa: E402
from validators.lineage import (  # noqa: E402
    build_dependency_graph,
    detect_cycles,
    find_impacts,
    summarize_lineage,
    topological_order,
)


def _load_model(pbip_dir: str):
    root = Path(pbip_dir)
    if not root.is_dir():
        return None, f"path not found or not a directory: {pbip_dir}"
    sm_dir: Path | None = None
    if root.name.endswith(".SemanticModel"):
        sm_dir = root
    else:
        for child in root.iterdir():
            if child.is_dir() and child.name.endswith(".SemanticModel"):
                sm_dir = child
                break
    if sm_dir is None:
        return None, f"no *.SemanticModel folder under {root}"
    return read_semantic_model(sm_dir), str(sm_dir)


def analyze_lineage(pbip_dir: str) -> dict:
    """Build the dependency graph + return summary stats.

    Returns:
        {
            "ok":         True,
            "summary":    {total_nodes, measures, columns, edges, cycles, ...},
            "graph":      {nodes, edges, deps, rdeps},
            "model_path": str,
        }
    """
    model, info = _load_model(pbip_dir)
    if model is None:
        return {"ok": False, "errors": [info], "summary": {}, "graph": {}}

    graph = build_dependency_graph(model)
    summary = summarize_lineage(graph)
    return {
        "ok":         True,
        "summary":    summary,
        "graph":      graph,
        "model_path": info,
    }


def find_measure_impacts(pbip_dir: str, measure_name: str) -> dict:
    """Find direct and transitive dependents of a measure.

    Useful BEFORE renaming a measure — shows everything that would break.
    """
    model, info = _load_model(pbip_dir)
    if model is None:
        return {"ok": False, "errors": [info], "found": False, "direct": [], "transitive": []}

    graph = build_dependency_graph(model)
    impacts = find_impacts(graph, measure_name, kind="measure")
    impacts["ok"] = True
    impacts["model_path"] = info
    return impacts


def find_column_impacts(pbip_dir: str, column_ref: str) -> dict:
    """Find dependents of a column.

    column_ref can be "Table.Column" or just "Column" (first match wins).
    """
    model, info = _load_model(pbip_dir)
    if model is None:
        return {"ok": False, "errors": [info], "found": False, "direct": [], "transitive": []}

    graph = build_dependency_graph(model)
    impacts = find_impacts(graph, column_ref, kind="column")
    impacts["ok"] = True
    impacts["model_path"] = info
    return impacts


def detect_circular_dependencies(pbip_dir: str) -> dict:
    """Return any circular DAX dependency chains in the model."""
    model, info = _load_model(pbip_dir)
    if model is None:
        return {"ok": False, "errors": [info], "cycles": []}

    graph = build_dependency_graph(model)
    cycles = detect_cycles(graph)
    return {
        "ok":          True,
        "cycles":      cycles,
        "cycle_count": len(cycles),
        "model_path":  info,
    }


def suggest_safe_rename_order(pbip_dir: str) -> dict:
    """Return topological order — safe rename / refactor order."""
    model, info = _load_model(pbip_dir)
    if model is None:
        return {"ok": False, "errors": [info], "order": []}

    graph = build_dependency_graph(model)
    order = topological_order(graph)
    return {"ok": True, "order": order, "model_path": info, "count": len(order)}


__all__ = [
    "analyze_lineage",
    "find_measure_impacts",
    "find_column_impacts",
    "detect_circular_dependencies",
    "suggest_safe_rename_order",
]

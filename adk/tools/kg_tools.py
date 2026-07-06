"""Standalone post-build analysis tool: query the project Knowledge Graph.

This module is a **standalone utility** — it is NOT part of the main
build pipeline (PlannerAgent → SchemaAgent → DAXAgent → ReportAgent → …).
It is intended for ad-hoc, post-build analysis of a finished PBIP project:
impact analysis, cross-entity dependency tracing, and structural queries.

Usage (CLI or script — not called by any pipeline agent):
    from adk.tools.kg_tools import query_knowledge_graph
    result = query_knowledge_graph("/path/to/MyProject.pbip", query="summary")

The tool is not registered in the orchestrator pipeline and introduces no
runtime dependency on the build flow. It is safe to call independently after
a successful build.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.tmdl_parser import read_semantic_model  # noqa: E402
from validators.knowledge_graph import (  # noqa: E402
    KnowledgeGraph,
    build_knowledge_graph,
)


def _load_graph(pbip_dir: str) -> tuple[KnowledgeGraph | None, str]:
    """Load the semantic model + report pages from a PBIP dir into a KG."""
    import json

    root = Path(pbip_dir)
    if not root.is_dir():
        return None, f"path not found: {pbip_dir}"
    sm_dir: Path | None = None
    for child in root.iterdir():
        if child.is_dir() and child.name.endswith(".SemanticModel"):
            sm_dir = child
            break
    if sm_dir is None:
        return None, f"no *.SemanticModel folder under {root}"
    model = read_semantic_model(sm_dir)
    # Read report pages to wire visual/page nodes + bindings.
    pages: list[dict[str, Any]] = []
    report_dir = next(root.glob("*.Report"), None)
    if report_dir and (report_dir / "definition" / "pages").is_dir():
        for page_folder in sorted((report_dir / "definition" / "pages").iterdir()):
            if not page_folder.is_dir():
                continue
            display_name = page_folder.name
            pjson = page_folder / "page.json"
            if pjson.is_file():
                try:
                    display_name = json.loads(pjson.read_text(encoding="utf-8")).get("displayName", display_name)
                except Exception:
                    pass
            visuals: list[dict[str, Any]] = []
            vdir = page_folder / "visuals"
            if vdir.is_dir():
                for vfolder in sorted(vdir.iterdir()):
                    vjson = vfolder / "visual.json"
                    if vjson.is_file():
                        try:
                            visuals.append(json.loads(vjson.read_text(encoding="utf-8")))
                        except Exception:
                            pass
            pages.append({"displayName": display_name, "visuals": visuals})
    return build_knowledge_graph(model, pages), str(sm_dir)


def query_knowledge_graph(pbip_dir: str, query: str = "summary", node_id: str = "") -> dict[str, Any]:
    """Query the project Knowledge Graph.

    Builds a knowledge graph (Tables, Columns, Measures, Relationships, Visuals,
    Pages + typed edges) from the PBIP and runs a graph query. Supports whole-
    project impact analysis, shortest path, neighbours, and node lookup by type.

    Args:
        pbip_dir: Path to a built PBIP project folder.
        query: One of ``summary`` | ``impact`` | ``neighbours`` | ``shortest_path``
            | ``nodes_by_type`` | ``subgraph``.
        node_id: Node id for queries that need one (impact, neighbours). For
            ``shortest_path`` use ``"src|dst"``.

    Returns:
        ``{"ok": True, "tool": "query_knowledge_graph", "data": {...}}`` or
        ``{"ok": False, "errors": [...]}``.
    """
    kg, info = _load_graph(pbip_dir)
    if kg is None:
        return {"ok": False, "tool": "query_knowledge_graph", "errors": [info], "data": {}}

    data: dict[str, Any] = {"model_path": info}
    if query == "summary":
        data["summary"] = kg.summary()
    elif query == "impact":
        data["impact"] = kg.impact(node_id) if node_id else []
        data["node_id"] = node_id
    elif query == "neighbours":
        data["neighbours"] = kg.neighbours(node_id) if node_id else []
        data["node_id"] = node_id
    elif query == "shortest_path":
        if "|" in node_id:
            src, dst = node_id.split("|", 1)
            data["path"] = kg.shortest_path(src.strip(), dst.strip())
        else:
            data["path"] = None
    elif query == "nodes_by_type":
        data["nodes"] = kg.nodes_by_type(node_id) if node_id else []
    else:
        return {
            "ok": False,
            "tool": "query_knowledge_graph",
            "errors": [f"unknown query: {query}"],
            "data": {},
        }
    return {"ok": True, "tool": "query_knowledge_graph", "data": data}


__all__ = ["query_knowledge_graph"]

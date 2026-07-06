"""Agent-to-UI (A2UI) protocol — dynamic UI manifest generation.

A2UI is a standard for agents to produce a *dynamic, personalized UI manifest*
that any generic UI client can render — rather than a hard-coded UI tied to one
framework. For the powerbi-builder, the agent already emits a PBIR report
(Power BI's native JSON). This module adds an **A2UI manifest** alongside it:
a framework-neutral description of the report's components (cards, charts,
tables) with their data bindings, so a non-Power-BI UI client (web dashboard,
chat widget, mobile view) can render the same dashboard.

The manifest schema is intentionally simple and stable:

::

    {
      "schema": "a2ui/1.0",
      "project": "SalesDashboard",
      "components": [
        {
          "id": "card-0",
          "type": "card",
          "title": "Total Amount",
          "binding": {"measure": "Total Amount", "table": "sample"},
          "props": {"format": "currency"}
        },
        {
          "id": "bar-1",
          "type": "chart",
          "chartType": "bar",
          "title": "Amount by Region",
          "binding": {"axis": "Region", "value": "Total Amount"},
          "props": {}
        },
        ...
      ],
      "layout": {"canvas": "1280x720", "pages": ["Summary"]}
    }

The manifest is written as ``ui-manifest.json`` at the PBIP root, next to
``build.spec.json`` and ``README.md``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Visual type -> A2UI component type mapping
# ---------------------------------------------------------------------------

_VISUAL_TYPE_MAP: dict[str, str] = {
    "card": "card",
    "barChart": "chart",
    "columnChart": "chart",
    "lineChart": "chart",
    "pieChart": "chart",
    "donutChart": "chart",
    "scatterChart": "chart",
    "areaChart": "chart",
    "clusteredBarChart": "chart",
    "clusteredColumnChart": "chart",
    "table": "table",
    "matrix": "table",
    "kpi": "kpi",
    "slicer": "slicer",
}

_CHART_SUBTYPE_MAP: dict[str, str] = {
    "barChart": "bar",
    "clusteredBarChart": "bar",
    "columnChart": "column",
    "clusteredColumnChart": "column",
    "lineChart": "line",
    "pieChart": "pie",
    "donutChart": "donut",
    "scatterChart": "scatter",
    "areaChart": "area",
}


def _component_from_visual(visual: dict[str, Any], idx: int) -> dict[str, Any]:
    """Convert a PBIR visual dict into an A2UI component descriptor."""
    vtype = visual.get("visualType", "card")
    title = visual.get("title", "") or f"Visual {idx}"
    comp_type = _VISUAL_TYPE_MAP.get(vtype, "generic")
    position = visual.get("position", {})
    component: dict[str, Any] = {
        "id": visual.get("id", f"visual-{idx}"),
        "type": comp_type,
        "title": title,
        "position": position,
        "props": {},
    }
    if comp_type == "chart":
        component["chartType"] = _CHART_SUBTYPE_MAP.get(vtype, vtype)
    # Extract data bindings from the query state if present.
    query = visual.get("query", {})
    state = query.get("queryState", {}) if isinstance(query, dict) else {}
    for role, fields in state.items():
        if isinstance(fields, list) and fields:
            binding: dict[str, Any] = {}
            for f in fields:
                if isinstance(f, dict):
                    name = f.get("name") or f.get("column")
                    if name:
                        binding[role] = name
            if binding:
                component["binding"] = binding
    return component


def build_manifest(project_name: str, pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an A2UI manifest from a project's report pages + visuals.

    Args:
        project_name: The PBIP project name.
        pages: The ``ctx.pages`` list (each page has ``displayName`` + ``visuals``).

    Returns:
        A dict matching the ``a2ui/1.0`` schema (see module docstring).
    """
    components: list[dict[str, Any]] = []
    page_names: list[str] = []
    for page in pages or []:
        page_names.append(page.get("displayName", "Page"))
        for i, visual in enumerate(page.get("visuals", []) or []):
            components.append(_component_from_visual(visual, i))
    return {
        "schema": "a2ui/1.0",
        "project": project_name,
        "components": components,
        "componentCount": len(components),
        "layout": {
            "canvas": "1280x720",
            "pages": page_names,
        },
    }


def render_ui_manifest(pbip_dir: str) -> dict[str, Any]:
    """Read a built PBIP's report.json + pages and emit the A2UI manifest.

    This is the ADK-tool entry point. It reads the on-disk PBIR report structure
    and returns the manifest (also writing it to ``ui-manifest.json`` at the
    PBIP root). Returns the standard ``{ok, tool, message, data, errors}``
    envelope.
    """
    root = Path(pbip_dir)
    if not root.is_dir():
        return {
            "ok": False,
            "tool": "render_ui_manifest",
            "message": f"path not found: {pbip_dir}",
            "errors": [f"path not found: {pbip_dir}"],
        }
    report_dir = next(root.glob("*.Report"), None)
    if report_dir is None:
        return {
            "ok": False,
            "tool": "render_ui_manifest",
            "message": "no .Report folder found",
            "errors": ["no .Report folder found"],
        }
    # Read pages from the PBIR definition/pages tree.
    pages_dir = report_dir / "definition" / "pages"
    pages: list[dict[str, Any]] = []
    if pages_dir.is_dir():
        for page_folder in sorted(pages_dir.iterdir()):
            if not page_folder.is_dir():
                continue
            page_json = page_folder / "page.json"
            visuals: list[dict[str, Any]] = []
            display_name = page_folder.name
            if page_json.is_file():
                try:
                    import json

                    pdata = json.loads(page_json.read_text(encoding="utf-8"))
                    display_name = pdata.get("displayName", display_name)
                except Exception:
                    pass
            vdir = page_folder / "visuals"
            if vdir.is_dir():
                for vfolder in sorted(vdir.iterdir()):
                    vjson = vfolder / "visual.json"
                    if vjson.is_file():
                        try:
                            import json

                            visuals.append(json.loads(vjson.read_text(encoding="utf-8")))
                        except Exception:
                            pass
            pages.append({"displayName": display_name, "visuals": visuals})

    manifest = build_manifest(root.name, pages)
    # Write the manifest next to README.md / build.spec.json.
    try:
        import json

        from utils.security import atomic_write_text  # noqa: E402

        atomic_write_text(root / "ui-manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    except Exception:
        pass  # fail-safe: still return the manifest even if the write fails

    return {
        "ok": True,
        "tool": "render_ui_manifest",
        "message": f"rendered A2UI manifest with {manifest['componentCount']} components",
        "data": manifest,
    }


__all__ = ["build_manifest", "render_ui_manifest"]

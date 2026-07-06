"""ADK tools for semantic report review + report-state visibility (Report Reviewer agent).

Exposes:
  * ``review_report``           — full semantic review of a generated PBIP
                                  report: ghost references, visual/data-type
                                  compatibility, layout overlap, measure coverage.
  * ``check_visual_references`` — detect visuals that reference non-existent
                                  measures or columns (ghost references).
  * ``list_pages``              — enumerate report pages with per-page visual counts.
  * ``describe_page``           — full detail of one page: every visual's type,
                                  position, and data bindings (let the model "see"
                                  the canvas before deciding edits).

These complement the structural ``validate_pbip_structure`` and the rules-based
``run_bpa_validation`` with a *semantic* layer: does the report make sense, not
just "is it syntactically valid". ``list_pages``/``describe_page`` add a
*visibility* layer so the model can read the current canvas state (positions +
bindings) before editing, instead of acting blind.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.tmdl_parser import read_semantic_model  # noqa: E402


def _read_visual_position(visual: dict) -> dict[str, float | int]:
    """Read geometry from a visual.json payload.

    Power BI writes geometry under a nested ``position`` object (see
    ``pbir_generator.visual_json``: ``{"position": {"x","y","z","height",
    "width","tabOrder"}}``). Older code read flat top-level ``x/y/...`` keys
    which were always absent (defaulting to 0), so overlap detection silently
    never fired. This helper centralises the nested read so ``review_report``
    and ``describe_page`` share one correct source of truth.
    """
    pos = visual.get("position", {})
    return {
        "x": float(pos.get("x", 0)),
        "y": float(pos.get("y", 0)),
        "z": int(pos.get("z", 0)),
        "height": float(pos.get("height", 0)),
        "width": float(pos.get("width", 0)),
        "tabOrder": int(pos.get("tabOrder", pos.get("tab_order", 0))),
    }


def _read_visual_title(visual: dict) -> str | None:
    """Extract the title text from a visual.json, if set.

    ``pbir_generator.visual_json`` writes titles as
    ``visual.objects.title[0].properties.text.expr.Literal.Value`` wrapped in
    single quotes (``"'My Title'"``). Return the unwrapped string, or None.
    """
    try:
        obj = visual["visual"]["objects"]["title"][0]["properties"]["text"]
        val = obj["expr"]["Literal"]["Value"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(val, str) and len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    return val if isinstance(val, str) else None


def _load_model_measures_and_columns(sm_dir: Path) -> tuple[set[str], dict[str, set[str]]]:
    """Return (measure_names, {table_name: {column_names}}) from a SemanticModel."""
    try:
        model = read_semantic_model(sm_dir)
        measures = set(model.get("measure_names", []))
        tables = {t["table_name"]: {c["name"] for c in t.get("columns", [])}
                  for t in model.get("tables", [])}
        return measures, tables
    except Exception:
        return set(), {}


def _extract_refs_from_visual(visual: dict) -> list[dict[str, str]]:
    """Extract measure/column references from a visual.json payload.

    PBIR visual.json nests the query state under
    ``visual.visual.query.queryState`` (see pbir_generator.visual_json), so we
    must look there in addition to the legacy top-level ``visual.queryState``
    and ``queryState.protoConf`` locations the original code scanned.
    """
    refs: list[dict[str, str]] = []
    candidate_blobs: list[dict] = []
    # Legacy / synthetic shape: top-level "queryState" on the visual.json root.
    qs = visual.get("queryState")
    if isinstance(qs, dict):
        candidate_blobs.append(qs)
    # Canonical PBIR shape: visual.query.queryState (written by pbir_generator).
    inner = visual.get("visual", {})
    if isinstance(inner, dict):
        query = inner.get("query", {})
        if isinstance(query, dict):
            cqs = query.get("queryState")
            if isinstance(cqs, dict):
                candidate_blobs.append(cqs)
    for blob in candidate_blobs:
        # protoConf may hold an alternate query definition.
        for key in ("query", "queryState", "protoConf"):
            sub = blob.get(key) if key != "queryState" else blob
            if isinstance(sub, dict):
                _walk_for_refs(sub, refs)
    return refs


def _walk_for_refs(obj: Any, refs: list[dict[str, str]], depth: int = 0) -> None:
    """Recursively walk a visual JSON looking for measure/column references.

    Recognises two reference shapes:
      * ``{"measure": "[MeasureName]"}`` / ``{"measureName": "..."}`` — the
        legacy / bracketed form.
      * ``{"kind": "measure", "name": "MeasureName"}`` — the simplified
        ``queryState`` form emitted by pbir_generator's role-projection builder
        and accepted by ``add_visual``. The same applies to ``kind: "column"``.
    """
    if depth > 10:
        return
    if isinstance(obj, dict):
        # Simplified form: {"kind": "measure"|"column", "name": "..."}
        kind = obj.get("kind")
        if isinstance(kind, str) and kind.lower() in ("measure", "column"):
            name = obj.get("name")
            if isinstance(name, str) and name:
                refs.append({"kind": kind.lower(), "name": name.strip("[]")})
        # Legacy form: {"measure": "[Name]"} / {"measureName": "..."} / column.
        for k, v in obj.items():
            if k.lower() in ("measure", "measurename") and isinstance(v, str):
                refs.append({"kind": "measure", "name": v.strip("[]")})
            elif k.lower() in ("column", "columnname") and isinstance(v, str):
                refs.append({"kind": "column", "name": v})
            else:
                _walk_for_refs(v, refs, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_refs(item, refs, depth + 1)


def check_visual_references(pbip_dir: str) -> dict[str, Any]:
    """Detect visuals that reference non-existent measures or columns.

    Returns ``{ok, ghost_refs: [{page, visual, kind, name}], total_visuals, ghost_count}``.
    """
    root = Path(pbip_dir).expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "errors": [f"Not a directory: {pbip_dir}"]}
    sm_dirs = list(root.glob("*.SemanticModel"))
    if not sm_dirs:
        return {"ok": False, "errors": ["No *.SemanticModel folder found"]}
    measures, tables = _load_model_measures_and_columns(sm_dirs[0])
    all_columns = set()
    for cols in tables.values():
        all_columns |= cols

    rep_dirs = list(root.glob("*.Report"))
    ghost_refs: list[dict[str, str]] = []
    total_visuals = 0
    for rep in rep_dirs:
        pages_dir = rep / "definition" / "pages"
        if not pages_dir.is_dir():
            continue
        for page in pages_dir.iterdir():
            if not page.is_dir():
                continue
            visuals_dir = page / "visuals"
            if not visuals_dir.is_dir():
                continue
            for vdir in visuals_dir.iterdir():
                if not vdir.is_dir():
                    continue
                vjson = vdir / "visual.json"
                if not vjson.is_file():
                    continue
                total_visuals += 1
                try:
                    visual = json.loads(vjson.read_text(encoding="utf-8"))
                except Exception:
                    continue
                refs = _extract_refs_from_visual(visual)
                for ref in refs:
                    name = ref["name"]
                    if ref["kind"] == "measure" and name and name not in measures:
                        ghost_refs.append({
                            "page": page.name, "visual": vdir.name,
                            "kind": "measure", "name": name,
                        })
                    elif ref["kind"] == "column" and name and name not in all_columns:
                        ghost_refs.append({
                            "page": page.name, "visual": vdir.name,
                            "kind": "column", "name": name,
                        })
    return {
        "ok": True,
        "ghost_refs": ghost_refs,
        "total_visuals": total_visuals,
        "ghost_count": len(ghost_refs),
    }


def review_report(pbip_dir: str) -> dict[str, Any]:
    """Full semantic review of a generated PBIP report.

    Checks:
      * Ghost references (visuals pointing to non-existent measures/columns)
      * Visual count reasonableness (too many/few per page)
      * Layout overlap (visuals occupying the same region)
      * Measure coverage (are key measures like Total/Count present?)

    Returns ``{ok, score, strengths, issues, suggestions, ...}``.
    """
    root = Path(pbip_dir).expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "errors": [f"Not a directory: {pbip_dir}"]}

    # 1) ghost references
    ghost = check_visual_references(pbip_dir)
    issues: list[str] = []
    strengths: list[str] = []
    suggestions: list[str] = []
    score = 100

    ghost_refs = ghost.get("ghost_refs", [])
    if ghost_refs:
        score -= 20 * min(len(ghost_refs), 3)
        for g in ghost_refs:
            issues.append(
                f"Ghost reference: visual '{g['visual']}' on page '{g['page']}' "
                f"references {g['kind']} '{g['name']}' which does not exist in the model."
            )
    else:
        strengths.append("All visuals reference real model objects (no ghost references).")

    # 2) visual count per page
    rep_dirs = list(root.glob("*.Report"))
    total_pages = 0
    total_visuals = 0
    for rep in rep_dirs:
        pages_dir = rep / "definition" / "pages"
        if not pages_dir.is_dir():
            continue
        for page in pages_dir.iterdir():
            if not page.is_dir():
                continue
            total_pages += 1
            visuals_dir = page / "visuals"
            if not visuals_dir.is_dir():
                continue
            vcount = sum(1 for v in visuals_dir.iterdir() if v.is_dir())
            total_visuals += vcount
            if vcount > 10:
                score -= 5
                issues.append(f"Page '{page.name}' has {vcount} visuals — consider splitting.")
            elif vcount == 0:
                score -= 10
                issues.append(f"Page '{page.name}' has no visuals.")
    if total_pages > 0 and total_visuals > 0:
        strengths.append(f"Report has {total_pages} page(s) with {total_visuals} visual(s).")

    # 3) layout overlap — check visual bounding boxes
    for rep in rep_dirs:
        pages_dir = rep / "definition" / "pages"
        if not pages_dir.is_dir():
            continue
        for page in pages_dir.iterdir():
            if not page.is_dir():
                continue
            visuals_dir = page / "visuals"
            if not visuals_dir.is_dir():
                continue
            boxes: list[tuple[str, int, int, int, int]] = []
            for vdir in visuals_dir.iterdir():
                if not vdir.is_dir():
                    continue
                vjson = vdir / "visual.json"
                if not vjson.is_file():
                    continue
                try:
                    v = json.loads(vjson.read_text(encoding="utf-8"))
                    pos = _read_visual_position(v)
                    x, y = int(pos["x"]), int(pos["y"])
                    w, h = int(pos["width"]), int(pos["height"])
                    boxes.append((vdir.name, x, y, x + w, y + h))
                except Exception:
                    continue
            # pairwise overlap check
            for i in range(len(boxes)):
                for j in range(i + 1, len(boxes)):
                    n1, x1a, y1a, x1b, y1b = boxes[i]
                    n2, x2a, y2a, x2b, y2b = boxes[j]
                    if x1a < x2b and x1b > x2a and y1a < y2b and y1b > y2a:
                        score -= 3
                        issues.append(f"Visuals '{n1}' and '{n2}' on page '{page.name}' overlap.")

    # 4) measure coverage
    sm_dirs = list(root.glob("*.SemanticModel"))
    if sm_dirs:
        measures, _ = _load_model_measures_and_columns(sm_dirs[0])
        has_total = any("total" in m.lower() for m in measures)
        has_count = any("count" in m.lower() for m in measures)
        if not has_total:
            suggestions.append("Consider adding a 'Total <Amount>' measure for KPI cards.")
        if not has_count:
            suggestions.append("Consider adding a row-count measure (e.g. 'Order Count').")
        if has_total and has_count:
            strengths.append("Key measures (Total + Count) are present.")

    score = max(0, score)
    return {
        "ok": True,
        "score": score,
        "strengths": strengths,
        "issues": issues,
        "suggestions": suggestions,
        "total_pages": total_pages,
        "total_visuals": total_visuals,
        "ghost_count": len(ghost_refs),
    }


def _find_report_dir(pbip_dir: str) -> Path | None:
    """Resolve a PBIP project root and return its *.Report directory, or None."""
    root = Path(pbip_dir).expanduser().resolve()
    if not root.is_dir():
        return None
    rep_dirs = list(root.glob("*.Report"))
    return rep_dirs[0] if rep_dirs else None


def list_pages(pbip_dir: str) -> dict[str, Any]:
    """List all pages in a PBIP report with per-page visual counts.

    Read-only. Follows the same absolute-path convention as ``review_report``
    (no ``output_root`` / ``safe_join``).

    Args:
        pbip_dir: Path to the PBIP project folder (contains *.Report).

    Returns:
        ``{ok, pages: [{page_id, display_name, width, height, visual_count}],
        total_pages, total_visuals}``. On a missing project: ``{ok: False}``.
    """
    rep = _find_report_dir(pbip_dir)
    if rep is None:
        root = Path(pbip_dir).expanduser().resolve()
        if not root.is_dir():
            return {"ok": False, "errors": [f"Not a directory: {pbip_dir}"]}
        return {"ok": False, "errors": ["No *.Report folder found"]}

    pages_dir = rep / "definition" / "pages"
    if not pages_dir.is_dir():
        return {"ok": True, "pages": [], "total_pages": 0, "total_visuals": 0}

    pages: list[dict[str, Any]] = []
    total_visuals = 0
    for page in sorted(pages_dir.iterdir(), key=lambda p: p.name):
        if not page.is_dir():
            continue
        page_json = page / "page.json"
        display_name = page.name
        width = height = 0
        if page_json.is_file():
            try:
                pmeta = json.loads(page_json.read_text(encoding="utf-8"))
                display_name = pmeta.get("displayName", page.name)
                width = int(pmeta.get("width", 0))
                height = int(pmeta.get("height", 0))
            except Exception:
                pass
        visuals_dir = page / "visuals"
        vcount = 0
        if visuals_dir.is_dir():
            vcount = sum(1 for v in visuals_dir.iterdir() if v.is_dir())
        total_visuals += vcount
        pages.append({
            "page_id": page.name,
            "display_name": display_name,
            "width": width,
            "height": height,
            "visual_count": vcount,
        })
    return {
        "ok": True,
        "pages": pages,
        "total_pages": len(pages),
        "total_visuals": total_visuals,
    }


def describe_page(pbip_dir: str, page_id: str) -> dict[str, Any]:
    """Full detail of one report page: every visual's type, position, and bindings.

    Read-only. This is the "eyes" of the agent: before adding/moving a visual
    it should call this to see what is already on the target page (so it can
    pick a free spot and avoid overlaps). Positions come from the nested
    ``position`` object written by ``pbir_generator.visual_json``.

    Args:
        pbip_dir: Path to the PBIP project folder.
        page_id:  Page folder name (the ``pages/<id>`` directory).

    Returns:
        ``{ok, page_id, display_name, width, height,
        visuals: [{id, type, title, position, bindings}], visual_count}``.
        On a missing page: ``{ok: False, errors: [...]}``.
    """
    rep = _find_report_dir(pbip_dir)
    if rep is None:
        root = Path(pbip_dir).expanduser().resolve()
        if not root.is_dir():
            return {"ok": False, "errors": [f"Not a directory: {pbip_dir}"]}
        return {"ok": False, "errors": ["No *.Report folder found"]}

    page_dir = rep / "definition" / "pages" / page_id
    if not page_dir.is_dir():
        return {"ok": False, "errors": [f"Page '{page_id}' not found"]}

    # page metadata
    display_name = page_id
    width = height = 0
    page_json = page_dir / "page.json"
    if page_json.is_file():
        try:
            pmeta = json.loads(page_json.read_text(encoding="utf-8"))
            display_name = pmeta.get("displayName", page_id)
            width = int(pmeta.get("width", 0))
            height = int(pmeta.get("height", 0))
        except Exception:
            pass

    visuals_dir = page_dir / "visuals"
    visuals: list[dict[str, Any]] = []
    if visuals_dir.is_dir():
        for vdir in sorted(visuals_dir.iterdir(), key=lambda p: p.name):
            if not vdir.is_dir():
                continue
            vjson = vdir / "visual.json"
            if not vjson.is_file():
                continue
            try:
                v = json.loads(vjson.read_text(encoding="utf-8"))
            except Exception:
                continue
            inner = v.get("visual", {})
            visuals.append({
                "id": vdir.name,
                "type": inner.get("visualType", "unknown"),
                "title": _read_visual_title(v),
                "position": _read_visual_position(v),
                "bindings": _extract_refs_from_visual(v),
            })
    return {
        "ok": True,
        "page_id": page_id,
        "display_name": display_name,
        "width": width,
        "height": height,
        "visuals": visuals,
        "visual_count": len(visuals),
    }


__all__ = ["review_report", "check_visual_references", "list_pages", "describe_page"]

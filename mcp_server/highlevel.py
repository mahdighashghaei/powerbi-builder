"""High-level MCP tools — Phase 7.

These tools wrap the existing PbipToolbox + orchestrator into single-call
operations that an MCP client (Claude Desktop, an LLM agent, a CLI script)
can invoke without orchestrating the multi-step pipeline manually.

All tools return a normalized dict::

    {"ok": bool, "tool": str, "message": str, "data": dict, "errors": [str]}

so callers can route them through the same UI pipeline they use for the
low-level tools.

Tools
-----
* ``generate_pbip``       — build a complete PBIP from CSV / Excel / JSON
* ``edit_pbip``           — add measures / pages to an existing PBIP
* ``add_measure``         — append a single DAX measure
* ``add_visual``          — add one visual to an existing page
* ``add_page``            — add one new page with smart layout
* ``deploy_to_fabric``    — upload a PBIP to a Fabric workspace
* ``suggest_measures``    — propose DAX measures from a CSV / PBIP schema
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from utils import AuditLogger, ensure_dir, stable_uuid

log = AuditLogger.get("mcp_server.highlevel")


def _build_metadata(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize per-agent ``AgentResult.data`` from a ``RunReport.steps``
    list into a flat, caller-friendly metadata dict.

    Each step already carries its own agent's full ``data`` payload (see
    ``agents/orchestrator.py::RunReport.add``), but the top-level
    ``generate_pbip`` return previously discarded everything except
    ``agent``/``ok``/``message`` per step -- dropping insights, KPI
    counts, quality scores, and page/visual/table counts that the
    pipeline had already computed. This reassembles them by agent name so
    callers don't have to know the internal step order/shape.
    """
    by_agent: dict[str, dict[str, Any]] = {}
    for s in steps:
        by_agent.setdefault(s.get("agent", ""), {})
        d = s.get("data") or {}
        if d:
            by_agent[s["agent"]] = d

    meta: dict[str, Any] = {}

    report_data = by_agent.get("ReportAgent", {})
    if "page_count" in report_data:
        meta["page_count"] = report_data.get("page_count")
    if "visual_count" in report_data:
        meta["visual_count"] = report_data.get("visual_count")

    dax_data = by_agent.get("DAXAgent", {})
    if "count" in dax_data:
        meta["measure_count"] = dax_data.get("count")
    if "folders" in dax_data:
        meta["measure_folders"] = dax_data.get("folders")

    # SchemaAgent runs once per table -- accumulate across every such step.
    schema_steps = [s for s in steps if s.get("agent") == "SchemaAgent" and s.get("data")]
    if schema_steps:
        meta["table_count"] = len(schema_steps)
        meta["column_count"] = sum(
            s["data"].get("column_count", 0) for s in schema_steps
        )

    analyzer_data = by_agent.get("DataAnalyzerAgent", {})
    if "quality_score" in analyzer_data:
        meta["quality_score"] = analyzer_data.get("quality_score")
    if "potential_kpi_count" in analyzer_data:
        meta["potential_kpi_count"] = analyzer_data.get("potential_kpi_count")

    reviewer_data = by_agent.get("ReportReviewerAgent", {})
    if "score" in reviewer_data:
        meta["review_score"] = reviewer_data.get("score")

    insights_data = by_agent.get("InsightsAgent", {})
    for key in (
        "anomaly_count", "segment_count", "underperformer_count",
        "trend_count", "kpi_suggestion_count",
    ):
        if key in insights_data:
            meta[key] = insights_data[key]

    return meta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(ok: bool, tool: str, message: str,
            data: dict | None = None, errors: list[str] | None = None) -> dict:
    return {
        "ok": ok,
        "tool": tool,
        "message": message,
        "data": data or {},
        "errors": errors or [],
    }


def _safe_name(s: str) -> str:
    keep = "".join(c if c.isalnum() else " " for c in (s or ""))
    parts = keep.split()
    return "".join(p[:1].upper() + p[1:] for p in parts) or "PowerBIProject"


def _safe_slug(s: str) -> str:
    """Convert a display name to a URL-safe page id slug.

    "Sales Trends" → "sales-trends"; "Product & Customer Analysis" →
    "product-customer-analysis".
    """
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip()).strip("-").lower()
    return slug or "page"


# Project root for discovering bundled sample data files.
_PROJECT_ROOT = Path(__file__).parent.parent

_DATA_EXTENSIONS = {".csv", ".json", ".xlsx", ".xls", ".xlsm", ".xlsb"}


def _suggest_data_files() -> list[str]:
    """Return paths of data files bundled with the project.

    Searches the project root and ``examples/`` for CSV/Excel/JSON files
    so error messages can offer actionable alternatives.  Config/template
    JSON files are excluded.
    """
    _CONFIG_NAMES = {".mcp.json", "claude_desktop_config.template.json"}
    suggestions: list[str] = []
    for search_dir in (_PROJECT_ROOT, _PROJECT_ROOT / "examples"):
        if not search_dir.is_dir():
            continue
        for p in sorted(search_dir.iterdir()):
            if not p.is_file():
                continue
            if p.name in _CONFIG_NAMES:
                continue
            if p.suffix.lower() in _DATA_EXTENSIONS:
                suggestions.append(str(p))
    return suggestions


# Public alias so the ADK layer (adk/agent.py, adk/chat.py) can import a
# stable, non-underscored name instead of reaching into a private symbol.
suggest_data_files = _suggest_data_files


def _file_not_found_msg(path: str, label: str = "Source file") -> str:
    """Build an error message that includes available-file suggestions."""
    msg = f"{label} not found: {path}"
    suggestions = _suggest_data_files()
    if suggestions:
        msg += "\nAvailable data files:\n  " + "\n  ".join(suggestions)
    return msg


def _find_single_project(output_root: Path) -> tuple[str, Path] | None:
    """Return (project_name, project_root) for exactly one PBIP under output_root.

    Returns None if zero or multiple projects are present. The project root
    is the folder containing both *.SemanticModel and *.Report.
    """
    sm_dirs = list(output_root.glob("*.SemanticModel"))
    rep_dirs = list(output_root.glob("*.Report"))
    if len(sm_dirs) == 1 and len(rep_dirs) == 1:
        name = sm_dirs[0].name.removesuffix(".SemanticModel")
        return name, output_root
    if not sm_dirs and not rep_dirs:
        # nested project? look one level down
        for sub in output_root.iterdir():
            if not sub.is_dir():
                continue
            inner_sm = list(sub.glob("*.SemanticModel"))
            inner_rep = list(sub.glob("*.Report"))
            if len(inner_sm) == 1 and len(inner_rep) == 1:
                return inner_sm[0].name.removesuffix(".SemanticModel"), sub
    return None


def _resolve_project(pbip_dir: str) -> tuple[str, Path]:
    """Resolve a pbip_dir argument to (project_name, project_root)."""
    p = Path(pbip_dir).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"pbip_dir not found: {p}")

    sm_dirs = list(p.glob("*.SemanticModel"))
    if not sm_dirs:
        # caller may have passed the .SemanticModel folder itself
        if p.name.endswith(".SemanticModel"):
            return p.name.removesuffix(".SemanticModel"), p.parent
        raise ValueError(f"No *.SemanticModel folder found under {p}")
    if len(sm_dirs) > 1:
        raise ValueError(
            f"Found {len(sm_dirs)} *.SemanticModel folders under {p}; "
            "please pass a path that contains exactly one PBIP project."
        )
    return sm_dirs[0].name.removesuffix(".SemanticModel"), p


# ---------------------------------------------------------------------------
# generate_pbip
# ---------------------------------------------------------------------------


def generate_pbip(source: str,
                  description: str,
                  *,
                  project_name: str | None = None,
                  output_root: str = "./output",
                  theme_preset: str = "default",
                  sheet: str | None = None,
                  num_pages: int = 0,
                  visual_variety: str = "") -> dict:
    """Build a complete PBIP project from a CSV / Excel / JSON file.

    Drives the same OrchestratorAgent the CLI uses.

    Args:
        source: Path to CSV, JSON-schema, or Excel file.
        description: Plain-English description (what to build).
        project_name: Override the auto-derived project name.
        output_root: Base output directory (default: ./output).
        theme_preset: Theme preset key (default, modern_dark, ...).
        sheet: Excel sheet name when source is xlsx (default first sheet).
        num_pages: Optional explicit page count. When set (> 0), decide the
            exact number of pages you want up front and pass it here —
            this produces exactly that many pages in this ONE call. Do
            NOT rely on description keywords ("3 pages") alone and then
            call build_report to top up: build_report ADDS pages on top
            of whatever this call already created, which overshoots
            whatever count you actually wanted.
        visual_variety: "" (default) or "all" to also include scatter/pie/
            kpi visuals directly in this build (previously only available
            via a separate build_report follow-up call).

    Returns:
        Normalized result dict with ``data["pbip_root"]``, ``data["project_name"]``,
        ``data["validation"]`` from the validator step, and
        ``data["metadata"]`` (page_count, visual_count, measure_count,
        table_count, quality_score, review_score, insight/KPI counts --
        whichever of these the pipeline actually computed).
    """
    try:
        # local import — keeps the MCP server importable even if agents
        # have heavy optional deps that fail to load in an MCP context.
        from agents.orchestrator import OrchestratorAgent

        src = Path(source).expanduser().resolve()
        if not src.exists():
            return _result(False, "generate_pbip",
                           _file_not_found_msg(str(src)),
                           errors=[f"missing: {src}"])

        ext = src.suffix.lower()
        if ext == ".csv":
            input_mode = "create"
        elif ext == ".json":
            input_mode = "create"
        elif ext in {".xlsx", ".xls", ".xlsm", ".xlsb"}:
            input_mode = "edit_excel"
        else:
            return _result(False, "generate_pbip",
                           f"Unsupported source type: {ext}",
                           errors=[f"unsupported: {ext}"])

        out_root = Path(output_root).expanduser().resolve()
        ensure_dir(out_root)
        orchestrator = OrchestratorAgent(output_root=out_root)
        report = orchestrator.run(
            source_path=src,
            business_description=description,
            project_name=project_name,
            input_mode=input_mode,
            theme_preset=theme_preset,
            num_pages=num_pages,
            visual_variety=visual_variety,
        )

        return _result(
            report.ok,
            "generate_pbip",
            f"Built '{report.project_name}'." if report.ok
            else f"Build failed: {report.error or 'see steps'}",
            data={
                "project_name": report.project_name,
                "pbip_root": report.pbip_root,
                "validation": report.validation,
                "metadata": _build_metadata(report.steps),
                "steps": [
                    {"agent": s["agent"], "ok": s["ok"], "message": s["message"]}
                    for s in report.steps
                ],
            },
            errors=[s["message"] for s in report.steps if not s["ok"]],
        )
    except Exception as exc:
        log.exception("[generate_pbip] failed")
        return _result(False, "generate_pbip", str(exc), errors=[str(exc)])


# ---------------------------------------------------------------------------
# edit_pbip
# ---------------------------------------------------------------------------


def edit_pbip(pbip_dir: str,
              description: str,
              *,
              output_root: str | None = None,
              theme_preset: str = "default") -> dict:
    """Edit an existing PBIP — add measures, pages, etc. based on description.

    Re-runs the orchestrator in ``edit_pbip`` mode. The source folder is copied
    into ``output_root`` first so the original is not modified in place.

    Args:
        pbip_dir: Path to an existing PBIP project folder.
        description: Plain-English description of the edits to make.
        output_root: Base output directory (default: parent of pbip_dir).
        theme_preset: Theme preset key.
    """
    try:
        from agents.orchestrator import OrchestratorAgent

        src = Path(pbip_dir).expanduser().resolve()
        if not src.is_dir():
            return _result(False, "edit_pbip",
                           f"PBIP folder not found: {src}",
                           errors=[f"missing: {src}"])
        if not list(src.glob("*.SemanticModel")):
            return _result(False, "edit_pbip",
                           f"No *.SemanticModel inside {src}",
                           errors=["not a PBIP project"])

        out_root = Path(output_root).expanduser().resolve() if output_root else src.parent
        ensure_dir(out_root)
        orchestrator = OrchestratorAgent(output_root=out_root)
        report = orchestrator.run(
            source_path=src,
            business_description=description,
            project_name=None,  # keep original name
            input_mode="edit_pbip",
            theme_preset=theme_preset,
        )
        return _result(
            report.ok,
            "edit_pbip",
            f"Edited '{report.project_name}'." if report.ok
            else f"Edit failed: {report.error or 'see steps'}",
            data={
                "project_name": report.project_name,
                "pbip_root": report.pbip_root,
                "validation": report.validation,
                "steps": [
                    {"agent": s["agent"], "ok": s["ok"], "message": s["message"]}
                    for s in report.steps
                ],
            },
            errors=[s["message"] for s in report.steps if not s["ok"]],
        )
    except Exception as exc:
        log.exception("[edit_pbip] failed")
        return _result(False, "edit_pbip", str(exc), errors=[str(exc)])


# ---------------------------------------------------------------------------
# add_measure
# ---------------------------------------------------------------------------


def add_measure(pbip_dir: str,
                name: str,
                expression: str,
                *,
                table: str | None = None,
                format_string: str | None = None,
                display_folder: str | None = None,
                description: str | None = None) -> dict:
    """Append a single DAX measure to a PBIP semantic model.

    Args:
        pbip_dir: Path to an existing PBIP project folder.
        name: Measure name (e.g. ``"Total Sales"``).
        expression: DAX expression (e.g. ``"SUM('Sales'[Amount])"``).
        table: Target table name. If None, the first non-Date, non-Measures
               data table is used.
        format_string: Optional TMDL formatString (e.g. ``"#,0.00"``).
        display_folder: Optional display folder path.
        description: Optional measure description — IGNORED (TMDL does not
            accept ``description`` on measures in current Power BI versions);
            accepted as a no-op so MCP clients can pass it without breaking.
    """
    try:
        from mcp_server.server import PbipToolbox

        project_name, project_root = _resolve_project(pbip_dir)
        toolbox = PbipToolbox(project_root)
        sm_def = f"{project_name}.SemanticModel/definition"

        m: dict[str, Any] = {"name": name, "expression": expression}
        if table:
            m["table"] = table
        if format_string:
            m["formatString"] = format_string
        if display_folder:
            m["displayFolder"] = display_folder
        # `description` accepted for forward compatibility but dropped: TMDL
        # parser rejects it as an unknown property on measures.
        _ = description

        result = toolbox.write_tmdl_measures(sm_def, [m])
        return _result(
            result.ok,
            "add_measure",
            result.message,
            data={
                "project_name": project_name,
                "pbip_root": str(project_root),
                "measure_name": name,
                "table": table,
                **(result.data or {}),
            },
            errors=result.errors,
        )
    except Exception as exc:
        log.exception("[add_measure] failed")
        return _result(False, "add_measure", str(exc), errors=[str(exc)])


# ---------------------------------------------------------------------------
# add_visual
# ---------------------------------------------------------------------------


def add_visual(pbip_dir: str,
               page_id: str,
               visual_type: str,
               query_state: dict,
               *,
               title: str | None = None,
               x: float = 40,
               y: float = 40,
               width: float = 400,
               height: float = 300,
               visual_id: str | None = None) -> dict:
    """Add a single visual to an existing page in a PBIP report.

    Args:
        pbip_dir: Path to an existing PBIP project folder.
        page_id: id of the existing page (the folder name under pages/).
        visual_type: PBIR visual type (card, barChart, table, slicer, ...).
        query_state: Visual data binding — accepts either the simplified
            ``{"select":[...]}`` form or the full role-projection form.
        title: Optional visual title.
        x, y, width, height: Geometry (px).
        visual_id: Optional explicit visual id; otherwise generated.
    """
    try:
        from mcp_server.server import PbipToolbox

        project_name, project_root = _resolve_project(pbip_dir)
        toolbox = PbipToolbox(project_root)
        rep_def = f"{project_name}.Report/definition"

        # check page exists
        page_dir = project_root / f"{project_name}.Report" / "definition" / "pages" / page_id
        if not page_dir.is_dir():
            return _result(False, "add_visual",
                           f"Page '{page_id}' not found at {page_dir}",
                           errors=[f"missing page: {page_id}"])

        # Validate query_state's shape explicitly. Without this, a malformed
        # value (e.g. a caller accidentally serializing it to a string) fails
        # deep inside _normalize_query_state with an opaque
        # "'str' object has no attribute 'get'"/"'...' has no attribute
        # 'values'" AttributeError — a real, observed failure mode that gives
        # a calling agent no actionable signal to self-correct from.
        if not isinstance(query_state, dict):
            return _result(
                False, "add_visual",
                f"query_state must be a JSON object (dict), got "
                f"{type(query_state).__name__}: {query_state!r}. Expected e.g. "
                '{"select": [{"kind": "measure", "table": "T", "name": "Total Sales"}]}.',
                errors=[f"query_state must be a dict, got {type(query_state).__name__}"],
            )

        vid = visual_id or stable_uuid()
        visual = {
            "id": vid,
            "visualType": visual_type,
            "queryState": query_state,
            "x": x, "y": y, "width": width, "height": height,
            "z": 0, "tabOrder": 0,
        }
        if title:
            visual["title"] = title

        # we want to ADD a visual without rewriting the page.json. Easiest
        # path: render via PbipToolbox.write_pbir_page on a synthetic page_def
        # that contains only this one visual? That would overwrite the page.
        # Instead, write the visual.json directly using pbir_generator —
        # mirroring what write_pbir_page does for a single visual.
        from mcp_server import pbir_generator as _pb
        from utils import atomic_write_json
        ensure_dir(page_dir / "visuals" / vid)
        # Derive the SemanticModel definition path from the Report definition
        # path by swapping ONLY the trailing ".Report/" folder suffix, NOT a
        # naive str.replace(".Report", ...) which corrupts project names that
        # themselves contain ".Report" (e.g. "My.Report.Sales").
        sm_def = rep_def.rsplit(".Report/", 1)
        sm_def = sm_def[0] + ".SemanticModel/" + sm_def[1] if len(sm_def) == 2 else rep_def
        data_table = toolbox._resolve_data_table(sm_def)
        qs = toolbox._normalize_query_state(visual_type, query_state, data_table)
        payload = _pb.visual_json(
            visual_id=vid,
            visual_type=visual_type,
            query_state=qs,
            x=float(x), y=float(y), width=float(width), height=float(height),
            z=0, tab_order=0, title=title,
        )
        vpath = page_dir / "visuals" / vid / "visual.json"
        atomic_write_json(vpath, payload)

        return _result(
            True, "add_visual",
            f"Added '{visual_type}' visual to page '{page_id}'.",
            data={
                "project_name": project_name,
                "pbip_root": str(project_root),
                "page_id": page_id,
                "visual_id": vid,
                "visual_path": str(vpath),
            },
        )
    except Exception as exc:
        log.exception("[add_visual] failed")
        return _result(False, "add_visual", str(exc), errors=[str(exc)])


# ---------------------------------------------------------------------------
# add_page
# ---------------------------------------------------------------------------


def add_page(pbip_dir: str,
             display_name: str,
             *,
             visuals: list[dict] | None = None,
             width: int = 1280,
             height: int = 720,
             page_id: str | None = None,
             auto_layout: bool = True) -> dict:
    """Add a new page to an existing PBIP report and update pages.json.

    Args:
        pbip_dir: Path to an existing PBIP project folder.
        display_name: Display name for the new page.
        visuals: Optional list of visual definitions (same shape as
            write_pbir_page's ``page_def["visuals"]``). If None or empty,
            the page is created empty.
        width, height: Page dimensions (px).
        page_id: Optional explicit page id; otherwise generated.
        auto_layout: When True (default) and visuals are supplied, re-compute
            non-overlapping zone-based positions for them before writing, so
            the page lands clean. Set False to honour caller-supplied geometry.
    """
    try:
        from mcp_server.server import PbipToolbox
        from utils import atomic_write_json
        from utils import pbip_paths as paths
        from mcp_server import pbir_generator as _pb

        project_name, project_root = _resolve_project(pbip_dir)
        toolbox = PbipToolbox(project_root)
        rep_def = f"{project_name}.Report/definition"

        # merge into pages.json: read existing order first (needed for slug collision check)
        rep_root = paths.report_root(project_root, project_name)
        pages_meta = paths.report_pages_metadata(rep_root)
        existing_ids: list[str] = []
        if pages_meta.exists():
            try:
                pdata = json.loads(pages_meta.read_text(encoding="utf-8"))
                existing_ids = list(pdata.get("pageOrder", []))
            except (json.JSONDecodeError, OSError):
                existing_ids = []

        # Generate a readable page id slug from the display name (unless
        # an explicit page_id is given).  Avoid collisions with existing pages.
        if page_id:
            pid = page_id
        else:
            base = _safe_slug(display_name)
            pid = base
            suffix = 2
            while pid in existing_ids:
                pid = f"{base}-{suffix}"
                suffix += 1

        final_visuals = visuals or []
        if auto_layout and final_visuals:
            # Stamp smart zone-based positions so a freshly added page has no
            # overlaps, regardless of what geometry the caller passed.
            final_visuals = _layout_visuals(final_visuals, width, height)

        page_def = {
            "id": pid,
            "displayName": display_name,
            "width": width,
            "height": height,
            "visuals": final_visuals,
        }
        write_res = toolbox.write_pbir_page(rep_def, page_def)
        if not write_res.ok:
            return _result(False, "add_page", write_res.message,
                           errors=write_res.errors)

        if pid not in existing_ids:
            existing_ids.append(pid)
        atomic_write_json(pages_meta, _pb.pages_metadata(existing_ids))

        return _result(
            True, "add_page",
            f"Added page '{display_name}' ({len(final_visuals)} visuals).",
            data={
                "project_name": project_name,
                "pbip_root": str(project_root),
                "page_id": pid,
                "display_name": display_name,
                "visual_count": len(final_visuals),
                "page_order": existing_ids,
                "auto_layout": auto_layout,
            },
        )
    except Exception as exc:
        log.exception("[add_page] failed")
        return _result(False, "add_page", str(exc), errors=[str(exc)])


# ---------------------------------------------------------------------------
# build_report — multi-page rich report with diverse visual types
# ---------------------------------------------------------------------------


def build_report(pbip_dir: str,
                 *,
                 num_pages: int = 3,
                 visual_variety: str = "all",
                 description: str = "") -> dict:
    """Add multiple pages with diverse visual types to an existing PBIP project.

    Unlike ``generate_pbip`` (which always creates 1 summary page with up to
    6 visuals), this tool inspects the existing schema + measures and builds
    a rich multi-page report covering all available visual types (card, bar,
    column, line, pie, donut, scatter, matrix, kpi, slicer, table).

    Args:
        pbip_dir: Path to an existing PBIP project folder (must already have
            a semantic model with tables + measures).
        num_pages: Number of additional pages to create (default 3). The
            original summary page is kept.
        visual_variety: ``"all"`` (use every visual type that fits the schema)
            or ``"standard"`` (cards + bar/column/line + table only).
        description: Optional business description for page naming hints.
    """
    try:
        from mcp_server.server import PbipToolbox
        from mcp_server import pbir_generator as _pb
        from agents.dax_agent import _classify_columns
        from utils.tmdl_parser import read_semantic_model

        # Validate parameters
        if num_pages < 1:
            return _result(False, "build_report",
                           "num_pages must be >= 1.",
                           errors=[f"num_pages={num_pages}"])
        visual_variety = (visual_variety or "all").lower().strip()
        if visual_variety not in ("all", "standard"):
            return _result(False, "build_report",
                           f"visual_variety must be 'all' or 'standard', got '{visual_variety}'.",
                           errors=[f"invalid visual_variety: {visual_variety}"])

        project_name, project_root = _resolve_project(pbip_dir)

        # Read schema + measures from the existing semantic model
        sm_dir = next(project_root.glob("*.SemanticModel"), None)
        if sm_dir is None:
            return _result(False, "build_report",
                           "No .SemanticModel folder found.",
                           errors=["not a PBIP project"])
        model = read_semantic_model(sm_dir)
        table = model["primary_table"]
        columns = model["all_columns"]
        measure_names = list(model.get("measure_names", []))
        if not measure_names:
            return _result(False, "build_report",
                           "No measures found in the semantic model. "
                           "Run generate_pbip first or add measures.",
                           errors=["no measures"])

        buckets = _classify_columns(columns)
        amount_measure = _pick_amount_measure(measure_names)
        amount_col = buckets["amount"][0]["name"] if buckets["amount"] else None
        date_col = buckets["date"][0]["name"] if buckets["date"] else None
        region_col = buckets["region"][0]["name"] if buckets["region"] else None
        category_col = buckets["category"][0]["name"] if buckets["category"] else None

        # Build page definitions
        page_defs = _plan_rich_pages(
            table=table,
            buckets=buckets,
            measure_names=measure_names,
            amount_measure=amount_measure,
            amount_col=amount_col,
            date_col=date_col,
            region_col=region_col,
            category_col=category_col,
            num_pages=num_pages,
            visual_variety=visual_variety,
            description=description,
        )

        pages_added: list[dict] = []
        for pdef in page_defs:
            res = add_page(
                pbip_dir=str(project_root),
                display_name=pdef["display_name"],
                visuals=pdef["visuals"],
                width=pdef.get("width", 1280),
                height=pdef.get("height", 720),
            )
            if res.get("ok"):
                pages_added.append({
                    "page_id": res["data"]["page_id"],
                    "display_name": res["data"]["display_name"],
                    "visual_count": res["data"]["visual_count"],
                })
            else:
                log.warning("[build_report] add_page failed: %s", res.get("message"))

        total_visuals = sum(p["visual_count"] for p in pages_added)

        # Fail if all page additions failed (don't report success with 0 pages)
        if not pages_added:
            return _result(
                False, "build_report",
                "All page additions failed — no pages were added.",
                data={"project_name": project_name, "pbip_root": str(project_root)},
                errors=["all add_page calls failed"],
            )

        # Validate the final project
        toolbox = PbipToolbox(project_root)
        val = toolbox.validate_pbip_structure(str(project_root))

        return _result(
            True, "build_report",
            f"Added {len(pages_added)} pages with {total_visuals} visuals "
            f"(visual types: {visual_variety}).",
            data={
                "project_name": project_name,
                "pbip_root": str(project_root),
                "pages_added": pages_added,
                "total_visuals": total_visuals,
                "validation": {"ok": val.ok, "errors": val.errors} if val else None,
            },
        )
    except Exception as exc:
        log.exception("[build_report] failed")
        return _result(False, "build_report", str(exc), errors=[str(exc)])


def _pick_amount_measure(measure_names: list[str]) -> str:
    """Prefer a Total <something> measure, else the first measure."""
    for m in measure_names:
        if m.lower().startswith("total "):
            return m
    return measure_names[0] if measure_names else "Total Sales"


def _make_visual_def(vid: str, vtype: str, x: float, y: float,
                     w: float, h: float, tab: int, query_state: dict,
                     title: str | None = None) -> dict:
    """Build a visual dict in the shape expected by add_page's visuals list."""
    v: dict = {
        "id": vid,
        "visualType": vtype,
        "queryState": query_state,
        "x": x, "y": y, "width": w, "height": h,
        "z": 0, "tabOrder": tab,
    }
    if title:
        v["title"] = title
    return v


def _simplified_select(items: list[dict]) -> dict:
    """Build a simplified queryState ``{"select": [...]}``.

    Each item: ``{"kind": "column"|"measure", "name": ..., "table": ...,
    optional "role": ...}``
    """
    return {"select": items}


def _plan_rich_pages(
    *,
    table: str,
    buckets: dict,
    measure_names: list[str],
    amount_measure: str,
    amount_col: str | None,
    date_col: str | None,
    region_col: str | None,
    category_col: str | None,
    num_pages: int,
    visual_variety: str,
    description: str,
) -> list[dict]:
    """Plan ``num_pages`` pages with diverse visuals spread across them.

    Returns a list of ``{"display_name", "visuals": [...]}`` dicts.
    """
    # Build the full pool of visual plans (each is a visual dict ready for add_page)
    pool: list[tuple[str, dict]] = []  # (suggested_page_topic, visual_def)

    # --- Standard visuals (always included) ---
    pool.append(("Overview", _make_visual_def(
        "card-total", "card", 10, 10, 200, 120, 0,
        _simplified_select([{"kind": "measure", "name": amount_measure, "table": table, "role": "Values"}]),
        title=amount_measure,
    )))

    if region_col and amount_col:
        pool.append(("Overview", _make_visual_def(
            "bar-region", "barChart", 10, 140, 400, 300, 1,
            _simplified_select([
                {"kind": "column", "name": region_col, "table": table, "role": "Category"},
                {"kind": "measure", "name": amount_measure, "table": table, "role": "Y"},
            ]),
            title=f"{amount_measure} by {region_col}",
        )))

    if category_col and amount_col:
        pool.append(("Overview", _make_visual_def(
            "column-category", "columnChart", 420, 140, 400, 300, 2,
            _simplified_select([
                {"kind": "column", "name": category_col, "table": table, "role": "Category"},
                {"kind": "measure", "name": amount_measure, "table": table, "role": "Y"},
            ]),
            title=f"{amount_measure} by {category_col}",
        )))

    if date_col and amount_col:
        pool.append(("Trends", _make_visual_def(
            "line-date", "lineChart", 10, 10, 600, 350, 0,
            _simplified_select([
                {"kind": "column", "name": date_col, "table": table, "role": "Category"},
                {"kind": "measure", "name": amount_measure, "table": table, "role": "Y"},
            ]),
            title=f"{amount_measure} over time",
        )))

    # --- Rich visual types (when visual_variety == "all") ---
    if visual_variety == "all":
        if region_col and amount_col:
            pool.append(("Breakdown", _make_visual_def(
                "pie-region", "pieChart", 10, 10, 350, 300, 0,
                _simplified_select([
                    {"kind": "column", "name": region_col, "table": table, "role": "Category"},
                    {"kind": "measure", "name": amount_measure, "table": table, "role": "Y"},
                ]),
                title=f"{amount_measure} share by {region_col}",
            )))
            pool.append(("Breakdown", _make_visual_def(
                "donut-region", "donutChart", 370, 10, 350, 300, 1,
                _simplified_select([
                    {"kind": "column", "name": region_col, "table": table, "role": "Category"},
                    {"kind": "measure", "name": amount_measure, "table": table, "role": "Y"},
                ]),
                title=f"{amount_measure} by {region_col} (donut)",
            )))
            pool.append(("Breakdown", _make_visual_def(
                "slicer-region", "slicer", 730, 10, 200, 300, 2,
                _simplified_select([
                    {"kind": "column", "name": region_col, "table": table, "role": "Values"},
                ]),
                title=f"Filter by {region_col}",
            )))

        if category_col and region_col and amount_col:
            pool.append(("Breakdown", _make_visual_def(
                "matrix-cat-region", "matrix", 10, 320, 500, 300, 3,
                {
                    "Rows": [{"kind": "column", "name": category_col, "table": table}],
                    "Values": [{"kind": "measure", "name": amount_measure, "table": table}],
                    "Columns": [{"kind": "column", "name": region_col, "table": table}],
                },
                title=f"{amount_measure}: {category_col} × {region_col}",
            )))

        # Scatter: needs two numeric measures or two amount-like columns
        if len(measure_names) >= 2:
            pool.append(("Analysis", _make_visual_def(
                "scatter-sales-profit", "scatterChart", 10, 10, 500, 350, 0,
                _simplified_select([
                    {"kind": "measure", "name": measure_names[0], "table": table, "role": "X"},
                    {"kind": "measure", "name": measure_names[1], "table": table, "role": "Y"},
                    {"kind": "column", "name": category_col or region_col or "Product", "table": table, "role": "Category"},
                ]),
                title=f"{measure_names[0]} vs {measure_names[1]}",
            )))

        # KPI: needs a measure + ideally a target-like measure
        if len(measure_names) >= 2:
            pool.append(("Analysis", _make_visual_def(
                "kpi-total", "kpi", 520, 10, 350, 200, 1,
                {
                    "Indicator": [{"kind": "measure", "name": measure_names[0], "table": table}],
                    "Goal": [{"kind": "measure", "name": measure_names[1], "table": table}],
                },
                title=f"KPI: {measure_names[0]}",
            )))

    # --- Table on a details page ---
    table_cols: list[str] = []
    for key in ("date", "region", "category", "amount", "qty"):
        for col in buckets.get(key, []):
            if col["name"] not in table_cols:
                table_cols.append(col["name"])
            if len(table_cols) >= 5:
                break
        if len(table_cols) >= 5:
            break
    if table_cols:
        select_items = [{"kind": "column", "name": c, "table": table} for c in table_cols]
        select_items.append({"kind": "measure", "name": amount_measure, "table": table})
        pool.append(("Details", _make_visual_def(
            "table-details", "tableEx", 10, 10, 1200, 400, 0,
            _simplified_select(select_items),
            title="Detail table",
        )))

    # Distribute pool visuals across num_pages pages by topic
    topics_order = ["Overview", "Trends", "Breakdown", "Analysis", "Details"]
    topic_to_visuals: dict[str, list[dict]] = {}
    for topic, vdef in pool:
        topic_to_visuals.setdefault(topic, []).append(vdef)

    # Build pages: assign topics to pages, spread visuals in a grid
    pages: list[dict] = []
    topics = [t for t in topics_order if t in topic_to_visuals]
    # If more pages than topics, repeat remaining topics
    for i in range(num_pages):
        topic = topics[i % len(topics)] if topics else f"Page {i+1}"
        visuals = topic_to_visuals.get(topic, [])
        if not visuals:
            continue
        # Re-layout visuals on a 1280×720 grid
        layouted = _layout_visuals(visuals, 1280, 720)
        pages.append({
            "display_name": topic,
            "visuals": layouted,
            "width": 1280,
            "height": 720,
        })

    return pages[:num_pages]


def _layout_visuals(visuals: list[dict], page_w: int, page_h: int) -> list[dict]:
    """Re-assign x/y/width/height using the smart zone-based layout engine.

    Delegates to ``utils.layout_engine.build_layout`` (cards → top strip,
    slicers → right column, charts → centre, tables → bottom) instead of the
    old uniform grid. Falls back to identity if a visual lacks an ``id``.
    """
    from utils.layout_engine import build_layout

    if not visuals:
        return visuals
    specs = [{"id": v["id"], "type": v.get("visualType", "card")} for v in visuals]
    positions = build_layout(specs, page_w, page_h)
    result = []
    for i, v in enumerate(visuals):
        v2 = dict(v)
        pos = positions.get(v.get("id"))
        if pos:
            v2["x"] = float(pos["x"])
            v2["y"] = float(pos["y"])
            v2["width"] = float(pos["width"])
            v2["height"] = float(pos["height"])
            v2["z"] = int(pos["z"])
            v2["tabOrder"] = int(pos["tabOrder"])
        else:
            v2.setdefault("x", 40.0)
            v2.setdefault("y", 40.0)
            v2.setdefault("width", 400.0)
            v2.setdefault("height", 300.0)
            v2.setdefault("z", 0)
            v2.setdefault("tabOrder", i)
        result.append(v2)
    return result


# ---------------------------------------------------------------------------
# deploy_to_fabric
# ---------------------------------------------------------------------------


def deploy_to_fabric(pbip_dir: str,
                     workspace: str,
                     *,
                     mode: str = "auto",
                     dry_run: bool = True,
                     skip_report: bool = False,
                     skip_model: bool = False) -> dict:
    """Upload a PBIP to a Microsoft Fabric workspace.

    Defaults to ``dry_run=True`` so an agent loop cannot accidentally publish.

    Args:
        pbip_dir: Path to an existing PBIP project folder.
        workspace: Target Fabric workspace name.
        mode: "auto" | "create" | "update" (default auto).
        dry_run: When True, prints the fab commands but does not invoke them.
        skip_report: Skip the Report import (model only).
        skip_model: Skip the SemanticModel import (report only — rarely useful).
    """
    try:
        from fabric.deploy import deploy as _fab_deploy

        result = _fab_deploy(
            pbip_dir=pbip_dir,
            workspace=workspace,
            mode=mode,
            dry_run=dry_run,
            skip_report=skip_report,
            skip_model=skip_model,
        )
        return _result(
            result.ok,
            "deploy_to_fabric",
            "Dry-run complete — no network calls were made." if result.dry_run
            else ("Deploy succeeded." if result.ok else f"Deploy failed: {result.error}"),
            data={
                "workspace": result.workspace,
                "mode": result.mode,
                "dry_run": result.dry_run,
                # result.pbip is a dict[str, str] (the resolved PBIP layout);
                # pass it through as structured data, not a Python repr.
                "pbip": dict(result.pbip) if result.pbip else None,
                "actions": result.actions,
            },
            errors=[result.error] if result.error else [],
        )
    except Exception as exc:
        log.exception("[deploy_to_fabric] failed")
        return _result(False, "deploy_to_fabric", str(exc), errors=[str(exc)])


# ---------------------------------------------------------------------------
# suggest_measures
# ---------------------------------------------------------------------------


def suggest_measures(source: str,
                     *,
                     base_table: str | None = None,
                     pattern_types: list[str] | None = None,
                     base_name: str | None = None,
                     base_expr: str | None = None,
                     date_col: str = "'Date'[Date]") -> dict:
    """Suggest DAX measures for a CSV / Excel file OR an existing PBIP.

    Two modes:
      * **Auto** (default): runs ``auto_suggest_measures`` against the schema
        derived from ``source``. Use this to bootstrap a fresh model.
      * **Pattern-driven**: if ``pattern_types`` is provided, runs
        ``suggest_dax_measures`` over the pattern library — e.g.
        ``pattern_types=["ytd","yoy_pct"], base_name="Total Sales"``.

    Args:
        source: Path to a CSV / Excel / JSON / PBIP folder.
        base_table: Table name override (auto-detected from source otherwise).
        pattern_types: Optional pattern keys (see ``list_dax_patterns``).
        base_name: Base measure name for pattern-driven mode (required when
            ``pattern_types`` is set).
        base_expr: Base measure DAX (e.g. ``"[Total Sales]"``); derived if missing.
        date_col: Date column reference for time patterns.
    """
    try:
        from adk.tools.schema_dax_tools import auto_suggest_measures
        from adk.tools.dax_pattern_tools import suggest_dax_measures
        from mcp_server.schema_inference import (
            infer_csv_schema, infer_excel_schema_compat, infer_json_schema
        )
        from utils.tmdl_parser import read_semantic_model

        p = Path(source).expanduser().resolve()
        existing_measures: list[str] = []
        schema: dict | None = None

        if p.is_dir():
            # PBIP project: read schema from TMDL
            sm_dirs = list(p.glob("*.SemanticModel"))
            if not sm_dirs:
                return _result(False, "suggest_measures",
                               f"No *.SemanticModel folder under {p}",
                               errors=["not a PBIP project"])
            model = read_semantic_model(sm_dirs[0])
            schema = {
                "table_name": base_table or model["primary_table"],
                "columns": model["all_columns"],
            }
            existing_measures = list(model.get("measure_names", []))
        elif p.is_file():
            ext = p.suffix.lower()
            if ext == ".csv":
                schema = infer_csv_schema(p)
            elif ext == ".json":
                schema = infer_json_schema(p)
            elif ext in {".xlsx", ".xls", ".xlsm", ".xlsb"}:
                schema = infer_excel_schema_compat(p)
            else:
                return _result(False, "suggest_measures",
                               f"Unsupported source type: {ext}",
                               errors=[f"unsupported: {ext}"])
            if base_table:
                schema = dict(schema)
                schema["table_name"] = base_table
        else:
            return _result(False, "suggest_measures",
                           f"Source not found: {p}",
                           errors=[f"missing: {p}"])

        if pattern_types:
            if not base_name:
                return _result(False, "suggest_measures",
                               "pattern_types requires base_name",
                               errors=["missing: base_name"])
            tbl = schema["table_name"]
            be = base_expr or f"[{base_name}]"
            patt = suggest_dax_measures(
                pattern_types=pattern_types,
                base_name=base_name,
                base_expr=be,
                table=tbl,
                date_col=date_col,
            )
            return _result(
                True, "suggest_measures",
                f"Generated {patt['count']} measures from "
                f"{len(pattern_types)} patterns.",
                data={
                    "mode": "pattern",
                    "table": tbl,
                    "measures": patt["measures"],
                    "skipped_patterns": patt["skipped"],
                },
            )

        auto = auto_suggest_measures(schema, existing_measures=existing_measures)
        return _result(
            True, "suggest_measures",
            f"Generated {auto['count']} measures "
            f"(skipped {auto['skipped']} duplicates).",
            data={
                "mode": "auto",
                "table": schema["table_name"],
                "measures": auto["measures"],
                "folders": auto["folders"],
                "skipped": auto["skipped"],
            },
        )
    except Exception as exc:
        log.exception("[suggest_measures] failed")
        return _result(False, "suggest_measures", str(exc), errors=[str(exc)])


__all__ = [
    "generate_pbip",
    "edit_pbip",
    "add_measure",
    "add_visual",
    "add_page",
    "deploy_to_fabric",
    "suggest_measures",
]

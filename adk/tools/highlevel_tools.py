"""High-level orchestration tools for Google ADK — Phase 7.

These tools wrap the MCP high-level operations into single-call ADK FunctionTool
functions so the ADK agent can ``generate_pbip``, ``edit_pbip``, ``add_measure``,
``add_visual``, ``add_page``, ``suggest_measures``, and ``deploy_to_fabric``
without orchestrating the multi-step pipeline manually.

Every tool follows the same pattern:
  1. Delegate to ``mcp_server.highlevel`` (except ``generate_pbip``, which
     routes through a real MCP client -> subprocess-server round trip via
     ``adk/mcp_client.py`` -- see that module's docstring for why)
  2. Return a plain dict with ok / message / data / errors

Usage in agent.py::

    from adk.tools.highlevel_tools import (
        generate_pbip, edit_pbip, add_measure, add_visual,
        add_page, suggest_measures, deploy_to_fabric,
    )
    root_agent = Agent(tools=[..., generate_pbip, edit_pbip, ...])
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_server import highlevel as _hl  # noqa: E402

# ---------------------------------------------------------------------------
# generate_pbip
# ---------------------------------------------------------------------------


async def generate_pbip(
    source: str,
    description: str,
    project_name: str = "",
    output_root: str = "./output",
    theme_preset: str = "default",
    sheet: str = "",
    source_artifact: str = "",
    num_pages: int = 0,
    visual_variety: str = "",
    tool_context: Any = None,
) -> dict[str, Any]:
    """Build a complete PBIP project from a CSV / Excel / JSON file in one call.

    Drives the full OrchestratorAgent pipeline (Schema → DAX → Report → Validate).

    You can provide the input **either** as a filesystem path (``source``)
    **or** as an artifact name (``source_artifact``) from a prior ``/upload``
    or browser file-attach.  If ``source_artifact`` is set, the tool loads
    the artifact bytes from the ``ArtifactService`` (via ``tool_context`` when
    available) and writes them to a temp file under ``output_root/_uploads/``,
    and uses that as the source — so the user never needs to type a path.

    Args:
        source: Path to the input file (.csv / .xlsx / .json).  Ignored if
            ``source_artifact`` is set.
        source_artifact: Name of an uploaded artifact (e.g. ``"user:data.csv"``).
            When set, takes priority over ``source``.
        description: Plain-English description of what to build.
        project_name: Optional override for the auto-derived name.
        output_root: Where to write the .pbip folder (default: ./output).
        theme_preset: Theme preset — default | corporate_blue | modern_dark | earth_tones | vibrant.
        sheet: Excel sheet name (default: first sheet).
        num_pages: If the user states an exact page count, decide it up
            front and pass it here — this ONE call produces exactly that
            many pages. Do not rely on description keywords alone and then
            call ``build_report`` to add more afterward: ``build_report``
            ADDS pages on top of this one's output, and routinely
            overshoots whatever total the user actually asked for. Only
            reach for ``build_report``/``add_page`` for a genuine follow-up
            edit request made *after* the user has seen this result.
        visual_variety: "" (default) or "all" to include scatter/pie/kpi
            visuals directly in this call, matching what a separate
            ``build_report(visual_variety="all")`` follow-up would add —
            decide this up front instead of building once, reviewing, and
            calling build_report to top up the variety.

    Returns:
        dict with ``ok``, ``message``, ``data`` (project_name, pbip_root,
        validation, metadata, steps), ``errors``.
    """
    # If an artifact name is provided, resolve it to bytes and materialize a
    # local file so the orchestrator (which reads from a path) can consume it.
    if source_artifact:
        from pathlib import Path
        upload_dir = Path(output_root).expanduser().resolve() / "_uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        fname = source_artifact.removeprefix("user:")
        candidate = upload_dir / fname

        # Preferred path: read the artifact from the ArtifactService via the
        # tool context (works in distributed / non-filesystem setups). The
        # plugin's on_user_message_callback materializes uploads to _uploads/
        # too, so the disk fallback below covers the case where tool_context
        # is unavailable (e.g. direct in-process calls, tests).
        artifact_loaded = False
        if tool_context is not None:
            try:
                part = tool_context.load_artifact(filename=source_artifact)
                data = getattr(part, "inline_data", None)
                blob = getattr(data, "data", None) if data is not None else None
                if blob:
                    candidate.write_bytes(blob)
                    artifact_loaded = True
            except Exception:
                pass  # fall through to disk lookup

        # Fallback: the plugin already wrote the upload to _uploads/<name>.
        if not artifact_loaded and candidate.is_file():
            artifact_loaded = True

        if artifact_loaded:
            source = str(candidate)
        else:
            return {
                "ok": False,
                "tool": "generate_pbip",
                "message": (f"Artifact '{source_artifact}' not found. Use /upload "
                            f"to save it, or ensure tool_context has access to the "
                            f"ArtifactService."),
                "data": {},
                "errors": [f"artifact not materialized: {source_artifact}"],
            }
    # Real MCP round-trip: the same generate_pbip that mcp_server/server.py
    # exposes over stdio, reached here via a persistent client session
    # (adk/mcp_client.py) instead of the in-process _hl import used
    # elsewhere in this file. See adk/mcp_client.py's module docstring for
    # why this one tool goes through MCP while its siblings don't yet.
    from adk.mcp_client import call_mcp_tool
    return await call_mcp_tool(
        "generate_pbip",
        source=source,
        description=description,
        project_name=project_name,
        output_root=output_root,
        theme_preset=theme_preset,
        sheet=sheet,
        num_pages=num_pages,
        visual_variety=visual_variety,
    )


# ---------------------------------------------------------------------------
# edit_pbip
# ---------------------------------------------------------------------------


def edit_pbip(
    pbip_dir: str,
    description: str,
    output_root: str = "",
    theme_preset: str = "default",
) -> dict[str, Any]:
    """Edit an existing PBIP — add measures / pages from a plain-English description.

    Copies the source PBIP into ``output_root`` then runs the edit pipeline
    (ReadPBIP → DAX → Report → Validate). The original is left untouched.

    Args:
        pbip_dir: Path to an existing PBIP project folder.
        description: Edits to apply (e.g. ``"add YoY %% and a trend page"``).
        output_root: Where to write the edited copy (default: parent of pbip_dir).
        theme_preset: Theme preset key.

    Returns:
        dict with ``ok``, ``message``, ``data``, ``errors``.
    """
    return _hl.edit_pbip(
        pbip_dir=pbip_dir,
        description=description,
        output_root=output_root or None,
        theme_preset=theme_preset,
    )


# ---------------------------------------------------------------------------
# add_measure
# ---------------------------------------------------------------------------


def add_measure(
    pbip_dir: str,
    name: str,
    expression: str,
    table: str = "",
    format_string: str = "",
    display_folder: str = "",
) -> dict[str, Any]:
    """Append a single DAX measure to a PBIP semantic model.

    Args:
        pbip_dir: Path to an existing PBIP project folder.
        name: Measure name (e.g. ``"Total Sales"``).
        expression: DAX expression (e.g. ``"SUM('Sales'[Amount])"``).
        table: Target table name. Auto-detected from the first data table if empty.
        format_string: Optional TMDL formatString (e.g. ``"#,0.00"``).
        display_folder: Optional display folder.

    Returns:
        dict with ``ok`` and ``data`` containing ``measure_name`` and ``table``.
    """
    return _hl.add_measure(
        pbip_dir=pbip_dir,
        name=name,
        expression=expression,
        table=table or None,
        format_string=format_string or None,
        display_folder=display_folder or None,
    )


# ---------------------------------------------------------------------------
# add_visual
# ---------------------------------------------------------------------------


def add_visual(
    pbip_dir: str,
    page_id: str,
    visual_type: str,
    query_state: dict,
    title: str = "",
    x: float = 40,
    y: float = 40,
    width: float = 400,
    height: float = 300,
    visual_id: str = "",
) -> dict[str, Any]:
    """Add a single visual to an existing page in a PBIP report.

    Args:
        pbip_dir: Path to an existing PBIP project folder.
        page_id: id of an existing page (the ``pages/<id>`` folder name).
        visual_type: PBIR visual type — ``card``, ``barChart``, ``columnChart``,
            ``lineChart``, ``tableEx``, ``matrix``, ``kpi``, ``slicer``,
            ``donutChart``, ``scatterChart``, etc.
        query_state: Visual data binding — accepts either the simplified
            ``{"select": [...]}`` form or the full role-projection form.
        title: Optional visual title.
        x, y, width, height: Geometry in pixels.
        visual_id: Optional explicit id; otherwise auto-generated.

    Returns:
        dict with ``ok`` and ``data`` containing ``page_id``, ``visual_id``.
    """
    return _hl.add_visual(
        pbip_dir=pbip_dir,
        page_id=page_id,
        visual_type=visual_type,
        query_state=query_state,
        title=title or None,
        x=x,
        y=y,
        width=width,
        height=height,
        visual_id=visual_id or None,
    )


# ---------------------------------------------------------------------------
# add_page
# ---------------------------------------------------------------------------


def add_page(
    pbip_dir: str,
    display_name: str,
    visuals: list | None = None,
    width: int = 1280,
    height: int = 720,
    page_id: str = "",
    auto_layout: bool = True,
) -> dict[str, Any]:
    """Add a new page to an existing PBIP report and update pages.json.

    Args:
        pbip_dir: Path to an existing PBIP project folder.
        display_name: Display name for the new page.
        visuals: Optional list of visual definitions (same shape as
            ``write_pbir_page``'s ``page_def["visuals"]``).
        width, height: Page dimensions in pixels.
        page_id: Optional explicit page id; otherwise generated.
        auto_layout: When True (default), visuals are auto-positioned with the
            smart zone-based layout engine so the new page has no overlaps.
            Set False to honour caller-supplied geometry.

    Returns:
        dict with ``ok`` and ``data`` containing ``page_id``, ``display_name``.
    """
    return _hl.add_page(
        pbip_dir=pbip_dir,
        display_name=display_name,
        visuals=visuals or [],
        width=width,
        height=height,
        page_id=page_id or None,
        auto_layout=auto_layout,
    )


# ---------------------------------------------------------------------------
# suggest_measures
# ---------------------------------------------------------------------------


def suggest_measures(
    source: str,
    base_table: str = "",
    pattern_types: list | None = None,
    base_name: str = "",
    base_expr: str = "",
    date_col: str = "'Date'[Date]",
) -> dict[str, Any]:
    """Propose DAX measures for a CSV / Excel / JSON / PBIP project.

    Two modes:
        * **Auto** (default): deterministic schema-driven suggestions.
        * **Pattern-driven**: pass ``pattern_types`` (e.g. ``["ytd", "yoy_pct"]``)
          plus ``base_name`` to materialise measures from the pattern library.

    Args:
        source: Path to a CSV / Excel / JSON file OR an existing PBIP folder.
        base_table: Table name override.
        pattern_types: Optional pattern keys (see ``list_dax_patterns``).
        base_name: Required when ``pattern_types`` is set.
        base_expr: Base measure DAX (default ``[<base_name>]``).
        date_col: Date column for time patterns.

    Returns:
        dict with ``ok`` and ``data`` containing ``measures``.
    """
    return _hl.suggest_measures(
        source=source,
        base_table=base_table or None,
        pattern_types=pattern_types,
        base_name=base_name or None,
        base_expr=base_expr or None,
        date_col=date_col,
    )


# ---------------------------------------------------------------------------
# deploy_to_fabric
# ---------------------------------------------------------------------------


def deploy_to_fabric(
    pbip_dir: str,
    workspace: str,
    mode: str = "auto",
    dry_run: bool = True,
    skip_report: bool = False,
    skip_model: bool = False,
) -> dict[str, Any]:
    """Upload a PBIP to a Microsoft Fabric workspace via the ``fab`` CLI.

    Defaults to ``dry_run=True`` for safety — agent loops must explicitly
    opt in to real network calls.

    Args:
        pbip_dir: Path to an existing PBIP project folder.
        workspace: Target Fabric workspace name.
        mode: ``"auto"`` | ``"create"`` | ``"update"`` (default auto).
        dry_run: ``True`` = print fab commands, do not execute.
        skip_report: Skip the Report import (model only).
        skip_model: Skip the SemanticModel import (rare).

    Returns:
        dict with ``ok``, ``data`` (workspace, mode, dry_run, actions).
    """
    return _hl.deploy_to_fabric(
        pbip_dir=pbip_dir,
        workspace=workspace,
        mode=mode,
        dry_run=dry_run,
        skip_report=skip_report,
        skip_model=skip_model,
    )


# ---------------------------------------------------------------------------
# build_report — multi-page rich report with diverse visual types (Phase 8)
# ---------------------------------------------------------------------------


def build_report(
    pbip_dir: str,
    num_pages: int = 3,
    visual_variety: str = "all",
    description: str = "",
) -> dict[str, Any]:
    """Add multiple pages with diverse visual types to an existing PBIP project.

    Unlike ``generate_pbip`` (which always creates 1 summary page with up to
    6 visuals), this tool inspects the existing schema + measures and builds
    a rich multi-page report covering all available visual types (card, bar,
    column, line, pie, donut, scatter, matrix, kpi, slicer, table).

    **Use this after ``generate_pbip``** when the user asks for multiple pages,
    rich/detailed reports, or "all visual types". The original summary page
    is preserved; new pages are added on top.

    Args:
        pbip_dir: Path to an existing PBIP project folder (must already have
            a semantic model with tables + measures — run generate_pbip first).
        num_pages: Number of additional pages to create (default 3).
        visual_variety: ``"all"`` (every visual type that fits the schema:
            pie, donut, scatter, matrix, kpi, slicer, etc.) or ``"standard"``
            (cards + bar/column/line + table only).
        description: Optional business description for page naming hints.

    Returns:
        dict with ``ok``, ``data["pages_added"]``, ``data["total_visuals"]``,
        and ``data["validation"]``.
    """
    return _hl.build_report(
        pbip_dir=pbip_dir,
        num_pages=num_pages,
        visual_variety=visual_variety,
        description=description,
    )


__all__ = [
    "generate_pbip",
    "edit_pbip",
    "add_measure",
    "add_visual",
    "add_page",
    "build_report",
    "suggest_measures",
    "deploy_to_fabric",
]

"""powerbi-builder root agent — Google ADK entry point.

Run with:
    adk web adk/
    adk api_server adk/
    adk run adk/ "Build a sales dashboard from data.csv"
"""
import os
import sys
from pathlib import Path

# Ensure project root is on sys.path so mcp_server, agents, utils are importable
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from google.adk.agents import Agent, SequentialAgent  # noqa: E402

from adk.config import MODEL_NAME, SKILLS_DIR, AGENTS_DIR, TEMPERATURE, SKILL_MAX_CHARS  # noqa: E402
from adk.skills_index import build_index, index_as_table  # noqa: E402
from google.genai import types  # noqa: E402
from utils import AuditLogger  # noqa: E402

log = AuditLogger.get("adk.agent")
from adk.tools.tmdl_tools import (  # noqa: E402
    read_csv_schema,
    write_tmdl_table,
    write_tmdl_measures,
    read_pbip_schema,
)
from adk.tools.pbir_tools import write_pbir_page, write_theme_json  # noqa: E402
from adk.tools.validation_tools import validate_pbip_structure  # noqa: E402
from adk.tools.project_tools import create_project_scaffold, finalize_pages_index  # noqa: E402
from adk.tools.dax_pattern_tools import list_dax_patterns, suggest_dax_measures  # noqa: E402
from adk.tools.calc_group_tools import list_calc_group_presets, write_calc_group  # noqa: E402
from adk.tools.schema_dax_tools import auto_suggest_measures  # noqa: E402
from adk.tools.relationship_tools import detect_and_write_relationships  # noqa: E402
from adk.tools.date_table_tools import check_needs_date_table, write_date_table  # noqa: E402
from adk.tools.theme_tools import list_theme_presets, apply_theme  # noqa: E402
from adk.tools.deneb_tools import list_deneb_presets, write_deneb_visual  # noqa: E402
from adk.tools.svg_tools import list_svg_patterns, suggest_svg_measures  # noqa: E402
from adk.tools.quality_tools import list_bpa_rules, run_bpa_validation  # noqa: E402
from adk.tools.naming_tools import (  # noqa: E402
    normalize_name,
    suggest_display_folder,
    plan_naming_for_pbip,
)
from adk.tools.lineage_tools import (  # noqa: E402
    analyze_lineage,
    find_measure_impacts,
    find_column_impacts,
    detect_circular_dependencies,
    suggest_safe_rename_order,
)
from adk.tools.kg_tools import query_knowledge_graph  # noqa: E402
from adk.tools.fabric_tools import (  # noqa: E402
    check_fab_installation,
    check_fab_auth,
    list_fabric_workspaces,
    list_fabric_items,
    preview_pbip_for_deploy,
    deploy_pbip_to_fabric,
)
from adk.tools.highlevel_tools import (  # noqa: E402
    generate_pbip,
    edit_pbip,
    add_measure,
    add_visual,
    add_page,
    build_report,
    suggest_measures,
)
from adk.tools.data_analysis_tools import analyze_data, verify_analysis, ask_user  # noqa: E402
from adk.tools.data_cleaning_tools import plan_cleaning, apply_cleaning, verify_cleaning  # noqa: E402
from adk.tools.planner_tools import create_build_plan, validate_plan  # noqa: E402
from adk.tools.review_tools import (  # noqa: E402
    review_report,
    check_visual_references,
    list_pages,
    describe_page,
)
from adk.tools.status_tools import get_project_status, get_run_summary  # noqa: E402
from adk.tools.trajectory_tools import get_trajectory, list_trajectory_runs  # noqa: E402
from adk.tools.a2ui_tools import render_ui_manifest  # noqa: E402
from adk.tools.edit_tools import (  # noqa: E402
    update_visual,
    delete_visual,
    delete_page,
    edit_measure,
    delete_measure,
    edit_table_source,
    relayout_page,
)
from adk.tools.layout_tools import plan_page_layout  # noqa: E402
from adk.tools.skill_tools import list_skills, load_skill_detail, load_skill_reference  # noqa: E402
from google.adk.tools.load_artifacts_tool import LoadArtifactsTool  # noqa: E402

# Built-in ADK tool that lets the model load artifact contents (e.g. an
# uploaded CSV) into its context.  Paired with /upload and
# SaveFilesAsArtifactsPlugin for the file-upload workflow.
load_artifacts = LoadArtifactsTool()

from adk.plugin import PowerBIBuilderPlugin  # noqa: E402

# ---------------------------------------------------------------------------
# Load agent + skill instructions
# ---------------------------------------------------------------------------
# Progressive disclosure: instead of eagerly inlining the full body of several
# SKILL.md files into the system prompt (the context-rot anti-pattern), we
# build a lightweight index of *all* skills (name + one-line description) and
# put only that index in the root instruction. The model fetches the full skill
# body and reference files on demand via the `list_skills` /
# `load_skill_detail` / `load_skill_reference` tools. This keeps the context
# window light while preserving access to precise instructions when needed.

_agent_md = AGENTS_DIR / "PowerBIBuilder.agent.md"
agent_instructions = _agent_md.read_text(encoding="utf-8") if _agent_md.exists() else ""

# Built once at import time (cheap: only frontmatter is parsed, no bodies).
_skill_index = build_index()
_skill_index_table = index_as_table(_skill_index)
# Map of skill name -> folder, for quick lookup in instructions.
_skill_names = [m.name for m in _skill_index]

# Kept for backward-compat with any external import; no longer used to bloat
# the system prompt. Returns the full body (frontmatter stripped) on demand.
def _load_skill(name: str, max_chars: int = SKILL_MAX_CHARS) -> str:
    from adk.skills_index import read_skill_body  # noqa: E402 (lazy import)

    body = read_skill_body(name)
    return body[:max_chars] if body else ""

# ---------------------------------------------------------------------------
# Phase 8 — Specialist sub-agents (multi-agent delegation)
# ---------------------------------------------------------------------------
# Each specialist owns a focused toolset so the root orchestrator can
# delegate (auto-transfer) granular work. The root retains the full toolset
# so it can also act standalone.

_SCHEMA_INSTR = """You are the *Schema specialist* for a Power BI project.
You infer table/column types from CSV/Excel/JSON, create the .pbip scaffold,
write TMDL table definitions, build Date tables, and detect relationships.
Always call `create_project_scaffold` BEFORE `write_tmdl_table`. Include
`source_path` (absolute CSV path) in every table_def."""

_DAX_INSTR = """You are the *DAX measures specialist* for a Power BI project.
You author well-structured DAX measures with displayFolder + description +
formatString. Use `list_dax_patterns` to discover patterns, `suggest_dax_measures`
to generate ready-made measures, and `write_tmdl_measures` to persist them.
Every measure MUST include a "table" field matching the target table name."""

_REPORT_INSTR = """You are the *Report & visual specialist* for a Power BI project.
You build PBIR pages with cards/charts/tables, apply themes, add Deneb/SVG
visuals, and finalise the pages.json index. Call `finalize_pages_index` AFTER
all `write_pbir_page` calls. Use `list_theme_presets` / `list_deneb_presets`
to discover options."""

_DEPLOY_INSTR = """You are the *Fabric deployment specialist*.
You check the fab CLI installation/auth, list workspaces/items, preview a PBIP
for deploy, and upload to Fabric. `deploy_pbip_to_fabric` defaults to
dry_run=True — only set dry_run=False when the user explicitly confirms."""

_ANALYZER_INSTR = """You are the *Data Analyzer specialist* for a Power BI project.
You analyse the user's raw data file for quality issues — nulls, outliers,
duplicates, single-value columns, type mismatches. Call `analyze_data` to get
a full quality profile with a score, issues list, and questions. Then call
`verify_analysis` to self-check the profile by re-reading a sample. For any
ambiguous issue (e.g. a column with 50% nulls), use `ask_user` to pose the
question to the user and WAIT for their answer before proceeding. Never guess
silently — surface every issue. Store the profile so the Cleaner and Planner
can use it."""

_CLEANER_INSTR = """You are the *Data Cleaner specialist* for a Power BI project.
You take the data quality profile produced by the Analyzer plus the user's
answers, build a cleaning plan with `plan_cleaning`, and apply it with
`apply_cleaning` (which writes a cleaned copy — the original is never touched).
Then call `verify_cleaning` to confirm the quality score improved. The cleaned
file becomes the source for schema generation. If cleaning did not help, warn
the user but proceed with the cleaned file."""

_PLANNER_INSTR = """You are the *Planner specialist* for a Power BI project.
You produce an ordered build plan from the user's description and any available
data profile using `create_build_plan`, then validate it with `validate_plan`.
The plan tells the pipeline which agents to run and in what order. When the
data profile shows quality issues, the plan must include a cleaning step. In
interactive mode, present the plan and wait for the user's confirmation before
the pipeline proceeds."""

_REVIEWER_INSTR = """You are the *Report Reviewer specialist* for a Power BI project.
After the report is built and structurally validated, you review it
semantically. Call `review_report` to check for ghost references (visuals
pointing to non-existent measures/columns), visual count reasonableness, layout
overlaps, and measure coverage. Use `check_visual_references` for a focused
ghost-reference audit. Integrate BPA findings from `run_bpa_validation` when
available. Flag critical issues (ghost refs, empty pages) as errors so the
user knows exactly what to fix."""

_STATUS_INSTR = """You are the *Status specialist* for a Power BI project.
You surface the current run status at any point: which agents have run, their
results, errors, progress percentage, data quality score, cleaning outcome,
and review score. Call `get_project_status` for the current project state and
`get_run_summary` for a full progress report. Be concise and actionable."""


# ---------------------------------------------------------------------------
# Phase 8 — Callbacks (must be defined BEFORE the factories that reference them)
# ---------------------------------------------------------------------------

# Tools whose result carries the active project name/root to track in state.
_PROJECT_TOOLS = {"generate_pbip", "edit_pbip", "create_project_scaffold"}


def track_project(tool, args: dict, tool_context, tool_response: dict):
    """``after_tool_callback`` — record the active project + pipeline data in session state.

    Phase 1 — extended beyond project name/root to sync the full pipeline
    output (validation, steps) so ADK sub-agents and the Phase 4 feedback loop
    can read a consistent view from ``session.state`` without re-running tools.
    """
    name = getattr(tool, "name", str(tool))
    if name not in _PROJECT_TOOLS or not isinstance(tool_response, dict):
        return None
    if not tool_response.get("ok"):
        return None
    data = tool_response.get("data", {}) or {}
    project_name = data.get("project_name")
    project_root = data.get("project_root") or data.get("pbip_root")
    if project_name is None and name == "create_project_scaffold":
        project_name = args.get("project_name")
    if project_name:
        tool_context.state["current_project"] = project_name
    if project_root:
        tool_context.state["current_project_root"] = project_root
    # Phase 1 — sync pipeline-level data into session.state so downstream ADK
    # agents (and the Phase 4 feedback loop) can read validation + steps without
    # re-invoking tools. Only present for generate_pbip / edit_pbip.
    if name in ("generate_pbip", "edit_pbip"):
        if data.get("validation") is not None:
            tool_context.state["validation"] = data["validation"]
        if data.get("steps"):
            tool_context.state["run_steps"] = [
                {"agent": s.get("agent", "?"), "ok": s.get("ok", False),
                 "message": s.get("message", "")}
                for s in data["steps"]
            ]
    log.info("[callback] track_project tool=%s project=%s root=%s",
             name, project_name, project_root)
    return None


def log_model_request(callback_context, llm_request):
    """``before_model_callback`` — log each LLM call."""
    agent_name = getattr(callback_context, "agent_name", "?")
    log.info("[callback] model_request agent=%s", agent_name)
    return None


def on_tool_error(tool, args, tool_context, error):
    """``on_tool_error_callback`` — log tool errors."""
    name = getattr(tool, "name", str(tool))
    log.warning("[callback] tool_error tool=%s error=%s", name, error)
    return None


def on_model_error(callback_context, llm_request, error):
    """``on_model_error_callback`` — log model errors (rate limits, etc.).

    When the error is transient (429/503/timeout — see :func:`utils.retry.
    is_retryable_error`) apply a short backoff sleep so the run doesn't
    immediately hammer the API again. Returns ``None`` so ADK reports the
    error as an event; it does not silently swallow permanent failures.
    """
    import time as _time
    from utils.retry import is_retryable_error

    agent_name = getattr(callback_context, "agent_name", "?")
    retryable = is_retryable_error(error)
    if retryable:
        # Short backoff: 2s then the caller's own retry budget handles the rest.
        backoff = float(os.getenv("POWERBI_RETRY_BACKOFF", "2.0"))
        log.warning(
            "[callback] model_error agent=%s (transient, backing off %.1fs) error=%s",
            agent_name, backoff, error,
        )
        _time.sleep(backoff)
    else:
        log.warning("[callback] model_error agent=%s (permanent) error=%s",
                    agent_name, error)
    return None


_PATH_TOOLS = {
    "generate_pbip": "source",
    "read_csv_schema": "csv_path",
    "edit_pbip": "pbip_dir",
    "add_measure": "pbip_dir",
    "add_visual": "pbip_dir",
    "add_page": "pbip_dir",
    "build_report": "pbip_dir",
    "deploy_pbip_to_fabric": "pbip_dir",
    "validate_pbip_structure": "pbip_dir",
    "preview_pbip_for_deploy": "pbip_dir",
    "suggest_measures": "source",
    # Edit/delete tools (Phase 7) — all take pbip_dir
    "update_visual": "pbip_dir",
    "delete_visual": "pbip_dir",
    "delete_page": "pbip_dir",
    "edit_measure": "pbip_dir",
    "delete_measure": "pbip_dir",
    "edit_table_source": "pbip_dir",
    "relayout_page": "pbip_dir",
    # Review/status tools (Phase 5-6)
    "review_report": "pbip_dir",
    "check_visual_references": "pbip_dir",
    "list_pages": "pbip_dir",
    "describe_page": "pbip_dir",
}


def validate_paths(tool, args: dict, tool_context):
    """``before_tool_callback`` — verify file/dir paths before a tool runs."""
    if not args or not isinstance(args, dict):
        return None
    name = getattr(tool, "name", str(tool))
    arg_key = _PATH_TOOLS.get(name)
    if not arg_key:
        return None
    path_str = args.get(arg_key)
    if not path_str:
        return None
    from pathlib import Path
    p = Path(path_str).expanduser().resolve()
    if p.exists():
        return None
    from mcp_server.highlevel import suggest_data_files
    suggestions = suggest_data_files()
    label = "file" if p.suffix else "directory"
    msg = f"{label.capitalize()} not found: {path_str}"
    if suggestions:
        msg += "\nAvailable data files:\n  " + "\n  ".join(suggestions)
    log.info("[callback] validate_paths BLOCKED tool=%s arg=%s path=%s",
             name, arg_key, path_str)
    return {
        "ok": False, "tool": name, "message": msg,
        "data": {}, "errors": [f"missing {label}: {path_str}"],
    }


# Factories — ADK forbids a sub-agent from having two parents, so each
# parent (root_agent, pipeline_agent) needs its *own* instances.  Building
# agents through factories keeps the two trees in sync without drift.


def _make_schema_agent(name: str = "schema_specialist") -> Agent:
    return Agent(
        name=name,
        model=MODEL_NAME,
        description="Schema & TMDL specialist: infers tables, writes model scaffold + TMDL tables + Date table + relationships.",
        instruction=_SCHEMA_INSTR,
        tools=[
            read_csv_schema,
            create_project_scaffold,
            write_tmdl_table,
            write_date_table,
            check_needs_date_table,
            detect_and_write_relationships,
            read_pbip_schema,
        ],
        # Callbacks are shared with root_agent so delegated tool calls also
        # get path validation + project tracking.
        before_tool_callback=validate_paths,
        after_tool_callback=track_project,
        before_model_callback=log_model_request,
    )


def _make_dax_agent(name: str = "dax_specialist") -> Agent:
    return Agent(
        name=name,
        model=MODEL_NAME,
        description="DAX measures specialist: authors pattern-driven measures, calc groups, SVG measures.",
        instruction=_DAX_INSTR,
        tools=[
            list_dax_patterns,
            suggest_dax_measures,
            auto_suggest_measures,
            write_tmdl_measures,
            list_calc_group_presets,
            write_calc_group,
            list_svg_patterns,
            suggest_svg_measures,
            suggest_measures,
            add_measure,
        ],
        before_tool_callback=validate_paths,
        after_tool_callback=track_project,
        before_model_callback=log_model_request,
    )


def _make_report_agent(name: str = "report_specialist") -> Agent:
    return Agent(
        name=name,
        model=MODEL_NAME,
        description="Report & visual specialist: PBIR pages, themes, Deneb visuals, pages index.",
        instruction=_REPORT_INSTR,
        tools=[
            write_pbir_page,
            write_theme_json,
            list_theme_presets,
            apply_theme,
            list_deneb_presets,
            write_deneb_visual,
            finalize_pages_index,
            add_visual,
            add_page,
            # Visibility + smart-layout tools so the specialist can read the
            # current canvas and place visuals without overlaps.
            list_pages,
            describe_page,
            plan_page_layout,
            relayout_page,
            update_visual,
            delete_visual,
        ],
        before_tool_callback=validate_paths,
        after_tool_callback=track_project,
        before_model_callback=log_model_request,
    )


def _make_deploy_agent(name: str = "deploy_specialist") -> Agent:
    return Agent(
        name=name,
        model=MODEL_NAME,
        description="Fabric deployment specialist: fab CLI checks, workspace listing, PBIP upload (dry_run by default).",
        instruction=_DEPLOY_INSTR,
        tools=[
            check_fab_installation,
            check_fab_auth,
            list_fabric_workspaces,
            list_fabric_items,
            preview_pbip_for_deploy,
            deploy_pbip_to_fabric,
        ],
        before_tool_callback=validate_paths,
        after_tool_callback=track_project,
        before_model_callback=log_model_request,
    )


def _make_data_analyzer_agent(name: str = "data_analyzer") -> Agent:
    return Agent(
        name=name,
        model=MODEL_NAME,
        description=(
            "Data Analyzer specialist: profiles raw data for quality (nulls, "
            "outliers, duplicates, single-value columns), self-verifies the "
            "analysis, and asks the user about ambiguous decisions."
        ),
        instruction=_ANALYZER_INSTR,
        tools=[
            analyze_data,
            verify_analysis,
            ask_user,
            read_csv_schema,
            read_pbip_schema,
        ],
        before_tool_callback=validate_paths,
        after_tool_callback=track_project,
        before_model_callback=log_model_request,
    )


def _make_data_cleaner_agent(name: str = "data_cleaner") -> Agent:
    return Agent(
        name=name,
        model=MODEL_NAME,
        description=(
            "Data Cleaner specialist: builds a cleaning plan from the data "
            "profile + user answers, applies it non-destructively, and "
            "self-verifies the quality improved."
        ),
        instruction=_CLEANER_INSTR,
        tools=[
            plan_cleaning,
            apply_cleaning,
            verify_cleaning,
            analyze_data,  # so it can re-profile the cleaned file
        ],
        before_tool_callback=validate_paths,
        after_tool_callback=track_project,
        before_model_callback=log_model_request,
    )


def _make_planner_agent(name: str = "planner") -> Agent:
    return Agent(
        name=name,
        model=MODEL_NAME,
        description=(
            "Planner specialist: produces an ordered build plan from the "
            "user's description + data profile, validates it, and presents "
            "it for confirmation in interactive mode."
        ),
        instruction=_PLANNER_INSTR,
        tools=[
            create_build_plan,
            validate_plan,
            analyze_data,  # so it can trigger an analysis if no profile yet
        ],
        before_tool_callback=validate_paths,
        after_tool_callback=track_project,
        before_model_callback=log_model_request,
    )


def _make_reviewer_agent(name: str = "report_reviewer") -> Agent:
    return Agent(
        name=name,
        model=MODEL_NAME,
        description=(
            "Report Reviewer specialist: semantic review of a generated "
            "report — ghost references, visual/data compatibility, layout "
            "overlaps, measure coverage."
        ),
        instruction=_REVIEWER_INSTR,
        tools=[
            review_report,
            check_visual_references,
            run_bpa_validation,
            analyze_lineage,
        ],
        before_tool_callback=validate_paths,
        after_tool_callback=track_project,
        before_model_callback=log_model_request,
    )


def _make_status_agent(name: str = "status") -> Agent:
    return Agent(
        name=name,
        model=MODEL_NAME,
        description=(
            "Status specialist: surfaces the current run status — agents "
            "run, results, progress, quality/review scores — at any point."
        ),
        instruction=_STATUS_INSTR,
        tools=[
            get_project_status,
            get_run_summary,
        ],
        before_tool_callback=validate_paths,
        after_tool_callback=track_project,
        before_model_callback=log_model_request,
    )


# Shared instances attached to the root orchestrator.
schema_agent = _make_schema_agent()
dax_agent = _make_dax_agent()
report_agent = _make_report_agent()
deploy_agent = _make_deploy_agent()
data_analyzer_agent = _make_data_analyzer_agent()
data_cleaner_agent = _make_data_cleaner_agent()
planner_agent = _make_planner_agent()
reviewer_agent = _make_reviewer_agent()
status_agent = _make_status_agent()


# ---------------------------------------------------------------------------
# Root agent
# ---------------------------------------------------------------------------

root_agent = Agent(
    name="powerbi_builder",
    model=MODEL_NAME,
    # Phase 8 — multi-agent: root can delegate to specialists, but retains
    # the full toolset so it also works standalone (adk run / adk web).
    sub_agents=[planner_agent, data_analyzer_agent, data_cleaner_agent, schema_agent, dax_agent, report_agent, reviewer_agent, status_agent, deploy_agent],
    # Phase 8 — callbacks: track active project in state + log model calls
    # + validate paths before tool runs (prevent wasted retries on bad paths)
    # + error callbacks for tool/model failures.
    after_tool_callback=track_project,
    before_tool_callback=validate_paths,
    before_model_callback=log_model_request,
    on_tool_error_callback=on_tool_error,
    on_model_error_callback=on_model_error,
    # Low temperature for deterministic tool-calling / code generation.
    generate_content_config=types.GenerateContentConfig(temperature=TEMPERATURE),
    description=(
        "End-to-end Power BI project builder. Creates complete PBIP projects "
        "(semantic model + report) from CSV, Excel, or JSON data. "
        "Edits existing PBIP/PBIX projects. Follows Microsoft TMDL and PBIR standards."
    ),
    instruction=f"""
{agent_instructions}

---

## Available Skills (Progressive Disclosure)

The table below is a **lightweight index** of all {len(_skill_names)} available
skills — only the name and a one-line description. Do NOT try to recall a
skill's full instructions from memory; instead, when a task calls for a skill's
detailed guidance, **fetch it on demand**:

1. Call `list_skills` to re-read the index (if you need the current list).
2. Call `load_skill_detail(name)` to load the full SKILL.md body + the list of
   its supporting `references/` / `resources/` / `assets/` files.
3. Call `load_skill_reference(name, reference, kind)` to load one supporting
   file's full contents when the skill body points you to it.

This keeps your context window light and avoids stale recall of skill details.
Load a skill's detail only when you are about to act on it.

{_skill_index_table}

---

## Tool Reference

### Low-Level Tools (step-by-step pipeline)

| Tool | Purpose |
|------|---------|
| `read_csv_schema` | Infer schema from CSV/Excel/JSON |
| `create_project_scaffold` | Create .pbip entry files + model.tmdl + database.tmdl (call FIRST) |
| `write_tmdl_table` | Write TMDL table definition |
| `write_tmdl_measures` | Append DAX measures to TMDL |
| `read_pbip_schema` | Read existing PBIP schema + measures (with DAX expressions) + pages (edit mode) |
| `write_pbir_page` | Write PBIR report page + visuals |
| `write_theme_json` | Write report theme |
| `finalize_pages_index` | Write pages.json index (call AFTER all write_pbir_page calls) |
| `list_pages` | List all report pages with per-page visual counts (read current report state) |
| `describe_page` | Full detail of one page: every visual's type, position, and data bindings (see the canvas before editing) |
| `plan_page_layout` | Compute smart zone-based non-overlapping positions for a set of visuals (1280×720 canvas) |
| `relayout_page` | Re-apply smart layout to all visuals on an existing page (fix overlaps in one call) |
| `list_dax_patterns` | List all available DAX pattern types (call to discover options) |
| `suggest_dax_measures` | Generate ready-made DAX measures from pattern library |
| `list_calc_group_presets` | List available calculation group presets |
| `write_calc_group` | Write a calculation group TMDL (time_intelligence / period_comparison) |
| `auto_suggest_measures` | Deterministic DAX measures from schema (no LLM) — call after read_csv_schema |
| `check_needs_date_table` | Check if schema has a datetime column → Date table recommended |
| `write_date_table` | Write complete Date.tmdl (18 calendar columns, auto CALENDAR range) |
| `detect_and_write_relationships` | Detect FK relationships between tables + write relationships.tmdl |
| `list_theme_presets` | List available theme presets (default, corporate_blue, modern_dark, earth_tones, vibrant) |
| `apply_theme` | Write theme.json with a preset or custom palette |
| `list_deneb_presets` | List available Deneb (Vega-Lite) visual presets (kpi_card, bullet_chart, spark_line, small_multiples, calendar_heatmap) |
| `write_deneb_visual` | Add a Deneb (Vega-Lite) custom visual to an existing PBIR page (from preset or hand-written spec) |
| `list_svg_patterns` | List available SVG measure presets (sparkline, progress_bar, rating_stars) |
| `suggest_svg_measures` | Generate SVG DAX measures (data URI images) — pass result.measures to write_tmdl_measures |
| `list_bpa_rules` | List available Best Practice Analyzer rules (performance, metadata, style, dax) |
| `run_bpa_validation` | Run BPA against a PBIP — returns findings grouped by severity / category / rule |
| `normalize_name` | Normalize a name to Title Case or PascalCase (preserves YoY/KPI/PY acronyms) |
| `suggest_display_folder` | Suggest a displayFolder hierarchy for a measure (Time Intelligence / Targets / Ratios / Aggregations) |
| `plan_naming_for_pbip` | Propose bulk rename + folder assignments for an existing PBIP |
| `analyze_lineage` | Build dependency graph (measure→measure, measure→column) + summary |
| `find_measure_impacts` | Find direct and transitive dependents of a measure (impact analysis before rename) |
| `find_column_impacts` | Find dependents of a column ("Table.Column") |
| `detect_circular_dependencies` | List any circular DAX dependency chains |
| `suggest_safe_rename_order` | Topological order — safe order to rename / refactor |
| `check_fab_installation` | Verify the fab CLI is installed and reachable |
| `check_fab_auth` | Report whether the fab CLI is signed in |
| `list_fabric_workspaces` | List visible Microsoft Fabric workspaces |
| `list_fabric_items` | List items in a Fabric workspace by name |
| `preview_pbip_for_deploy` | Resolve the *.SemanticModel + *.Report folders inside a PBIP |
| `deploy_pbip_to_fabric` | Upload PBIP to a Fabric workspace (SemanticModel first, then Report). Defaults to dry_run=True. |
| `validate_pbip_structure` | Validate completed project |

### High-Level Tools (Phase 7 — single-call orchestration)

| Tool | Purpose |
|------|---------|
| `generate_pbip` | **Build a complete PBIP from CSV/Excel/JSON in one call** — runs the full pipeline internally. Pass `num_pages`/`visual_variety="all"` here directly for an exact page count / rich visual mix — decide it up front, don't build a plain report and top it up with `build_report` afterward (see "Decide First" below). |
| `edit_pbip` | **Edit an existing PBIP from a description** — copies + runs edit pipeline |
| `add_measure` | **Append a single DAX measure** to a PBIP semantic model — a genuine follow-up edit, not part of the initial build |
| `add_visual` | **Add one visual** to an existing page — a genuine follow-up edit, not part of the initial build |
| `add_page` | **Add a new page** with optional visuals, updates pages.json — a genuine follow-up edit, not part of the initial build |
| `build_report` | **Additively adds N MORE pages** (pie, donut, scatter, matrix, kpi, slicer, …) on top of an existing PBIP's current page count. Only use this for a genuine follow-up ("add 2 more pages" after the user has already seen the first result) — never as an automatic second step after the initial `generate_pbip` call, since the two page counts add together and overshoot what the user asked for. |
| `suggest_measures` | **Propose DAX measures** from a CSV/PBIP schema (auto or pattern-driven) |

> Deploy is handled by `deploy_pbip_to_fabric` in the Fabric table above (not a
> separate high-level tool). It defaults to ``dry_run=True``.

## Decide First, Then Build Exactly That — Never Build-Then-Fix

**Before calling any build tool**, read the user's request and decide, up
front, in your own reasoning:
- The exact page count they want (a number, or "however many fit the
  content well" if they didn't give one).
- Whether they want a rich variety of visual types (pie, scatter, kpi,
  matrix, slicer, table, etc.) or a plain summary is enough.

Then call `generate_pbip` **once**, passing that decision directly:

```
generate_pbip(source="<csv_path>", description="...", project_name="...",
              num_pages=<N>, visual_variety="all")
```

This produces **exactly** `<N>` pages (never more) with full visual
variety in one call — there is no need to build first, list the pages,
notice the count is wrong, and delete pages to fix it. Do **not** use the
old two-step pattern (`generate_pbip` then `build_report` to "top up" the
page count): `build_report`'s `num_pages` is *additive* — it adds pages ON
TOP of whatever `generate_pbip` already created — so chaining the two
together routinely overshoots whatever total the user actually asked for
(a real, previously observed failure mode: the user asked for 2 pages,
got 3-5, and the agent had to `list_pages` → `describe_page` →
`delete_page` several times to recover).

**Reserve `build_report` / `add_page` / `add_visual` / `delete_page` for
genuine follow-up edits** — requests the user makes *after* seeing the
first result ("add one more page", "add a KPI here") — never as an
automatic second step to correct the very first build. The same
"decide first" principle applies to individual visual choices: if the user
describes what they want on each page, put that description into
`generate_pbip`'s `description` argument (it drives the same intent-aware
planning that decides pages and KPIs) rather than building a generic
report and then reactively calling `add_visual` one at a time to patch it
up afterward.

## Data Analysis Already Happens First — Don't Re-Check Reactively

`generate_pbip` already runs a full data-quality analysis
(`DataAnalyzerAgent`) internally, **before** any file is written, as part
of the same single call — this is not something you need to do yourself
first. Don't call the standalone `analyze_data`/`verify_analysis` tools
*after* `generate_pbip` as a reactive double-check; the analysis already
happened and is reflected in the build's own result (quality score,
cleaning steps applied, etc.). Only use `analyze_data` standalone
*before* calling `generate_pbip`, and only when the user hasn't given
enough of a description yet for you to decide what to build — i.e. to
help you ask a better clarifying question, not to verify work that's
already done.

## Path Validation

Before calling any tool with a file/directory path:
- If you are unsure whether a file exists, ask the user for the absolute path.
- The system automatically validates paths before tool execution and will
  return a list of available data files if the path is wrong.
- Bundled sample data: `SampleData.csv` (16 columns, real sales data) and
  `examples/sample.csv` (5 columns, simple test data).

## Exact Pipeline (Create Mode)

**ALWAYS follow this exact order:**

```
1. read_csv_schema(csv_path)
   → get schema: table_name, columns

2. create_project_scaffold(project_name, table_name, output_root="./output")
   → returns: semantic_model_dir (e.g. "SalesDashboard.SemanticModel/definition")
              report_dir (e.g. "SalesDashboard.Report/definition")
              project_root (absolute path, use for validation)

3. write_tmdl_table(output_dir=<semantic_model_dir>, table_def={{...}}, output_root="./output")
   → table_def MUST include source_path (absolute path to CSV)

4a. (optional) suggest_dax_measures(pattern_types=[...], base_name=..., base_expr=..., table=...)
    → use list_dax_patterns() to discover available patterns (ytd, yoy_pct, share_of_total, ...)
    → returns ready-made measure dicts — pass them directly to write_tmdl_measures
4b. write_tmdl_measures(output_dir=<semantic_model_dir>, measures=[...], output_root="./output")
   → IMPORTANT: each measure MUST include "table": "<TableName>" (same name as step 1 table_name)
     Example: {{"name": "Total Sales", "expression": "SUM('SampleData'[Sales])", "table": "SampleData"}}
     NEVER omit the "table" field — "Measures" is a reserved/invalid table name in Power BI

5. write_theme_json(output_dir=<report_dir>, output_root="./output")
   OR: apply_theme(output_dir=<report_dir>, preset="corporate_blue", output_root="./output")
       → use list_theme_presets() to discover available themes

6. write_pbir_page(output_dir=<report_dir>, page_def={{...}}, output_root="./output")
   → note the page_def id value

7. finalize_pages_index(project_name, page_ids=[<page_id>], output_root="./output")

8. validate_pbip_structure(pbip_dir=<project_root>, output_root="./output")
   → project_root is the ABSOLUTE path returned by create_project_scaffold
```

## Edit Pipeline

```
1. read_pbip_schema(pbip_dir)               → existing schema + measures (with DAX expressions) + pages
2. write_tmdl_measures(...)                 → new measures only (skip duplicates)
3. add_page(pbip_dir=<pbip_dir>, ...)       → new page with unique id (PREFER this over
                                               write_pbir_page + finalize_pages_index — see below)
5. validate_pbip_structure(pbip_dir=...)    → validate
```

**Prefer `add_page`/`add_visual`/`add_measure` (all take a single `pbip_dir`)
over the low-level `write_pbir_page`/`write_theme_json`/`finalize_pages_index`
tools when editing an EXISTING project.** Those three low-level tools use a
DIFFERENT, incompatible path convention (`output_root` + a relative
`output_dir`/`project_name`, meant for building a project from scratch via
`create_project_scaffold`'s FLAT layout: `output/<name>.Report/...`).
`generate_pbip`/`build_report` instead create a NESTED layout
(`output/<name>/<name>.Report/...`). Calling the low-level tools with
`output_root="./output"` against a project that already exists — the
literal, common mistake — silently writes a brand-new, DISCONNECTED
`.Report` folder next to the real project: the tool call reports `ok=True`,
nothing looks wrong, but the real project was never touched and any zip/
download the user later opens is missing your changes. If you must use one
of these three low-level tools directly on an existing project, set
`output_root` to the project's own directory (the SAME value you pass as
`pbip_dir` everywhere else) — never the generic `"./output"`.

## Intelligent Edit Workflow (ALWAYS follow before editing a report)

Before changing a report, you MUST first **see** the current canvas so you can
decide intelligently (where is there free space? what do existing visuals
bind to?). The report canvas is 1280×720 pixels. Follow this loop:

```
1. read_pbip_schema(pbip_dir)      → learn tables, columns, measures (with DAX expressions), pages
2. list_pages(pbip_dir)            → see what pages exist + how many visuals each has
3. describe_page(pbip_dir, page_id) → see exact visual positions + data bindings on the target page
4. DECIDE what to do, informed by step 3:
   - add a new visual  → pick a free spot; call plan_page_layout([{{id,type}},...]) for smart positions
                         (or call add_page with auto_layout=True for a fresh page)
   - move/resize        → update_visual(changes={{x, y, width, height, ...}})  (geometry written to nested position)
   - clean up overlaps  → relayout_page(pbip_dir, page_id)  (re-zones the whole page in one call)
   - remove             → delete_visual / delete_page
5. After edits: validate_pbip_structure(pbip_dir) + review_report(pbip_dir)  (verify the change worked)
```

**Key rules for placement:**
- The canvas is **1280×720**. Use `plan_page_layout` to get non-overlapping
  zone-based positions (cards→top, slicers→right, charts→centre, tables→bottom).
- `add_page` auto-layouts its visuals by default (`auto_layout=True`); pass
  `auto_layout=False` only when you supply explicit geometry.
- After adding several visuals to an existing page, call `relayout_page` to
  guarantee no overlaps.
- `update_visual` writes `x/y/width/height/z/tabOrder` to the nested
  `position` object — pass them flat, e.g. `{{"x": 100, "y": 50, "width": 400}}`.
- `describe_page` returns each visual's `position` (from the nested object) and
  `bindings` (measure/column refs) — use it to find free space and avoid
  editing the wrong visual.

## Key Rules

- `output_dir` for semantic model tools: `"ProjectName.SemanticModel/definition"`
- `output_dir` for report tools: `"ProjectName.Report/definition"`
- `output_root` is always `"./output"` (the base folder)
- `pbip_dir` for validation is the **project_root** returned by `create_project_scaffold`
  (this is the `./output` folder that contains both .SemanticModel and .Report subfolders)
- `table_def` MUST include `source_path` (absolute CSV path) so Desktop can load data
- Use `source_path` as the absolute path of the CSV file

## Output Format

After completing a build OR any edit (add_measure/add_visual/add_page/
build_report/edit_pbip all count), report:
```
✅ Project: <ProjectName>
📊 Table: <TableName> (<N> columns)
📐 Measures: <N> DAX measures
📄 Pages: <N> pages (<total_visuals> visuals)
✅ Validation: <status>
📦 Download: your project is ready to download as a zip file
```

**NEVER show the user a raw server filesystem path** (e.g.
`./output/<ProjectName>...`) as "where their file is" — the user is on a
web page and has no access to the server's filesystem; a path is not
something they can act on. Every successful generate_pbip/edit_pbip/
add_measure/add_visual/add_page/build_report call automatically saves a
fresh zip of the current project as a downloadable artifact
(`user:project_<ProjectName>.zip`) — tell the user their project is
ready to download (from the web UI's Artifacts panel, or via
`/download <ProjectName>` in the terminal REPL) instead of mentioning
any local path.
""",
    tools=[
        read_csv_schema,
        create_project_scaffold,
        write_tmdl_table,
        list_dax_patterns,
        suggest_dax_measures,
        auto_suggest_measures,
        check_needs_date_table,
        write_date_table,
        list_calc_group_presets,
        write_calc_group,
        detect_and_write_relationships,
        write_tmdl_measures,
        read_pbip_schema,
        list_theme_presets,
        apply_theme,
        list_deneb_presets,
        write_deneb_visual,
        list_svg_patterns,
        suggest_svg_measures,
        list_bpa_rules,
        run_bpa_validation,
        normalize_name,
        suggest_display_folder,
        plan_naming_for_pbip,
        analyze_lineage,
        find_measure_impacts,
        find_column_impacts,
        detect_circular_dependencies,
    suggest_safe_rename_order,
    # Knowledge Graph queries (Wave D2) — whole-project typed graph queries
    # (impact, shortest path, neighbours, nodes by type).
    query_knowledge_graph,
    check_fab_installation,
        check_fab_auth,
        list_fabric_workspaces,
        list_fabric_items,
        preview_pbip_for_deploy,
        deploy_pbip_to_fabric,
        # Data analysis tools
        analyze_data,
        verify_analysis,
        ask_user,
        # Data cleaning tools
        plan_cleaning,
        apply_cleaning,
        verify_cleaning,
        # Planner tools
        create_build_plan,
        validate_plan,
        # Review tools
        review_report,
        check_visual_references,
        # Report-state visibility tools — let the model read the current canvas
        # (pages + per-visual positions/bindings) before deciding edits.
        list_pages,
        describe_page,
        # Smart layout engine — zone-based non-overlapping positions
        plan_page_layout,
        # Status tools
        get_project_status,
        get_run_summary,
        # Trajectory evaluation tools (Wave A4) — retrieve the step-by-step
        # span trace of an agent run for replay/evaluation.
        get_trajectory,
        list_trajectory_runs,
        # A2UI protocol tool (Wave B2) — render a framework-neutral UI manifest
        # for a built PBIP so a generic UI client can render the dashboard.
        render_ui_manifest,
        # Edit/delete tools (modify existing PBIP elements)
        update_visual,
        delete_visual,
        delete_page,
        edit_measure,
        delete_measure,
        edit_table_source,
        relayout_page,
        write_pbir_page,
        write_theme_json,
        finalize_pages_index,
        validate_pbip_structure,
        # Phase 7 — high-level orchestration tools
        generate_pbip,
        edit_pbip,
        add_measure,
        add_visual,
        add_page,
        build_report,
        suggest_measures,
        # Phase 8 — file upload support: lets the model load uploaded
        # artifact contents (e.g. a CSV from /upload or adk web attach).
        load_artifacts,
        # Progressive-disclosure skill tools — load full skill instructions +
        # reference files on demand instead of bloating the system prompt.
        list_skills,
        load_skill_detail,
        load_skill_reference,
    ],
)


# ---------------------------------------------------------------------------
# Phase 8 — Workflow agent showcase (SequentialAgent)
# ---------------------------------------------------------------------------
# A non-LLM workflow agent that runs the full build pipeline in order:
# planner → analyzer → cleaner → schema → dax → report → reviewer.
# Exported for documentation/tests; the REPL uses ``root_agent`` (which can
# also delegate to these same specialists on demand via auto-transfer).
# Demonstrates ADK's SequentialAgent primitive.
# Fresh instances via factories — ADK forbids shared sub-agents between parents.
pipeline_agent = SequentialAgent(
    name="build_pipeline",
    description=(
        "Sequential workflow: planner → data analyzer → data cleaner → "
        "schema → dax → report → reviewer specialists run in order."
    ),
    sub_agents=[
        _make_planner_agent(),
        _make_data_analyzer_agent(),
        _make_data_cleaner_agent(),
        _make_schema_agent(),
        _make_dax_agent(),
        _make_report_agent(),
        _make_reviewer_agent(),
    ],
)


# ---------------------------------------------------------------------------
# Phase 8 — App export (makes plugins work in `adk web`)
# ---------------------------------------------------------------------------
# ``adk web`` looks for ``app`` (an ``App`` instance) **before**
# ``root_agent``.  An ``App`` carries ``plugins``, which the stock
# ``Runner`` created by ``adk web`` will wire automatically.  Without this,
# plugins like ``PowerBIBuilderPlugin`` (web-upload bridge + MIME stripping)
# and ``SaveFilesAsArtifactsPlugin`` never run in the browser UI — causing
# the Gemini 400 "Unsupported MIME type" error when uploading Excel/CSV.

def _build_plugins() -> list:
    """Build the plugin list used by both the REPL Runner and ``adk web``."""
    plugins = [PowerBIBuilderPlugin()]
    try:
        from google.adk.plugins.save_files_as_artifacts_plugin import (
            SaveFilesAsArtifactsPlugin,
        )
        plugins.append(SaveFilesAsArtifactsPlugin())
    except Exception as exc:  # pragma: no cover
        log.warning(f"SaveFilesAsArtifactsPlugin unavailable: {exc}")
    return plugins


from google.adk.apps import App  # noqa: E402

# The app name MUST match the directory name that ``adk web`` loads from.
# ``adk web adk/`` → app name "adk"; ``adk web .`` → app name from the
# parent dir.  Using "adk" here aligns with the standard ``adk web adk/``
# invocation and prevents session-not-found errors.
app = App(
    name="adk",
    root_agent=root_agent,
    plugins=_build_plugins(),
)

"""Base classes for the multi-agent system.

This module defines the small "ADK-style" agent framework used by the project.
Each agent is a self-contained object with:

* a ``name`` and a ``description`` (the "system prompt" / role),
* a single ``run(context)`` method that produces an :class:`AgentResult`,
* access to a shared :class:`AgentContext` (carries the business description,
  the inferred schema, the run's output root, and a reference to the MCP
  toolbox).

The orchestrator chains these agents sequentially, threading the context
through them so each one builds on the previous agent's output. This mirrors
the Google ADK ``Agent`` + sequential ``runner`` pattern while staying a thin,
dependency-free implementation (no external agent SDK required to run).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from utils import AuditLogger, utc_now_iso
from mcp_server.server import PbipToolbox


class ContextExtra(TypedDict, total=False):
    """Typed contract for AgentContext.extra.

    All keys are optional (total=False) — they are populated progressively
    as agents run. Declaring them here prevents silent key-name mismatches:
    typos in key strings become static-analysis errors rather than silent
    ``None`` lookups at runtime.

    Use ``Any`` for complex Pydantic model types to avoid circular imports at
    definition time; the actual types are documented in the field comments.
    """

    # DataAnalyzerAgent output
    data_profile: dict[str, Any]
    answers: dict[str, str]
    business_analysis: Any            # agents.schemas.BusinessAnalysis

    # BIReasoningAgent output
    bi_reasoning: Any                 # agents.schemas.BIReasoningResult

    # SchemaAgent output
    table_definition: dict[str, Any]
    tmdl_table_path: str

    # RelationshipAgent output
    relationships: list[dict[str, Any]]

    # PlannerAgent output
    plan: list[dict[str, Any]]
    build_plan: Any                   # agents.schemas.BuildPlan
    needs_cleaning: bool
    report_style: str
    clarifications: dict[str, Any]
    plan_confirmed: bool

    # ReportReviewerAgent output
    review: list[dict[str, Any]]

    # ReportAgent / VisualPlannerAgent
    report_json_path: str
    visual_candidates: list[dict[str, Any]]
    visual_plan_max_pages: int
    report_plan: Any                  # agents.schemas.ReportPlan

    # Pipeline-wide settings (set by orchestrator before pipeline starts)
    interactive: bool
    excel_sheet: str | None

    # Optimization layer — candidate scoring and selection
    scoring_weights:        dict[str, float]       # adjustable by feedback loop
    dax_candidates:         list[dict[str, Any]]   # [{id, strategy, measures, score}]
    schema_candidates:      list[dict[str, Any]]   # [{id, strategy, columns, score}]
    visual_plan_candidates: list[dict[str, Any]]   # [{id, strategy, plan, score}]
    judge_result:           dict[str, Any]         # JudgeLayer.evaluate() output


@dataclass
class AgentContext:
    """Mutable shared state passed through the agent pipeline.

    Fields are populated as agents run:

    * ``business_description`` -- the user's plain-English problem statement.
    * ``source_path``          -- path to the input CSV / JSON / Excel / PBIP.
    * ``schema``               -- filled by SchemaAgent or ReadPBIPAgent.
    * ``measures``             -- filled by DAXAgent.
    * ``pages``                -- filled by ReportAgent.
    * ``validation``           -- filled by ValidatorAgent.
    * ``project_name``         -- TMDL/PBIR-safe project name.
    * ``toolbox``              -- the MCP toolbox bound to the run's output root.

    Edit-mode fields (populated only when editing an existing project):
    * ``input_mode``           -- "create" | "edit_pbip" | "edit_pbix" | "edit_excel"
    * ``existing_pbip_path``   -- absolute path to the source PBIP folder.
    * ``existing_measures``    -- measure names already in the model (skip dupes).
    * ``existing_page_ids``    -- page folder ids already in the report.

    **Phase 1 — ``session_state``:** the source of truth for pipeline data.
    The typed fields above (``schema``, ``measures``, ``pages``, ``validation``)
    are kept as a backward-compatible *cache* so existing agents that read/write
    ``ctx.schema`` keep working. ``sync_to_state()`` copies them into
    ``session_state`` (which mirrors ADK's ``session.state`` dict); the ADK
    ``track_project`` callback then propagates ``session_state`` into the real
    ``session.state`` when the pipeline runs under an ADK Runner. This keeps the
    deterministic ``agents/`` pipeline unchanged while giving ADK agents and the
    Phase 4 feedback loop a single, typed contract to read from.
    """

    business_description: str
    source_path: Path
    toolbox: PbipToolbox
    project_name: str
    pbip_root: Path
    schema: dict[str, Any] | None = None
    measures: list[dict[str, Any]] = field(default_factory=list)
    pages: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, Any] | None = None
    extra: ContextExtra = field(default_factory=ContextExtra)
    theme_preset: str = "default"  # theme preset key (default, modern_dark, etc.)
    # edit-mode fields
    input_mode: str = "create"
    existing_pbip_path: Path | None = None
    existing_measures: list[str] = field(default_factory=list)
    existing_page_ids: list[str] = field(default_factory=list)
    # Phase 1 — ADK session.state mirror. Source of truth for pipeline data
    # when running under an ADK Runner; a plain dict mirror when running the
    # deterministic agents/ pipeline directly.
    session_state: dict[str, Any] = field(default_factory=dict)

    # convenience: relative sub-paths inside the run's allowed root
    @property
    def sm_definition_rel(self) -> str:
        return f"{self.project_name}.SemanticModel/definition"

    @property
    def report_definition_rel(self) -> str:
        return f"{self.project_name}.Report/definition"

    # ------------------------------------------------------------------
    # Phase 1 — session.state sync
    # ------------------------------------------------------------------

    def sync_to_state(self) -> dict[str, Any]:
        """Copy the typed context fields into ``session_state``.

        Called by the orchestrator after each agent so ``session_state`` always
        reflects the latest pipeline output. Returns the updated state dict so
        the ADK ``track_project`` callback can propagate it into ``session.state``.
        """
        self.session_state.update({
            "business_description": self.business_description,
            "project_name": self.project_name,
            "schema": self.schema,
            "measures": self.measures,
            "pages": self.pages,
            "validation": self.validation,
            "relationships": self.extra.get("relationships", []),
            "data_profile": self.extra.get("data_profile"),
            "plan": self.extra.get("plan"),
            "review": self.extra.get("review"),
            "input_mode": self.input_mode,
            "theme_preset": self.theme_preset,
        })
        return self.session_state

    def load_from_state(self) -> None:
        """Pull ``session_state`` back into the typed context fields.

        The inverse of :meth:`sync_to_state`. Used when an ADK sub-agent writes
        its result into ``session.state`` and the deterministic pipeline needs
        to see it through the legacy ``ctx.schema`` / ``ctx.measures`` fields.
        Missing keys leave the existing field untouched.
        """
        s = self.session_state
        if "schema" in s and s["schema"] is not None:
            self.schema = s["schema"]
        if "measures" in s:
            self.measures = s["measures"]
        if "pages" in s:
            self.pages = s["pages"]
        if "validation" in s and s["validation"] is not None:
            self.validation = s["validation"]
        if "relationships" in s:
            self.extra["relationships"] = s["relationships"]
        if "data_profile" in s:
            self.extra["data_profile"] = s["data_profile"]
        if "plan" in s:
            self.extra["plan"] = s["plan"]
        # C1 fix: restore review (was asymmetric — sync_to_state wrote it but
        # load_from_state never read it back, so reviewer findings were lost
        # on reload).
        if "review" in s:
            self.extra["review"] = s["review"]
        if "needs_cleaning" in s:
            self.extra["needs_cleaning"] = s["needs_cleaning"]
        if "report_style" in s:
            self.extra["report_style"] = s["report_style"]


@dataclass
class AgentResult:
    """Standard return type for every agent's ``run`` method."""

    agent: str
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None

    def finalize(self) -> "AgentResult":
        self.finished_at = utc_now_iso()
        return self


class BaseAgent:
    """Base class for all agents.

    Subclasses set ``name`` and ``description`` (role prompt) and implement
    :meth:`_run`. The public :meth:`run` wrapper handles logging and timing,
    and guarantees an :class:`AgentResult` is always returned (never raises
    out of the pipeline -- failures are captured in ``result.errors``).
    """

    name: str = "BaseAgent"
    description: str = ""

    def __init__(self, context: AgentContext) -> None:
        self.context = context
        self.log = AuditLogger.get(f"agent.{self.name.lower()}")

    def run(self) -> AgentResult:
        """Execute the agent, capturing any exception into the result."""
        self.log.info(f">> {self.name} starting")
        try:
            result = self._run()
        except Exception as exc:  # never let an agent crash the pipeline
            self.log.exception(f"{self.name} raised an exception")
            result = AgentResult(
                agent=self.name,
                ok=False,
                message=f"{self.name} crashed: {exc}",
                errors=[f"{type(exc).__name__}: {exc}"],
            )
        # Phase 1 — mirror the agent's context mutations into session_state so
        # the ADK layer (track_project callback) and downstream ADK sub-agents
        # see a consistent, typed view of the pipeline output.
        try:
            self.context.sync_to_state()
        except Exception:  # syncing must never break a successful run
            self.log.debug("sync_to_state skipped (non-fatal)")
        # Wave D1 — publish a state-transition event on the in-process event
        # bus so DAG-style orchestration can react to state changes without
        # the state having to live in the LLM conversation. Fail-safe: a bus
        # error never breaks the run.
        try:
            from utils.event_bus import default_bus  # noqa: E402

            default_bus().publish(
                "agent.completed",
                {"agent": self.name, "ok": result.ok, "message": result.message},
            )
        except Exception:
            pass
        result.finalize()
        status = "[OK]" if result.ok else "[FAIL]"
        self.log.info(f"{status} {self.name} done: {result.message}")
        return result

    def _run(self) -> AgentResult:  # pragma: no cover - interface
        raise NotImplementedError

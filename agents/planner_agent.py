"""PlannerAgent -- produces a build plan before the pipeline executes.

Role
----
Runs FIRST (before DataAnalyzer in a two-phase flow, or after it when the
profile is already available). It inspects the user's description + any
available data profile and produces an ordered build plan stored in
``ctx.extra["plan"]``. In interactive mode it prints the plan and asks the
user to confirm before proceeding.

Phase 2 — the planner is now *intent-aware*
-------------------------------------------
When a Google API key is available the planner asks the LLM to shape the plan
based on the **business description** (not just the data profile):

  * should a cleaning step run, or is the user's emphasis elsewhere?
  * what report style fits the request (minimal / standard / rich)?
  * which phases are actually needed?

The LLM output is validated against the ``BuildPlan`` Pydantic schema; on any
validation failure (or when no key is set) the planner falls back to the
deterministic rule-based plan from ``create_build_plan`` — so the offline
baseline stays byte-identical and the pipeline is **fail-safe, not fail-open**.

The orchestrator consumes the plan (Phase 2): ``needs_cleaning`` decides
whether DataCleanerAgent runs, and ``report_style`` flows into the Phase 3
visual/measure selectors. A deterministic fallback order is always available
when the plan is missing or invalid.
"""
from __future__ import annotations

import json
from typing import Any

from agents.base import AgentResult, BaseAgent
from agents.schemas import BuildPlan, PlanStep
from utils import AuditLogger

_log = AuditLogger.get("agent.planner")


# Deterministic thresholds shared with the fallback planner.
_CLEAN_QUALITY_THRESHOLD = 90.0


def _deterministic_plan(
    description: str, data_profile: dict[str, Any] | None
) -> BuildPlan:
    """Rule-based fallback plan — mirrors the legacy ``create_build_plan`` output.

    Kept as the fail-safe path so the offline baseline is unchanged and the
    pipeline never depends on an LLM being reachable.
    """
    profile = data_profile or {}
    quality_score = profile.get("quality_score", 100)
    issues = profile.get("issues", [])

    steps: list[PlanStep] = []
    has_raw_data = bool(profile.get("schema"))
    needs_cleaning = False

    if has_raw_data:
        steps.append(PlanStep(
            phase="analyze", agent="DataAnalyzerAgent",
            action="Profile data quality (nulls, outliers, duplicates)",
            rationale="Understand the data before building a model on it.",
        ))
        if quality_score < _CLEAN_QUALITY_THRESHOLD or issues:
            needs_cleaning = True
            steps.append(PlanStep(
                phase="clean", agent="DataCleanerAgent",
                action="Apply cleaning steps based on the profile",
                rationale=f"Quality score is {quality_score}/100 with {len(issues)} issue(s).",
            ))

    steps.append(PlanStep(
        phase="schema", agent="SchemaAgent",
        action="Infer schema and write TMDL table definitions",
        rationale="Create the semantic model structure from the (cleaned) data.",
    ))
    steps.append(PlanStep(
        phase="relationship", agent="RelationshipAgent",
        action="Detect cross-table relationships (heuristic + LLM)",
        rationale="Link fact and dimension tables for a navigable model.",
    ))
    steps.append(PlanStep(
        phase="dax", agent="DAXAgent",
        action="Generate 5-10 DAX measures with best-practice formatting",
        rationale="Add analytical measures (totals, averages, time intelligence).",
    ))
    steps.append(PlanStep(
        phase="report", agent="ReportAgent",
        action="Build PBIR report pages with visuals",
        rationale="Turn the model into a visual dashboard.",
    ))
    steps.append(PlanStep(
        phase="validate", agent="ValidatorAgent",
        action="Structural + semantic validation, auto-fix trivial issues",
        rationale="Ensure Power BI Desktop can open the project.",
    ))
    steps.append(PlanStep(
        phase="review", agent="ReportReviewerAgent",
        action="Semantic review of the generated report",
        rationale="Check visuals reference real measures/columns, layout is sound.",
    ))

    return BuildPlan(
        steps=steps,
        needs_cleaning=needs_cleaning,
        report_style="standard",
        planner_reasoning="deterministic rule-based plan",
    )


def _llm_plan(description: str, data_profile: dict[str, Any] | None) -> BuildPlan | None:
    """Ask the LLM to shape the plan from the business description.

    Returns ``None`` when no API key is set or the LLM call / validation fails
    so the caller can fall back to the deterministic plan.
    """
    try:
        from utils.model_config import MissingAPIKeyError, get_llm_config
        from utils.retry import retry_sync
    except Exception:
        return None

    try:
        llm_config = get_llm_config()
    except MissingAPIKeyError as exc:
        _log.error(f"LLM provider misconfigured, falling back to deterministic: {exc}")
        return None
    if llm_config is None:
        return None

    profile = data_profile or {}
    profile_brief = json.dumps({
        "quality_score": profile.get("quality_score", 100),
        "issue_count": len(profile.get("issues", [])),
        "has_raw_data": bool(profile.get("schema")),
    })

    # P4: Include schema columns in the prompt so the LLM can make informed decisions
    schema_info = ""
    schema = profile.get("schema") if profile else None
    if schema and isinstance(schema, dict):
        columns = schema.get("columns", [])
        col_summary = ", ".join(
            f"{c['name']}({c.get('dataType', '?')})"
            for c in columns[:20]  # cap at 20 to avoid token bloat
        )
        schema_info = (
            f"\nSCHEMA_COLUMNS (first 20):\n{col_summary}\n"
            f"Table: {schema.get('table_name', 'unknown')}\n"
        )

    prompt = (
        "You are a Power BI build planner. Given a business description and a "
        "data quality profile, decide the build plan. Output ONLY JSON matching "
        "this schema (no prose):\n"
        "{\n"
        '  "steps": [{"phase": "string", "agent": "string", "action": "string", '
        '"rationale": "string"}],\n'
        '  "needs_cleaning": boolean,\n'
        '  "report_style": "minimal" | "standard" | "rich",\n'
        '  "planner_reasoning": "string"\n'
        "}\n\n"
        "Rules:\n"
        "- 'report_style': 'minimal' = a single simple table/card view (the "
        "user asked for something quick/simple); 'standard' = default dashboard; "
        "'rich' = multi-page with many visual types (user asked for comprehensive).\n"
        "- 'needs_cleaning': true only when data quality is poor OR the user's "
        "description emphasizes accuracy/quality. A simple request does NOT need "
        "cleaning even if quality is slightly below 90.\n"
        "- Include phases: analyze, clean (if needed), schema, relationship, dax, "
        "report, validate, review.\n"
        "- Consider the available columns when deciding report_style and dax measures.\n\n"
        f"BUSINESS_DESCRIPTION:\n{description}\n\n"
        f"DATA_PROFILE:\n{profile_brief}\n"
        f"{schema_info}"
    )

    def _call_once() -> str:
        from utils.model_config import get_text_completion
        return get_text_completion(prompt, llm_config)

    try:
        text = retry_sync(_call_once, retries=2, base_delay=1.0, max_delay=8.0)
    except Exception:
        return None

    # extract the JSON object (tolerate surrounding prose / code fences)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None

    # validate against the BuildPlan schema
    try:
        plan = BuildPlan(
            steps=[PlanStep(**s) for s in raw.get("steps", [])],
            needs_cleaning=bool(raw.get("needs_cleaning", False)),
            report_style=str(raw.get("report_style", "standard")),
            planner_reasoning=str(raw.get("planner_reasoning", "LLM-generated plan")),
        )
    except Exception:
        return None

    # safety: a plan with no steps is useless — fall back
    if not plan.steps:
        return None
    return plan


class PlannerAgent(BaseAgent):
    """Produces an intent-aware build plan and stores it for the orchestrator."""

    name = "PlannerAgent"
    description = (
        "You are the PlannerAgent. Given the user's business description and "
        "any available data profile, produce an ordered build plan (which "
        "agents run, in what order, and why) that reflects the user's intent — "
        "not just the data quality. Decide whether cleaning is needed and what "
        "report style fits. Validate the plan is compatible with available "
        "capabilities. In interactive mode, present the plan and ask the user "
        "to confirm before the pipeline starts."
    )

    def _run(self) -> AgentResult:
        ctx = self.context

        profile = ctx.extra.get("data_profile")

        # Try the LLM planner first (intent-aware); fall back to deterministic.
        plan = _llm_plan(ctx.business_description, profile)
        if plan is None:
            plan = _deterministic_plan(ctx.business_description, profile)

        # self-check: validate the plan uses only known phases.
        # We only fall back on hard errors (unknown phases, duplicate phases,
        # circular dependencies) — NOT on missing-critical-phases warnings.
        # A minimal or LLM-crafted plan that intentionally skips some phases
        # (e.g. a test plan with only "schema") is still a valid plan; the
        # orchestrator decides which agents actually run.
        from adk.tools.planner_tools import validate_plan
        validation = validate_plan(
            [{"phase": s.phase, "agent": s.agent, "action": s.action,
              "rationale": s.rationale} for s in plan.steps]
        )
        hard_errors = [
            e for e in validation.get("errors", [])
            if "unknown phase" in e or "duplicate phase" in e or "Circular" in e
        ]
        if hard_errors:
            # Only fall back on structurally broken plans (unknown/duplicate phases)
            self.log.warning(
                f"plan validation hard errors ({hard_errors}); "
                "falling back to deterministic plan"
            )
            plan = _deterministic_plan(ctx.business_description, profile)
        elif not validation.get("valid"):
            # Soft errors (e.g. missing critical phases) — log but keep the plan
            self.log.info(
                f"plan validation soft errors ({validation.get('errors', [])}); "
                "keeping LLM plan (soft errors do not force fallback)"
            )

        # store both the rich BuildPlan (for Phase 2/3 consumers) and the
        # legacy list-of-dicts shape (so StatusAgent + existing UI keep working)
        ctx.extra["plan"] = [
            {"phase": s.phase, "agent": s.agent, "action": s.action,
             "rationale": s.rationale}
            for s in plan.steps
        ]
        ctx.extra["build_plan"] = plan  # the typed BuildPlan object
        ctx.extra["needs_cleaning"] = plan.needs_cleaning
        ctx.extra["report_style"] = plan.report_style

        # interactive: ask clarification questions, then present + confirm
        if ctx.extra.get("interactive", False) and plan.steps:
            clarifications = self._ask_clarification_questions(plan)
            ctx.extra["clarifications"] = clarifications

            print("\n" + "=" * 60)
            print("  Build Plan:")
            print("=" * 60)
            for i, step in enumerate(plan.steps, 1):
                print(f"  {i}. [{step.phase}] {step.agent}: {step.action}")
                print(f"     → {step.rationale}")
            print(f"  Report style: {plan.report_style}")
            print(f"  Cleaning needed: {plan.needs_cleaning}")
            print("=" * 60)
            confirm = input("  Proceed with this plan? [Y/n]: ").strip().lower()
            if confirm and confirm not in ("y", "yes", ""):
                ctx.extra["plan_confirmed"] = False
                return AgentResult(
                    agent=self.name, ok=False,
                    message="User rejected the plan — aborting.",
                    errors=["plan rejected by user"],
                )
            ctx.extra["plan_confirmed"] = True
            print("=" * 60 + "\n")
        else:
            # Non-interactive: seed deterministic defaults so downstream agents
            # always find a clarifications dict in ctx.extra.
            ctx.extra.setdefault("clarifications", {})

        self.log.info(
            f"plan created: {plan.step_count} step(s), style={plan.report_style}, "
            f"clean={plan.needs_cleaning}, source={'llm' if plan.planner_reasoning != 'deterministic rule-based plan' else 'deterministic'}"
        )
        return AgentResult(
            agent=self.name, ok=True,
            message=f"Build plan ready: {plan.step_count} step(s) (style={plan.report_style}).",
            data={
                "step_count": plan.step_count,
                "phases": [s.phase for s in plan.steps],
                "needs_cleaning": plan.needs_cleaning,
                "report_style": plan.report_style,
                "clarifications": ctx.extra.get("clarifications", {}),
            },
        )

    # ------------------------------------------------------------------
    # Interactive clarification questions
    # ------------------------------------------------------------------

    @staticmethod
    def _ask_clarification_questions(plan: "BuildPlan") -> dict[str, str]:  # type: ignore[name-defined]
        """Ask the user intelligent clarification questions in interactive mode.

        Returns a dict of answers that downstream agents (BIReasoningAgent,
        VisualPlannerAgent) consume to personalise the output.

        Non-interactive paths never call this method — deterministic defaults
        are used instead (see ``ctx.extra.setdefault("clarifications", {})``) .
        """
        print("\n" + "-" * 60)
        print("  Dashboard Clarification Questions")
        print("  (Press Enter to accept the default shown in brackets)")
        print("-" * 60)

        answers: dict[str, str] = {}

        questions: list[dict] = [
            {
                "key": "audience",
                "prompt": "  1. Who is the primary audience? [analyst / executive / operational]",
                "default": "analyst",
                "valid": ("analyst", "executive", "operational", ""),
            },
            {
                "key": "device",
                "prompt": "  2. Target device? [desktop / mobile]",
                "default": "desktop",
                "valid": ("desktop", "mobile", ""),
            },
            {
                "key": "time_granularity",
                "prompt": "  3. Preferred time granularity? [monthly / daily / yearly / auto]",
                "default": "auto",
                "valid": ("monthly", "daily", "yearly", "auto", ""),
            },
            {
                "key": "num_pages",
                "prompt": "  4. Number of pages? [1 / 2-3 / as-many]",
                "default": "1" if plan.report_style != "rich" else "2-3",
                "valid": ("1", "2-3", "as-many", "as_many", ""),
            },
            {
                "key": "complexity",
                "prompt": "  5. Detail level? [simple / balanced / full]",
                "default": "balanced",
                "valid": ("simple", "balanced", "full", ""),
            },
        ]

        for q in questions:
            try:
                raw = input(q["prompt"] + ": ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                raw = ""
            value = raw if raw and raw in q["valid"] else q["default"]
            # Normalise "as-many" → "as_many"
            if value == "as-many":
                value = "as_many"
            answers[q["key"]] = value or q["default"]

        print("-" * 60 + "\n")
        return answers

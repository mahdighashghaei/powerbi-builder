"""OrchestratorAgent -- the top-level coordinator.

Role
----
This is the ADK-style orchestrator. It receives the user input (CSV/JSON path +
business description), builds a task plan, and delegates sequentially to the
specialized subagents:

    SchemaAgent -> DAXAgent -> ReportAgent -> ValidatorAgent

It:

* creates the run's output root (a fresh, isolated folder per run),
* instantiates the MCP :class:`PbipToolbox` bound to that root (so every write
  is contained + path-traversal-safe),
* threads a shared :class:`AgentContext` through the agents,
* collects each agent's :class:`AgentResult` into a run report,
* writes the top-level README + item.metadata files so Power BI Desktop can
  open the project,
* returns a :class:`RunReport` with the path to the finished .pbip folder.

If any agent fails, the orchestrator still runs the ValidatorAgent so the user
gets a concrete error report, then marks the run as failed.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.base import AgentContext, AgentResult, BaseAgent
from agents.bi_reasoning_agent import BIReasoningAgent
from agents.dax_agent import DAXAgent
from agents.data_analyzer_agent import DataAnalyzerAgent
from agents.data_cleaner_agent import DataCleanerAgent
from agents.insights_agent import InsightsAgent
from agents.planner_agent import PlannerAgent
from agents.read_pbip_agent import ReadPBIPAgent
from agents.read_pbix_agent import ReadPBIXAgent
from agents.relationship_agent import RelationshipAgent
from agents.report_agent import ReportAgent
from agents.report_reviewer_agent import ReportReviewerAgent
from agents.schema_agent import SchemaAgent, _safe_project_name
from agents.schemas import BuildSpec
from agents.status_agent import StatusAgent
from agents.validator_agent import ValidatorAgent
from mcp_server.server import PbipToolbox
from utils import AuditLogger, atomic_write_json, atomic_write_text, ensure_dir, utc_now_iso
from utils import pbip_paths as paths
from utils.date_table import build_date_table_tmdl, needs_date_table
from utils.explainability import get_tracker

log = AuditLogger.get("agent.orchestrator")


# ---------------------------------------------------------------------------
# Run report
# ---------------------------------------------------------------------------


@dataclass
class RunReport:
    """Final summary of an orchestrator run."""

    ok: bool
    project_name: str
    pbip_root: str
    started_at: str
    finished_at: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, Any] | None = None
    error: str | None = None

    def add(self, result: AgentResult) -> None:
        self.steps.append(
            {
                "agent": result.agent,
                "ok": result.ok,
                "message": result.message,
                "errors": result.errors,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "data": result.data,
            }
        )
        if not result.ok and not self.error:
            self.error = f"{result.agent}: {result.message}"

    def finalize(self) -> "RunReport":
        self.finished_at = utc_now_iso()
        self.ok = all(s["ok"] for s in self.steps)
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_name": self.project_name,
            "pbip_root": self.pbip_root,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": self.steps,
            "validation": self.validation,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class OrchestratorAgent(BaseAgent):
    """Coordinates the Schema -> DAX -> Report -> Validate pipeline.

    Unlike the leaf agents, the orchestrator's ``run`` takes the raw user inputs
    (not a pre-built context) and returns a :class:`RunReport`.
    """

    name = "OrchestratorAgent"
    description = (
        "You are the OrchestratorAgent. You receive a data file and a business "
        "description, plan the work, and delegate to specialized subagents in "
        "sequence: SchemaAgent, DAXAgent, ReportAgent, ValidatorAgent. You "
        "assemble the final .pbip folder and report the outcome."
    )

    def __init__(self, output_root: str | Path, log_file: str | Path | None = None,
                 log_level: str = "INFO") -> None:
        # NOTE: orchestrator does not use the BaseAgent context pattern because
        # it CREATES the context. We skip BaseAgent.__init__ intentionally.
        self.log = AuditLogger.get("agent.orchestrator")
        self.output_root = ensure_dir(output_root)
        # configure logging once, centrally
        AuditLogger.configure(log_file=log_file, level=log_level)

    def run(  # type: ignore[override]
        self,
        source_path: str | Path,
        business_description: str,
        project_name: str | None = None,
        input_mode: str = "create",
        theme_preset: str = "default",
        sheet: str | None = None,
        interactive: bool = False,
        num_pages: int = 0,
        visual_variety: str = "",
    ) -> RunReport:
        """Run the full pipeline. Returns a :class:`RunReport`.

        Args:
            source_path:  CSV/JSON/Excel file OR existing PBIP/PBIX folder/file.
            business_description: Plain-English intent (what to add/build).
            project_name: Override auto-derived name.
            input_mode:   "create" | "edit_pbip" | "edit_pbix" | "edit_excel"
            theme_preset: Theme preset key (default, modern_dark, corporate_blue, etc.)
            sheet:        Excel sheet name to read (``--sheet``; only used for
                          Excel sources; ``None`` = first sheet).
            interactive:  When True, pause and ask the user when the Data
                          Analyzer raises ambiguous questions (null impute vs
                          drop, outlier handling, etc.). When False, best-effort
                          decisions are applied with a warning log.
            num_pages:    Optional explicit page count. When > 0, overrides
                          the description-keyword-inferred report_style's
                          page count in ReportAgent — decide the exact page
                          count up front in this one call instead of
                          building, discovering the count is wrong, and
                          reactively adding/deleting pages afterward.
                          0 (default) preserves today's inferred behavior.
            visual_variety: "" (default, today's behavior) or "all" to also
                          include scatter/pie/kpi candidates in ReportAgent's
                          visual planning (previously only available via the
                          separate, additive build_report tool).
        """
        started_at = utc_now_iso()
        source = Path(source_path).expanduser().resolve()

        # validate source depending on mode
        if input_mode == "edit_pbip":
            source_ok = source.is_dir()
        else:
            source_ok = source.is_file()

        if not source_ok:
            report = RunReport(
                ok=False, project_name="unknown",
                pbip_root=str(self.output_root), started_at=started_at,
                error=f"Input not found: {source}",
            )
            report.finalize()
            return report

        pname = project_name or _safe_project_name(business_description)

        # 1) set up output directory
        if input_mode == "edit_pbip":
            # copy the existing PBIP into a fresh output dir so edits are isolated
            run_dir = ensure_dir(self.output_root / pname)
            self._copy_existing_pbip(source, run_dir)
            self.log.info(f"=== Run started (edit PBIP): project={pname} ===")
        else:
            run_dir = ensure_dir(self.output_root / pname)
            # In create mode, wipe any stale Report pages from a previous run
            # so orphan page folders (not in pages.json) don't accumulate and
            # confuse Desktop / the validator. The SemanticModel is rebuilt
            # from scratch too, so clearing the whole project dir is safe and
            # guarantees a clean, deterministic run. edit_excel is also a
            # create-from-scratch flow (it reads a raw Excel file).
            if input_mode in ("create", "edit_excel") and run_dir.exists():
                self._clear_stale_project(run_dir, pname)
            self.log.info(
                f"=== Run started ({input_mode}): project={pname} "
                f"source={source.name} ==="
            )

        toolbox = PbipToolbox(run_dir)

        # Reset the explainability tracker so decisions from a previous run in
        # the same process do not bleed into this run's decisions.log.json.
        get_tracker().reset()

        _extra: dict[str, Any] = {"excel_sheet": sheet, "interactive": interactive}
        if num_pages > 0:
            _extra["requested_num_pages"] = num_pages
        if visual_variety:
            _extra["requested_visual_variety"] = visual_variety

        ctx = AgentContext(
            business_description=business_description,
            source_path=source,
            toolbox=toolbox,
            project_name=pname,
            pbip_root=run_dir,
            input_mode=input_mode,
            existing_pbip_path=source if input_mode == "edit_pbip" else None,
            theme_preset=theme_preset,
            extra=_extra,
        )

        report = RunReport(
            ok=True, project_name=pname,
            pbip_root=str(run_dir), started_at=started_at,
        )

        # Predictive optimization (Upgrade 6): adjust scoring weights BEFORE any
        # agent runs, based on feedback history, business-domain keywords, and
        # data-quality signals. This seeds ctx.extra["scoring_weights"] so every
        # downstream candidate-selection call uses pre-biased priors rather than
        # reactive post-error adjustments alone.
        self._apply_predictive_weights(ctx, run_dir)

        # Adaptive Intelligence (Upgrade 7): initialise cross-run learning memory,
        # compute input complexity, and set candidate_count + adaptive_bias in
        # ctx.extra so every downstream agent uses them without coupling.
        # All code is wrapped in a fail-safe try/except — must never block the run.
        try:
            from utils.adaptive_learning import AdaptiveLearningLayer
            from utils.learning_memory import LearningMemory
            from utils.scoring import compute_complexity_score, candidate_count_from_complexity

            _lm = LearningMemory(run_dir / "learning_memory.json")
            _lm.load()

            # Pre-schema complexity estimate (business description + KPI list only;
            # schema columns will be 0 until SchemaAgent runs).
            _biz_kpis_pre: list[str] = []
            _biz_pre = ctx.extra.get("business_analysis")
            if _biz_pre is not None:
                _biz_kpis_pre = list(getattr(_biz_pre, "potential_kpis", []) or [])
            _complexity_pre = compute_complexity_score(
                schema_columns=[],
                kpi_list=_biz_kpis_pre,
                business_description=business_description,
            )
            _candidate_count = candidate_count_from_complexity(_complexity_pre)

            # Compute adaptive bias from learning memory using pre-schema cluster
            _cluster_pre = _lm.cluster_input(
                business_description=business_description,
                schema_columns=[],
                kpi_list=_biz_kpis_pre,
            )
            _success_pats = _lm.get_success_patterns(_cluster_pre)
            _fail_pats    = _lm.get_failure_patterns(_cluster_pre)
            _adaptive_bias = AdaptiveLearningLayer.compute_adaptive_bias(
                base_semantic_score=0.0,  # neutral — actual score unknown pre-generation
                success_patterns=_success_pats,
                failure_patterns=_fail_pats,
                current_context={
                    "description": business_description,
                    "kpis": _biz_kpis_pre,
                },
            )

            ctx.extra["candidate_count"]   = _candidate_count
            ctx.extra["adaptive_bias"]     = _adaptive_bias
            ctx.extra["_input_cluster"]    = _cluster_pre
            ctx.extra["_learning_memory"]  = _lm

            self.log.info(
                f"adaptive-learning: complexity={_complexity_pre:.3f} "
                f"candidate_count={_candidate_count} "
                f"adaptive_bias={_adaptive_bias:+.4f} "
                f"cluster='{_cluster_pre}'"
            )

            # Strategy Synthesis Layer (additive) -- triggered from CROSS-RUN
            # signals only (this run's Judge has not evaluated anything yet).
            # When this cluster has accumulated enough failures, or judge
            # overrides have historically been frequent, synthesize a new
            # strategy per domain and seed it into the pool BEFORE
            # Schema/DAX/Report run, so this very run can try it.
            try:
                _override_freq_pre = _lm.get_judge_override_frequency()
                if len(_fail_pats) >= 3 or _override_freq_pre > 0.3:
                    from utils.strategy_synthesizer import StrategySynthesizer
                    _synth_pre = StrategySynthesizer().synthesize_new_strategies(
                        domain_pool_ids={"dax": [], "schema": [], "visual": []},
                        judge_result=None,
                        failure_patterns=_fail_pats,
                    )
                    if any(_synth_pre.values()):
                        ctx.extra["synthesized_strategies"] = _synth_pre
                        self.log.info(
                            f"strategy synthesis (pre-pipeline): "
                            f"dax={len(_synth_pre['dax'])} "
                            f"schema={len(_synth_pre['schema'])} "
                            f"visual={len(_synth_pre['visual'])} "
                            f"(failure_patterns={len(_fail_pats)}, "
                            f"override_freq={_override_freq_pre:.2f})"
                        )
            except Exception as _synth_pre_exc:  # noqa: BLE001 — fail-safe
                self.log.warning(f"strategy synthesis (pre-pipeline) skipped: {_synth_pre_exc}")
        except Exception as _al_exc:  # noqa: BLE001 — fail-safe
            self.log.warning(f"adaptive learning init skipped: {_al_exc}")

        # 2) choose + run the right pipeline
        if input_mode == "edit_pbip":
            first_agent_cls = ReadPBIPAgent
            first_is_fatal = True
        elif input_mode == "edit_pbix":
            first_agent_cls = ReadPBIXAgent
            first_is_fatal = True
        elif input_mode == "edit_excel":
            # SchemaAgent handles Excel via the schema_inference path
            first_agent_cls = SchemaAgent
            first_is_fatal = True
        else:
            first_agent_cls = SchemaAgent
            first_is_fatal = True

        # Pre-pipeline agents (Planner, Analyzer, Cleaner) run only in
        # create / edit_excel mode where there's a raw data file to profile.
        # In edit_pbip / edit_pbix the model is already built — skip to schema.
        #
        # Phase 2 — the orchestrator now CONSUMES the PlannerAgent's plan:
        # ``needs_cleaning`` decides whether DataCleanerAgent runs. The
        # decision combines the planner's intent-aware verdict with the actual
        # quality score from the Analyzer (fail-safe: cleaning runs when EITHER
        # source says it's needed). When no plan is present the legacy
        # behaviour (always run the cleaner) is preserved.
        if input_mode in ("create", "edit_excel"):
            # 1) Planner — produces the intent-aware build plan
            presult = PlannerAgent(ctx).run()
            report.add(presult)
            if not presult.ok:
                self.log.warning("Planner returned ok=False — aborting")
                # planner rejection (user said no) is fatal
                self._write_item_metadata(ctx)
                ctx.extra["run_steps"] = [
                    {"agent": s.get("agent", "?"), "ok": s.get("ok", False),
                     "message": s.get("message", ""), "errors": s.get("errors", [])}
                    for s in report.steps
                ]
                sresult = StatusAgent(ctx).run()
                report.add(sresult)
                self._write_readme(ctx, report)
                report.finalize()
                return report

            # 1b) BIReasoningAgent — business intelligence analysis before data profiling.
            #     Advisory only (never blocks the pipeline). Produces structured
            #     reasoning consumed by VisualPlannerAgent for smarter page/visual planning.
            brresult = BIReasoningAgent(ctx).run()
            report.add(brresult)
            # bi_reasoning is always ok=True (fail-safe), so we never abort on failure.

            # 2) Analyzer — profiles data quality
            aresult = DataAnalyzerAgent(ctx).run()
            report.add(aresult)
            self._ask_user_questions(ctx)
            if not aresult.ok:
                profile = ctx.extra.get("data_profile") or {}
                blocking = profile.get("blocking_issues", [])
                if blocking:
                    self.log.error(
                        f"DataAnalyzer blocking issues — aborting: {blocking}"
                    )
                    self._write_item_metadata(ctx)
                    ctx.extra["run_steps"] = [
                        {"agent": s.get("agent", "?"), "ok": s.get("ok", False),
                         "message": s.get("message", ""), "errors": s.get("errors", [])}
                        for s in report.steps
                    ]
                    sresult = StatusAgent(ctx).run()
                    report.add(sresult)
                    self._write_readme(ctx, report)
                    report.finalize()
                    return report
                self.log.warning(
                    "DataAnalyzerAgent returned ok=False — continuing"
                )

            # 3) Cleaner — run only when needed (Phase 2 plan consumption).
            #    needs_cleaning = planner says so OR quality score is low.
            profile = ctx.extra.get("data_profile") or {}
            quality_score = profile.get("quality_score", 100)
            issues = profile.get("issues", [])
            plan_needs_cleaning = ctx.extra.get("needs_cleaning", False)
            quality_needs_cleaning = quality_score < 90 or bool(issues)
            if plan_needs_cleaning or quality_needs_cleaning:
                cresult = DataCleanerAgent(ctx).run()
                report.add(cresult)
                if not cresult.ok:
                    self.log.warning(
                        f"DataCleanerAgent returned ok=False — continuing"
                    )
            else:
                self.log.info(
                    "skipping DataCleanerAgent — plan says no cleaning needed "
                    f"and quality score is {quality_score}/100"
                )

        # RelationshipAgent runs after schema is loaded, before DAX/Report
        pipeline = [first_agent_cls, RelationshipAgent, DAXAgent, ReportAgent]
        schema_failed = False
        for agent_cls in pipeline:
            result = agent_cls(ctx).run()
            report.add(result)
            if not result.ok and agent_cls is first_agent_cls and first_is_fatal:
                # C4 fix: track schema failure so we can prevent downstream agents
                schema_failed = True
                break
            # After SchemaAgent succeeds: refine complexity with actual schema columns
            # so DAXAgent and later agents get a more accurate candidate count + bias.
            if agent_cls is first_agent_cls and result.ok and ctx.schema:
                try:
                    from utils.adaptive_learning import AdaptiveLearningLayer
                    from utils.learning_memory import LearningMemory
                    from utils.scoring import (
                        compute_complexity_score, candidate_count_from_complexity,
                    )
                    _actual_cols = ctx.schema.get("columns", [])
                    _biz_schema = ctx.extra.get("business_analysis")
                    _biz_kpis_actual: list[str] = (
                        list(getattr(_biz_schema, "potential_kpis", []) or [])
                        if _biz_schema else []
                    )
                    _complexity_actual = compute_complexity_score(
                        schema_columns=_actual_cols,
                        kpi_list=_biz_kpis_actual,
                        business_description=business_description,
                    )
                    _candidate_count_actual = candidate_count_from_complexity(
                        _complexity_actual
                    )
                    # Refine cluster with real column count
                    _lm_actual: LearningMemory | None = ctx.extra.get("_learning_memory")  # type: ignore[assignment]
                    if _lm_actual is not None:
                        _cluster_actual = _lm_actual.cluster_input(
                            business_description=business_description,
                            schema_columns=_actual_cols,
                            kpi_list=_biz_kpis_actual,
                        )
                        _success_actual = _lm_actual.get_success_patterns(_cluster_actual)
                        _fail_actual    = _lm_actual.get_failure_patterns(_cluster_actual)
                        _bias_actual = AdaptiveLearningLayer.compute_adaptive_bias(
                            base_semantic_score=0.0,
                            success_patterns=_success_actual,
                            failure_patterns=_fail_actual,
                            current_context={
                                "description": business_description,
                                "columns": [c["name"] for c in _actual_cols],
                                "kpis": _biz_kpis_actual,
                            },
                        )
                        ctx.extra["adaptive_bias"]  = _bias_actual
                        ctx.extra["_input_cluster"] = _cluster_actual
                    ctx.extra["candidate_count"] = _candidate_count_actual
                    self.log.info(
                        f"adaptive-learning (post-schema): "
                        f"complexity={_complexity_actual:.3f} "
                        f"candidate_count={_candidate_count_actual}"
                    )
                except Exception as _rs_exc:  # noqa: BLE001
                    self.log.warning(f"adaptive post-schema refinement skipped: {_rs_exc}")

                # Semantic Truth Layer (system stabilization): discover, ONCE,
                # what the numeric columns actually mean (net vs. gross vs.
                # cost vs. profit) by empirically testing arithmetic
                # relationships on the real data, instead of guessing from
                # column names. Cached per schema fingerprint via
                # LearningMemory so the SAME schema shape never triggers
                # rediscovery on a future run.
                _semantic_model: dict[str, Any] | None = None
                try:
                    from utils.semantic_model import (
                        compute_schema_fingerprint, discover_semantic_relationships,
                        validate_cached_semantic_model,
                    )
                    _lm_sem = ctx.extra.get("_learning_memory")
                    _fp = compute_schema_fingerprint(ctx.schema.get("columns", []))
                    ctx.extra["_schema_fingerprint"] = _fp
                    _cached_model = _lm_sem.get_semantic_model(_fp) if _lm_sem is not None else None

                    _has_raw_csv = (
                        ctx.input_mode in ("create", "edit_excel")
                        and source and source.is_file()
                        and source.suffix.lower() == ".csv"
                    )
                    _df_sem = None
                    if _has_raw_csv:
                        # Deliberately read the ORIGINAL raw file (`source`,
                        # still in scope from the top of this method), not
                        # `ctx.source_path` — DataCleanerAgent may have
                        # already redirected that to a cleaned copy whose
                        # per-column outlier capping/imputation can silently
                        # break the exact cross-column arithmetic (e.g.
                        # "Gross Sales - Discounts = Sales") that this layer
                        # exists to discover. The raw file is what actually
                        # encodes the true, undistorted business relationships.
                        import pandas as _pd
                        _df_sem = _pd.read_csv(source)

                    # Part 2 robustness fix: a schema fingerprint is (name,
                    # dataType) only — two genuinely different datasets that
                    # happen to share a column-name/type signature (e.g. a
                    # project name reused for unrelated data) collide on the
                    # same fingerprint. Re-validate a cache hit's actual
                    # relationships against THIS run's data before trusting
                    # it; a stale/wrong cache is worse than no cache at all.
                    if _cached_model and _df_sem is not None:
                        if validate_cached_semantic_model(_cached_model, _df_sem):
                            _semantic_model = _cached_model
                            self.log.info(
                                f"semantic model: reused from cache, validated "
                                f"(fingerprint={_fp})"
                            )
                        else:
                            self.log.warning(
                                f"semantic model: cached model FAILED validation against "
                                f"this run's data (fingerprint={_fp}, possible schema-"
                                "fingerprint collision with an unrelated dataset) — "
                                "rediscovering fresh instead of trusting it"
                            )
                            _cached_model = None  # force rediscovery below
                    elif _cached_model:
                        # No raw data to validate against (edit_pbip/edit_pbix) —
                        # trust the cache as before; there's nothing more to check.
                        _semantic_model = _cached_model
                        self.log.info(f"semantic model: reused from cache, unvalidated "
                                      f"(fingerprint={_fp}, no raw data available to re-check)")

                    if _semantic_model is None and _df_sem is not None:
                        from agents.dax_agent import _classify_columns as _classify_cols_sem
                        _buckets_sem = _classify_cols_sem(ctx.schema.get("columns", []))
                        _discovery_cols = _buckets_sem.get("amount", []) + _buckets_sem.get("qty", [])
                        _semantic_model = discover_semantic_relationships(_df_sem, _discovery_cols)
                        if _lm_sem is not None and _semantic_model.get("relationships"):
                            _lm_sem.set_semantic_model(_fp, _semantic_model)
                        self.log.info(
                            f"semantic model: discovered "
                            f"{len(_semantic_model.get('relationships', []))} relationship(s), "
                            f"canonical={_semantic_model.get('canonical_metrics', {})}"
                        )
                    if _semantic_model:
                        ctx.extra["semantic_model"] = _semantic_model
                except Exception as _sem_exc:  # noqa: BLE001
                    self.log.warning(f"semantic model discovery skipped: {_sem_exc}")

                # Business-aware KPI Prioritization Layer (additive): rank the
                # amount-bucket columns by business importance ONCE, right
                # after the schema is finalized, so DAXAgent, ReportAgent,
                # InsightsAgent, and JudgeLayer all anchor on the same
                # highest-priority KPI instead of each independently picking
                # "the first monetary column in raw CSV order." Tie-breaks
                # now prefer the Semantic Truth Layer's discovered net/gross
                # structure over raw column order (the prior last-resort).
                _buckets_kpi: dict[str, list] = {}
                try:
                    from agents.dax_agent import _classify_columns
                    from utils.kpi_prioritizer import rank_kpi_candidates
                    _buckets_kpi = _classify_columns(ctx.schema.get("columns", []))
                    _prioritized_kpis = rank_kpi_candidates(
                        _buckets_kpi.get("amount", []),
                        ctx.extra.get("business_analysis"),
                        business_description,
                        semantic_model=_semantic_model,
                    )
                    if _prioritized_kpis:
                        ctx.extra["prioritized_kpis"] = _prioritized_kpis
                        self.log.info(
                            f"KPI prioritization: primary='{_prioritized_kpis[0]}' "
                            f"order={_prioritized_kpis}"
                        )
                except Exception as _kpi_exc:  # noqa: BLE001
                    self.log.warning(f"KPI prioritization skipped: {_kpi_exc}")

                # Concept Coverage Enforcement: extract explicitly-named
                # business concepts and synthesize derived-KPI candidates
                # (Margin %, Discount Rate %, Cost Ratio %) ONCE, so
                # DAXAgent's guarantee mechanism and JudgeLayer's coverage
                # check both work from the same list.
                try:
                    from utils.concept_coverage import extract_concepts
                    from utils.kpi_prioritizer import derive_candidate_kpis
                    _concepts = extract_concepts(business_description)
                    if _concepts:
                        ctx.extra["business_concepts"] = _concepts
                        _derived_kpis = derive_candidate_kpis(
                            _semantic_model, _buckets_kpi.get("amount", []),
                        )
                        ctx.extra["derived_kpi_candidates"] = _derived_kpis
                        self.log.info(
                            f"concept coverage: named={_concepts} "
                            f"derived_candidates={[d['name'] for d in _derived_kpis]}"
                        )
                except Exception as _cc_exc:  # noqa: BLE001
                    self.log.warning(f"concept extraction skipped: {_cc_exc}")

                # Binary-Outcome KPI Synthesis: detect a binary categorical
                # outcome column (e.g. "y" = yes/no in a marketing dataset)
                # ONCE, using distinct_count/distinct_values already computed
                # during schema inference (ctx.extra["data_profile"] — no
                # re-read of the source file). Closes the "no monetary amount
                # column at all" gap that Concept Coverage Enforcement can't
                # help with (its concepts are finance-vocabulary only).
                try:
                    from utils.kpi_prioritizer import detect_outcome_column
                    _data_profile = ctx.extra.get("data_profile") or {}
                    _outcome = detect_outcome_column(
                        ctx.schema.get("columns", []), _data_profile, business_description,
                    )
                    if _outcome:
                        ctx.extra["outcome_column"] = _outcome
                        self.log.info(f"outcome column detected: {_outcome}")
                except Exception as _oc_exc:  # noqa: BLE001
                    self.log.warning(f"outcome column detection skipped: {_oc_exc}")
        # C4 fix: if the first (schema) agent failed, skip DAX/Report — they
        # depend on a schema and will produce garbage output without it.
        if schema_failed:
            self.log.warning(
                "Schema agent failed — skipping DAX/Report (dependency not met)"
            )

        # 3) write top-level project entry files (.pbip, item.config.json,
        #    definition.pbism, definition.pbir) BEFORE validation so the
        #    validator can verify them.
        self._write_item_metadata(ctx)

        # 4) always run the validator if the schema landed (even on partial fail)
        if ctx.schema is not None:
            vresult = ValidatorAgent(ctx).run()
            report.add(vresult)
            report.validation = vresult.data

            # 4a) Phase 4 — feedback loop: if the validator found routable
            # high-severity issues, re-run the responsible agent with the
            # failure context in session_state, then re-validate. Capped at
            # MAX_FIX_RETRIES to avoid infinite loops; after the cap the run
            # degrades gracefully to the current error report (fail-safe).
            rresult = ReportReviewerAgent(ctx).run()
            report.add(rresult)
            ctx.extra["review"] = rresult.data

            # Global Judge: cross-agent consistency evaluation.
            # Runs AFTER the full pipeline so it can compare outputs from
            # SchemaAgent, DAXAgent, and ReportAgent simultaneously.
            # Fail-safe — a judge error must never break a successful build.
            try:
                from utils.judge import JudgeLayer
                judge_result = JudgeLayer().evaluate(ctx)
                ctx.extra["judge_result"] = judge_result  # type: ignore[assignment]
                if not judge_result["consistent"]:
                    self.log.warning(
                        f"Judge: consistency_score={judge_result['consistency_score']:.2f}, "
                        f"conflicts={judge_result['conflicts']}"
                    )
                else:
                    self.log.info(
                        f"Judge: consistent=True "
                        f"score={judge_result['consistency_score']:.2f} "
                        f"kpi_coverage={judge_result['kpi_coverage']:.2f}"
                    )
            except Exception:
                pass  # judge must never break the build

            # Cross-Agent Semantic Consistency Check (Upgrade 5).
            # Runs AFTER the Judge so both evaluate the identical final state.
            # Result stored in ctx.extra["consistency_result"] so the feedback
            # loop can consume penalty signals for weight adjustment.
            # Fail-safe — never breaks a successful build.
            try:
                from utils.consistency import CrossAgentConsistencyChecker
                consistency_result = CrossAgentConsistencyChecker().check(ctx)
                ctx.extra["consistency_result"] = consistency_result
                if not consistency_result["aligned"]:
                    self.log.warning(
                        f"CrossAgentConsistency: "
                        f"score={consistency_result['alignment_score']:.2f}, "
                        f"penalties={consistency_result['penalties']}"
                    )
                else:
                    self.log.info(
                        f"CrossAgentConsistency: aligned=True "
                        f"score={consistency_result['alignment_score']:.2f}"
                    )
            except Exception:
                pass  # consistency check must never break the build

            # Audit log every judge override_action so they appear in the
            # decisions trail. The feedback loop below will consume them as
            # routable re-run directives on its first attempt.
            _judge_ctx: dict = ctx.extra.get("judge_result") or {}
            for _oa in (_judge_ctx.get("override_actions") or []):
                self.log.warning(
                    f"Judge override_action: agent={_oa.get('agent')} "
                    f"action={_oa.get('action')} "
                    f"severity={_oa.get('severity')} "
                    f"reason={_oa.get('reason')}"
                )

            # Policy optimiser: consume judge policy_adjustments to update
            # scoring weights and candidate count for the feedback-loop re-runs.
            # Fail-safe — any error here must never break a successful build.
            try:
                from utils.explainability import log_decision as _ld
                _policy_adjs: list[dict] = _judge_ctx.get("policy_adjustments") or []
                if _policy_adjs:
                    _cur_weights: dict[str, float] = dict(
                        ctx.extra.get("scoring_weights") or {}
                    )
                    if not _cur_weights:
                        from utils.scoring import DEFAULT_WEIGHTS
                        _cur_weights = dict(DEFAULT_WEIGHTS)
                    _cur_count: int = int(ctx.extra.get("candidate_count", 5))
                    _adj_log: list[str] = []
                    for _pa in _policy_adjs:
                        _trigger = _pa.get("trigger", "?")
                        _delta: dict = _pa.get("weight_delta", {}) or {}
                        _cnt_bias: int = int(_pa.get("candidate_count_bias", 0))
                        for _dim, _d in _delta.items():
                            if _dim in _cur_weights:
                                _cur_weights[_dim] = min(
                                    0.60, max(0.01, _cur_weights[_dim] + float(_d))
                                )
                                _adj_log.append(f"{_trigger}:{_dim}+{_d:.3f}")
                        _cur_count = min(12, max(3, _cur_count + _cnt_bias))
                    # Renormalise weights to probability simplex
                    _total_pa = sum(_cur_weights.values())
                    if _total_pa > 0:
                        _cur_weights = {k: v / _total_pa for k, v in _cur_weights.items()}
                    ctx.extra["scoring_weights"]  = _cur_weights
                    ctx.extra["candidate_count"]  = _cur_count
                    _ld(
                        agent="Orchestrator",
                        decision_type="weight_adjustment",
                        subject="judge_policy_adjustments",
                        rationale=(
                            f"Applied {len(_policy_adjs)} judge policy adjustment(s): "
                            + ", ".join(_adj_log)
                            + f". candidate_count → {_cur_count}."
                        ),
                        confidence=0.9,
                        extra={
                            "policy_adjustments": _policy_adjs,
                            "weights_after": _cur_weights,
                            "candidate_count_after": _cur_count,
                        },
                    )
                    self.log.info(
                        f"judge policy: {len(_policy_adjs)} adjustment(s) applied, "
                        f"candidate_count={_cur_count}"
                    )
            except Exception as _pa_exc:  # noqa: BLE001
                self.log.warning(f"policy adjustment consumption skipped: {_pa_exc}")

            self._run_feedback_loop(ctx, report)

            # Business Insights Layer: advisory narrative pass over the
            # FINAL post-feedback-loop state (final measures, final judge
            # result, final report plan). Never blocks the build.
            iresult = InsightsAgent(ctx).run()
            report.add(iresult)

        # 4c) Status — collect a final run summary. Pass the steps via
        # ctx.extra (not a private attribute) so StatusAgent can read them.
        ctx.extra["run_steps"] = [
            {"agent": s.get("agent", "?"), "ok": s.get("ok", False),
             "message": s.get("message", ""), "errors": s.get("errors", [])}
            for s in report.steps
        ]
        sresult = StatusAgent(ctx).run()
        report.add(sresult)

        # 5) write README summarising the run
        self._write_readme(ctx, report)
        # 5b) write the versioned build specification (enduring, versioned asset)
        self._write_spec(ctx, report)
        # 5c) write explainability decision log (fail-safe: never breaks the run)
        self._write_decisions_log(ctx)

        # Cross-run learning memory: record this run's outcome so future runs
        # can bias candidate scoring toward historically-winning strategies.
        # Best-effort — errors must never fail a successful build.
        try:
            _lm_final: Any | None = ctx.extra.get("_learning_memory")
            if _lm_final is not None:
                _cluster_final: str = ctx.extra.get("_input_cluster", "unknown")
                _judge_final: dict = ctx.extra.get("judge_result") or {}
                _judge_overridden = bool(_judge_final.get("override_actions"))
                _run_success = all(s["ok"] for s in report.steps)

                # Record outcome for the winning DAX candidate (if known)
                _dax_cands: list[dict] = list(ctx.extra.get("dax_candidates") or [])
                if _dax_cands:
                    _winner = max(_dax_cands, key=lambda c: c.get("total", 0.0))
                    _winner_id = _winner.get("candidate_id", "unknown")
                    _lm_final.record_outcome(
                        cluster=_cluster_final,
                        candidate_id=_winner_id,
                        semantic_score=float(_winner.get("semantic_total", 0.0)),
                        success=_run_success,
                        judge_overridden=_judge_overridden,
                        context={
                            "description": business_description,
                            "kpi_coverage": _judge_final.get("kpi_coverage", 1.0),
                            "consistency_score": _judge_final.get("consistency_score", 1.0),
                            # System stabilization: richer context for future
                            # failure-pattern / adaptive-bias reasoning —
                            # reuses the existing recording mechanism, no new
                            # storage added.
                            "concept_coverage_score": _judge_final.get("concept_coverage_score", 1.0),
                            "semantic_correctness_score": _judge_final.get("semantic_correctness_score", 1.0),
                            "kpi_appropriateness_score": _judge_final.get("kpi_appropriateness_score", 1.0),
                            "schema_fingerprint": ctx.extra.get("_schema_fingerprint", ""),
                        },
                    )
                    # Strategy Synthesis feedback loop (additive): track
                    # per-strategy success rate (synthesized vs base) so
                    # weak strategies decay and eventually get pruned from
                    # future synthesis/candidate pools.
                    _lm_final.record_strategy_outcome(
                        strategy_id=_winner_id,
                        success=_run_success,
                        is_synthesized=_winner_id.startswith("synth_"),
                    )
                    _lm_final.decay_weak_strategies()
                    _pruned = _lm_final.prune_strategies()
                    if _pruned:
                        self.log.info(f"strategy synthesis: pruned weak strategies {_pruned}")
                _lm_final.save()
                self.log.info(
                    f"learning memory saved: cluster='{_cluster_final}' "
                    f"success={_run_success} "
                    f"override_freq={_lm_final.get_judge_override_frequency():.2f}"
                )
        except Exception as _lm_exc:  # noqa: BLE001
            self.log.warning(f"learning memory record/save skipped: {_lm_exc}")

        report.finalize()
        status = "SUCCESS" if report.ok else "FAILED"
        self.log.info(
            f"=== Run {status}: project={pname} steps={len(report.steps)} "
            f"errors={sum(len(s['errors']) for s in report.steps)} ==="
        )
        return report

    # ------------------------------------------------------------------
    # Phase 4 — feedback loop + predictive optimization
    # ------------------------------------------------------------------

    #: Maximum number of fix-retry iterations before graceful degradation.
    MAX_FIX_RETRIES = 3

    def _apply_predictive_weights(self, ctx: AgentContext, run_dir: Path) -> None:
        """Adjust scoring weights BEFORE generation based on context signals.

        Upgrade 6 — Predictive Optimization: rather than only shifting weights
        reactively (after validation errors), we pre-bias them using three
        independent signals collected *before* any agent runs:

          Signal 1 — Historical feedback
            Reads ``feedback_history.json`` written by previous runs of this
            project. When recent runs had quality_score < 70, the weights used
            at failure time are nudged toward the current weights (30 % blend),
            so the next run starts closer to the corrective posture.

          Signal 2 — Business-domain keywords
            Scans ``business_description`` for domain tokens (finance, ops,
            marketing, hr, executive …) and applies domain-specific priors
            (e.g. finance → boost kpi_alignment + business_value).

          Signal 3 — Data-quality score
            When DataAnalyzerAgent has already populated ``data_profile``,
            a low quality_score (< 70) applies a +0.10 boost to data_coverage
            so schema and DAX candidates that maximise coverage are preferred.

        All deltas are applied then renormalised to sum=1.0 (probability simplex)
        and written to ``ctx.extra["scoring_weights"]`` so DAXAgent, SchemaAgent,
        and VisualPlannerAgent use updated priors from their very first call.

        Fail-safe: any exception is logged and swallowed — predictive adjustment
        must never block or alter the pipeline's success/failure outcome.
        """
        import json as _json
        from utils.scoring import DEFAULT_WEIGHTS
        from utils.explainability import log_decision

        try:
            weights: dict[str, float] = dict(
                ctx.extra.get("scoring_weights") or DEFAULT_WEIGHTS  # type: ignore[arg-type]
            )

            priors: dict[str, float] = {}  # dimension → cumulative delta
            rationale_parts: list[str] = []

            # --- Signal 1: historical feedback from prior runs ---------------
            feedback_path = run_dir / "feedback_history.json"
            if feedback_path.exists():
                try:
                    with open(feedback_path, encoding="utf-8") as _fh:
                        _history: list[dict] = _json.load(_fh)
                    if _history:
                        _recent_fails = [
                            h for h in _history[-10:]
                            if isinstance(h, dict) and h.get("quality_score", 100) < 70
                        ]
                        if _recent_fails:
                            _last_fail_w: dict[str, float] = (
                                _recent_fails[-1].get("scoring_weights") or {}
                            )
                            for _dim, _val in _last_fail_w.items():
                                if _dim in weights:
                                    # 30 % nudge toward failure-time weights
                                    priors[_dim] = (
                                        priors.get(_dim, 0.0)
                                        + (_val - weights[_dim]) * 0.30
                                    )
                            rationale_parts.append(
                                f"history: {len(_recent_fails)} recent failure(s) — "
                                "30 % blend toward failure-time weights"
                            )
                except Exception:
                    pass  # history read is best-effort

            # --- Signal 2: business-domain keyword priors --------------------
            _desc_lower = (ctx.business_description or "").lower()
            _DOMAIN_PRIORS: list[tuple[frozenset[str], str, float]] = [
                (frozenset({"revenue", "profit", "sales", "finance", "financial",
                            "cost", "margin"}),        "kpi_alignment",   0.08),
                (frozenset({"revenue", "profit", "finance", "financial"}),
                                                        "business_value",  0.06),
                (frozenset({"operations", "operational", "inventory", "warehouse",
                            "logistics", "supply"}),   "data_coverage",   0.07),
                (frozenset({"marketing", "customer", "campaign",
                            "acquisition", "churn", "retention"}),
                                                        "visual_quality",  0.06),
                (frozenset({"marketing", "customer", "campaign"}),
                                                        "kpi_alignment",   0.05),
                (frozenset({"hr", "employee", "headcount",
                            "workforce", "payroll", "talent"}),
                                                        "data_coverage",   0.05),
                (frozenset({"hr", "employee", "headcount", "workforce"}),
                                                        "interpretability", 0.06),
                (frozenset({"executive", "ceo", "cfo", "board",
                            "c-suite", "leadership"}),  "business_value",  0.08),
                (frozenset({"executive", "ceo", "cfo", "board"}),
                                                        "kpi_alignment",   0.06),
                (frozenset({"detail", "drill", "granular",
                            "row-level", "transaction"}),
                                                        "data_coverage",   0.07),
            ]
            _desc_tokens = frozenset(_desc_lower.split())
            _domain_hits: list[str] = []
            for _kws, _dim, _delta in _DOMAIN_PRIORS:
                if _kws & _desc_tokens:
                    priors[_dim] = priors.get(_dim, 0.0) + _delta
                    _domain_hits.append(f"+{_delta:.2f}→{_dim}")
            if _domain_hits:
                rationale_parts.append(
                    f"domain keywords: {'; '.join(_domain_hits[:6])}"
                )

            # --- Signal 3: data-quality score (if analyzer ran first) --------
            _profile = ctx.extra.get("data_profile") or {}
            _q = _profile.get("quality_score")
            if _q is not None:
                if _q < 70:
                    priors["data_coverage"] = priors.get("data_coverage", 0.0) + 0.10
                    rationale_parts.append(
                        f"low data quality (q={_q}) → +0.10→data_coverage"
                    )
                elif _q < 85:
                    priors["data_coverage"] = priors.get("data_coverage", 0.0) + 0.05
                    rationale_parts.append(
                        f"moderate data quality (q={_q}) → +0.05→data_coverage"
                    )

            # --- Apply priors + renormalise ----------------------------------
            if not priors:
                return  # no signals → leave defaults unchanged

            _w_before = dict(weights)
            for _dim, _delta in priors.items():
                if _dim in weights:
                    weights[_dim] = min(0.60, max(0.01, weights[_dim] + _delta))
            _total_w = sum(weights.values())
            if _total_w > 0:
                weights = {k: v / _total_w for k, v in weights.items()}

            ctx.extra["scoring_weights"] = weights  # type: ignore[assignment]

            log_decision(
                agent="Orchestrator",
                decision_type="weight_adjustment",
                subject="predictive_priors",
                rationale=(
                    "Predictive weight priors applied BEFORE generation. "
                    + " | ".join(rationale_parts)
                ),
                confidence=0.85,
                extra={"weights_before": _w_before, "weights_after": weights},
            )
            self.log.info(
                "predictive weights: "
                f"bv={weights['business_value']:.3f} "
                f"ka={weights['kpi_alignment']:.3f} "
                f"dc={weights['data_coverage']:.3f} "
                f"vq={weights['visual_quality']:.3f} "
                f"ip={weights['interpretability']:.3f}"
            )

        except Exception as _exc:  # fail-safe — must never block the pipeline
            self.log.warning(f"predictive weight adjustment skipped: {_exc}")

    # Map agent_responsible (as routed by the validator) → the agent class to
    # re-run. Only the agents that own a fixable artefact are listed; an empty
    # agent_responsible (non-routable issue) is skipped.
    _FIX_AGENTS: dict[str, type[BaseAgent]] = {}  # populated lazily below

    def _fix_agent_cls(self, agent_name: str) -> type[BaseAgent] | None:
        """Resolve a routed agent name to the agent class to re-run."""
        # lazy population to avoid circular import at module load
        if not self._FIX_AGENTS:
            self._FIX_AGENTS = {
                "SchemaAgent": SchemaAgent,
                "DAXAgent": DAXAgent,
                "ReportAgent": ReportAgent,
                "RelationshipAgent": RelationshipAgent,
            }
        return self._FIX_AGENTS.get(agent_name)

    def _run_feedback_loop(self, ctx: AgentContext, report: RunReport) -> None:
        """Retry routable validation issues via the responsible agent.

        Enhanced (F1-F4, F6):
          F1: fix_context is stored so agents can read it before re-running.
          F2: includes WARNING-severity issues, not just ERRORs.
          F3: quality scoring tracks improvement across iterations.
          F4: feedback is persisted to a JSON store for future runs.
          F6: only the responsible agent is re-run (targeted fix).
        """
        from utils.scoring import DEFAULT_WEIGHTS
        from utils.explainability import log_decision

        # Initialise scoring weights from ctx (may have been set by a previous
        # run or by a caller) — or fall back to the published defaults.
        weights: dict[str, float] = dict(
            ctx.extra.get("scoring_weights") or DEFAULT_WEIGHTS  # type: ignore[arg-type]
        )

        quality_history: list[int] = []

        for attempt in range(self.MAX_FIX_RETRIES):
            validation = ctx.validation or {}
            issues = validation.get("issues", []) or []

            # F2: include both ERROR and WARNING severity issues
            routable = [
                i for i in issues
                if i.get("severity") in ("error", "warning")
                and i.get("agent_responsible")
            ]

            # Also check reviewer findings for ghost refs etc
            review = ctx.extra.get("review") or {}
            review_issues = review.get("issues", []) or []
            routable_review = [
                i for i in review_issues
                if isinstance(i, dict)
                and i.get("severity") in ("error", "warning")
                and i.get("agent_responsible")
            ]
            all_routable = routable + routable_review

            # Upgrade 4 — Judge override_actions as routable directives.
            # Injected on the first attempt only so the judge's authoritative
            # verdicts trigger exactly one targeted re-run cycle and do not
            # accumulate across retries (re-validation results guide later passes).
            if attempt == 0:
                _judge_res: dict = ctx.extra.get("judge_result") or {}
                for _oa in (_judge_res.get("override_actions") or []):
                    _oa_agent = _oa.get("agent", "")
                    if _oa_agent == "ALL":
                        # Global override: schedule all fixable agents
                        for _fa in ("DAXAgent", "SchemaAgent", "ReportAgent"):
                            all_routable.append({
                                "severity": _oa.get("severity", "error"),
                                "agent_responsible": _fa,
                                "message": _oa.get("reason", "judge global override"),
                                "context": {
                                    "issue_type": "judge_override",
                                    "detail": _oa.get("detail", ""),
                                },
                            })
                    elif _oa_agent:
                        all_routable.append({
                            "severity": _oa.get("severity", "error"),
                            "agent_responsible": _oa_agent,
                            "message": _oa.get("reason", "judge override"),
                            "context": {
                                "issue_type": "judge_override",
                                "detail": _oa.get("detail", ""),
                            },
                        })

                # Strategy Synthesis Layer (additive) -- this run's Judge has
                # now evaluated once, so live strategy_gaps/policy_adjustments
                # are available. Merge any newly synthesized specs into
                # ctx.extra["synthesized_strategies"] (augment, don't replace,
                # what the pre-pipeline pass may already have seeded) so the
                # DAX/Schema/Report re-runs below can try them immediately.
                try:
                    if _judge_res:
                        from utils.strategy_synthesizer import StrategySynthesizer
                        _lm_fb = ctx.extra.get("_learning_memory")
                        _fail_pats_fb = (
                            _lm_fb.get_failure_patterns(ctx.extra.get("_input_cluster", ""))
                            if _lm_fb is not None else []
                        )
                        _existing_synth: dict = ctx.extra.get("synthesized_strategies") or {}
                        _pool_ids = {
                            "dax":    [c.get("candidate_id") for c in (ctx.extra.get("dax_candidates") or [])],
                            "schema": [c.get("candidate_id") for c in (ctx.extra.get("schema_candidates") or [])],
                            "visual": [c.get("candidate_id") for c in (ctx.extra.get("visual_plan_candidates") or [])],
                        }
                        _synth_live = StrategySynthesizer().synthesize_new_strategies(
                            domain_pool_ids=_pool_ids,
                            judge_result=_judge_res,
                            failure_patterns=_fail_pats_fb,
                        )
                        _merged_synth: dict[str, list] = {
                            k: list(_existing_synth.get(k, [])) for k in ("dax", "schema", "visual")
                        }
                        for _domain, _specs in _synth_live.items():
                            _known_ids = {s.get("strategy_id") for s in _merged_synth.get(_domain, [])}
                            for _spec in _specs:
                                if _spec.get("strategy_id") not in _known_ids:
                                    _merged_synth.setdefault(_domain, []).append(_spec)
                        if any(_merged_synth.values()):
                            ctx.extra["synthesized_strategies"] = _merged_synth
                            self.log.info(
                                f"strategy synthesis (feedback-loop): "
                                f"dax={len(_merged_synth['dax'])} "
                                f"schema={len(_merged_synth['schema'])} "
                                f"visual={len(_merged_synth['visual'])}"
                            )
                except Exception as _synth_fb_exc:  # noqa: BLE001 — fail-safe
                    self.log.warning(f"strategy synthesis (feedback-loop) skipped: {_synth_fb_exc}")

            if not all_routable:
                if attempt > 0:
                    self.log.info(
                        f"feedback loop resolved all issues after {attempt} retry/retries"
                    )
                break

            self.log.info(
                f"feedback loop attempt {attempt + 1}/{self.MAX_FIX_RETRIES}: "
                f"{len(all_routable)} routable issue(s) to fix"
            )

            # group by responsible agent
            by_agent: dict[str, list[dict]] = {}
            for i in all_routable:
                by_agent.setdefault(i["agent_responsible"], []).append(i)

            # F1: put failure context in session_state so the re-run agent sees it
            ctx.session_state["fix_context"] = {
                "attempt": attempt + 1,
                "issues": all_routable,
                "previous_errors": [i.get("message", "") for i in routable],
            }
            ctx.extra["fix_context"] = ctx.session_state["fix_context"]

            # re-run each responsible agent (F6: targeted, only responsible agent)
            fixed_any = False
            for agent_name, agent_issues in by_agent.items():
                agent_cls = self._fix_agent_cls(agent_name)
                if agent_cls is None:
                    self.log.warning(
                        f"feedback loop: no fix agent for '{agent_name}' — skipping"
                    )
                    continue
                self.log.info(
                    f"feedback loop: re-running {agent_name} for {len(agent_issues)} issue(s)"
                )
                rresult = agent_cls(ctx).run()
                report.add(rresult)
                if rresult.ok:
                    fixed_any = True

            # re-validate to see if the fixes worked
            vresult = ValidatorAgent(ctx).run()
            report.add(vresult)
            report.validation = vresult.data

            # F3: track quality score (fewer issues = higher score)
            current_issues = len(vresult.data.get("issues", []) or [])
            quality_score = max(0, 100 - current_issues * 10)
            quality_history.append(quality_score)
            self.log.info(
                f"feedback loop: quality score after attempt {attempt + 1}: "
                f"{quality_score}/100 (history: {quality_history})"
            )

            # Optimization loop — adjust scoring weights based on issue types
            # so the next candidate-selection round uses updated priorities.
            # Each issue type shifts the most relevant dimension's weight up by
            # 0.05 (capped at 0.60) to penalise the failure mode that produced it.
            # Weights are renormalised to sum=1.0 after adjustment so the score
            # formula remains a proper weighted average.
            weight_delta = 0.05
            issue_contexts = [
                i.get("context", {})
                for i in all_routable
                if isinstance(i, dict)
            ]
            issue_types = {c.get("issue_type", "") for c in issue_contexts}
            weights_before = dict(weights)

            if "ghost_reference" in issue_types:
                weights["kpi_alignment"] = min(0.60, weights["kpi_alignment"] + weight_delta)
            if "empty_page" in issue_types:
                weights["data_coverage"] = min(0.60, weights["data_coverage"] + weight_delta)
            if "layout_overlap" in issue_types:
                weights["visual_quality"] = min(0.60, weights["visual_quality"] + weight_delta)
            if "missing_measure" in issue_types:
                weights["business_value"] = min(0.60, weights["business_value"] + weight_delta)

            # Consistency penalty signals (Upgrade 5 — CrossAgentConsistencyChecker).
            # Each penalty category maps to the scoring dimension that should be
            # boosted to correct the failure mode in the next candidate pass.
            _cons_res: dict = ctx.extra.get("consistency_result") or {}
            _penalties: list[str] = _cons_res.get("penalties", [])
            if any("kpi_mismatch" in p for p in _penalties):
                weights["kpi_alignment"] = min(0.60, weights["kpi_alignment"] + weight_delta)
            if any("schema_drift" in p for p in _penalties):
                weights["data_coverage"] = min(0.60, weights["data_coverage"] + weight_delta)
            if any("visual_incon" in p for p in _penalties):
                weights["visual_quality"] = min(0.60, weights["visual_quality"] + weight_delta)
            if any("orphan_measure" in p for p in _penalties):
                weights["interpretability"] = min(0.60, weights["interpretability"] + weight_delta)

            # Renormalise: always maintain a proper probability simplex
            total_w = sum(weights.values())
            weights = {k: v / total_w for k, v in weights.items()}
            ctx.extra["scoring_weights"] = weights  # type: ignore[assignment]

            if weights != weights_before:
                log_decision(
                    agent="Orchestrator",
                    decision_type="weight_adjustment",
                    subject=f"feedback_loop_attempt_{attempt + 1}",
                    rationale=(
                        f"Issue types detected: {sorted(issue_types)}. "
                        "Scoring weights adjusted to penalise recurring failure modes."
                    ),
                    confidence=1.0,
                    extra={"weights_before": weights_before, "weights_after": weights},
                )
                self.log.info(
                    f"feedback loop: scoring weights adjusted → "
                    f"bv={weights['business_value']:.3f} "
                    f"ka={weights['kpi_alignment']:.3f} "
                    f"dc={weights['data_coverage']:.3f} "
                    f"vq={weights['visual_quality']:.3f} "
                    f"ip={weights['interpretability']:.3f}"
                )

            # F4: persist feedback to a JSON store for future runs
            try:
                import json as _json
                feedback_path = Path(ctx.pbip_root) / "feedback_history.json"
                feedback_entry = {
                    "attempt": attempt + 1,
                    "issues_count": len(all_routable),
                    "quality_score": quality_score,
                    "agents_rerun": list(by_agent.keys()),
                    "fixed_any": fixed_any,
                    "scoring_weights": weights,
                    "timestamp": utc_now_iso(),
                }
                existing: list = []
                if feedback_path.exists():
                    with open(feedback_path, encoding="utf-8") as f:
                        existing = _json.load(f)
                existing.append(feedback_entry)
                with open(feedback_path, "w", encoding="utf-8") as f:
                    _json.dump(existing, f, indent=2)
            except Exception:
                pass  # persistence is best-effort

            if not fixed_any and attempt == self.MAX_FIX_RETRIES - 1:
                self.log.warning(
                    f"feedback loop exhausted {self.MAX_FIX_RETRIES} retries — "
                    "degrading to error report (fail-safe)"
                )

    # ------------------------------------------------------------------
    # interactive questioning helper
    # ------------------------------------------------------------------

    def _ask_user_questions(self, ctx: AgentContext) -> None:
        """Pose DataAnalyzer questions to the user in interactive CLI mode.

        Reads ``ctx.extra["data_profile"]["questions"]`` and, when interactive,
        prints each question and reads the user's choice via ``input()``.
        Answers are stored in ``ctx.extra["answers"]`` so the DataCleaner can
        act on them. In non-interactive mode the best-effort defaults already
        set by the analyzer are kept.
        """
        profile = ctx.extra.get("data_profile") or {}
        questions = profile.get("questions", [])
        if not questions:
            return
        if not ctx.extra.get("interactive", False):
            # best-effort defaults already populated by DataAnalyzerAgent
            self.log.info(f"non-interactive: {len(questions)} question(s) auto-decided")
            return
        answers = ctx.extra.get("answers", {})
        print("\n" + "=" * 60)
        print("  Data Analyzer needs a few decisions:")
        print("=" * 60)
        for q in questions:
            qid = q["id"]
            prompt = q["question"]
            options = q.get("options", [])
            default = q.get("default", "")
            print(f"\n  {prompt}")
            if options:
                for i, opt in enumerate(options, 1):
                    marker = " (default)" if opt == default else ""
                    print(f"    {i}. {opt}{marker}")
                while True:
                    raw = input(f"  Choose [1-{len(options)}] or type an option: ").strip()
                    if not raw and default:
                        answers[qid] = default
                        break
                    if raw.isdigit() and 1 <= int(raw) <= len(options):
                        answers[qid] = options[int(raw) - 1]
                        break
                    if raw in options:
                        answers[qid] = raw
                        break
                    print("  Invalid choice, try again.")
            else:
                raw = input(f"  Your answer: ").strip()
                answers[qid] = raw or default
            self.log.info(f"user answered {qid} -> {answers[qid]}")
        ctx.extra["answers"] = answers
        print("=" * 60 + "\n")

    # ------------------------------------------------------------------
    # top-level project artefacts
    # ------------------------------------------------------------------

    def _write_item_metadata(self, ctx: AgentContext) -> None:
        """Write all the entry/config files Power BI Desktop needs to open the project.

        All payloads are produced by :mod:`mcp_server.pbir_generator` so they
        match the exact schemas Power BI Desktop validates against (derived
        from real Desktop samples). Files written:

          * ``<Name>.pbip``                      -- root entry (the file you open)
          * ``<Name>.SemanticModel/.platform``   -- SM item type + logicalId
          * ``<Name>.SemanticModel/definition/definition.pbism`` -- SM entry v4.2
          * ``<Name>.Report/.platform``          -- report item type + logicalId
          * ``<Name>.Report/definition.pbir``    -- report entry v4.0 (byPath)
          * ``<Name>.Report/definition/version.json``       -- PBIR 2.0.0
          * ``<Name>.Report/definition/report.json``        -- report root (3.0.0)
          * ``<Name>.Report/definition/pages/pages.json``   -- pageOrder
        """
        from mcp_server import pbir_generator as pb

        sm_dir = paths.sm_root(ctx.pbip_root, ctx.project_name)
        rep_dir = paths.report_root(ctx.pbip_root, ctx.project_name)
        ensure_dir(sm_dir)
        ensure_dir(rep_dir)
        ensure_dir(paths.sm_definition(sm_dir))
        ensure_dir(paths.report_definition(rep_dir))

        sm_folder = f"{ctx.project_name}.SemanticModel"
        rep_folder = f"{ctx.project_name}.Report"

        # --- semantic model item + entry ---
        atomic_write_json(
            paths.sm_platform(sm_dir),
            pb.platform_properties("SemanticModel", ctx.project_name),
        )
        atomic_write_json(
            paths.sm_definition_pbism(sm_dir), pb.definition_pbism()
        )

        # --- report item + entry (definition.pbir at REPORT ROOT) ---
        atomic_write_json(
            paths.report_platform(rep_dir),
            pb.platform_properties("Report", ctx.project_name),
        )
        atomic_write_json(
            paths.report_definition_pbir(rep_dir),
            pb.definition_pbir(f"../{sm_folder}"),
        )

        # --- PBIR format markers ---
        atomic_write_json(
            paths.report_definition_version(rep_dir), pb.version_json()
        )

        # report.json: write a minimal valid one if ReportAgent hasn't already
        rjson = paths.report_json_file(rep_dir)
        if not rjson.exists():
            atomic_write_json(rjson, pb.report_json())

        # page index: definition/pages/pages.json
        # In edit mode: merge existing page ids + new pages (preserve order)
        new_page_ids = [p["id"] for p in ctx.pages]
        if ctx.input_mode == "edit_pbip" and ctx.existing_page_ids:
            merged = list(ctx.existing_page_ids)
            for pid in new_page_ids:
                if pid not in merged:
                    merged.append(pid)
            page_ids = merged
        else:
            page_ids = new_page_ids or ["summary-page"]
        atomic_write_json(
            paths.report_pages_metadata(rep_dir), pb.pages_metadata(page_ids)
        )

        # --- minimal database.tmdl + model.tmdl so the SM is loadable ---
        # In edit mode these already exist in the copied PBIP; skip if present
        self._write_sm_skeleton(ctx, sm_dir)

        # --- the root .pbip entry file (THE file Power BI Desktop opens) ---
        atomic_write_json(
            paths.pbip_entry_file(ctx.pbip_root, ctx.project_name),
            pb.pbip_entry(rep_folder),
        )
        self.log.info(f"wrote root entry file: {ctx.project_name}.pbip")

    def _write_sm_skeleton(self, ctx: AgentContext, sm_dir: Path) -> None:
        """Write the model.tmdl and database.tmdl that frame the tables.

        TMDL rules (Power BI Desktop):
          - database.tmdl: just the declaration line, no lineageTag, no nested model
          - model.tmdl: model Model with culture + dataAccessOptions
        """
        model_tmdl = paths.sm_model_file(sm_dir)
        if not model_tmdl.exists():
            # Reference-verified model.tmdl structure (from real Desktop PBIP exports):
            # - defaultPowerBIDataSourceVersion BEFORE dataAccessOptions
            # - discourageImplicitMeasures as bare keyword (required for V3)
            # - legacyRedirects / returnErrorValuesAsNull without ': true'
            # - queryGroup at FILE ROOT (0-indent), not inside model block
            # - PBI_QueryOrder at FILE ROOT (0-indent) as raw JSON array
            # - annotations inside model block (1-tab) are model properties
            table_name = (ctx.schema or {}).get("table_name", "Table")
            # build table list for PBI_QueryOrder (all tables in schema)
            all_tables = (ctx.schema or {}).get("all_tables", [])
            query_names = (
                [t["table_name"] for t in all_tables]
                if all_tables else [table_name]
            )
            # include Date table if we're going to generate it
            if (ctx.schema and ctx.input_mode == "create"):
                should_date, _, _ = needs_date_table(ctx.schema)
                if should_date and "Date" not in query_names:
                    query_names = query_names + ["Date"]
            query_order = "[" + ", ".join(f'"{n}"' for n in query_names) + "]"

            model_txt = (
                "model Model\n"
                "\tculture: en-US\n"
                "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
                "\tdiscourageImplicitMeasures\n"
                "\tsourceQueryCulture: en-US\n"
                "\tdataAccessOptions\n"
                "\t\tlegacyRedirects\n"
                "\t\treturnErrorValuesAsNull\n"
                "\n"
                # queryGroup at root level (0-indent) — required for Power Query editor
                "queryGroup Tables\n"
                "\tannotation PBI_QueryGroupOrder = 0\n"
                "\n"
                # file-root annotations (0-indent)
                "annotation __PBI_TimeIntelligenceEnabled = 0\n"
                f"annotation PBI_QueryOrder = {query_order}\n"
            )
            atomic_write_text(model_tmdl, model_txt)

        db_tmdl = paths.sm_database_file(sm_dir)
        if not db_tmdl.exists():
            # compatibilityLevel 1702 = most recent, required for full V3 metadata
            db_txt = (
                f"database {ctx.project_name}\n"
                "\tcompatibilityLevel: 1702\n"
                "\tcompatibilityMode: powerBI\n"
            )
            atomic_write_text(db_tmdl, db_txt)

        # Auto-generate a Date dimension table when the schema has a date column
        # (only in create mode -- in edit_pbip mode the source already has its own dates)
        if ctx.schema and ctx.input_mode == "create":
            should_create, fact_tbl, date_col = needs_date_table(ctx.schema)
            if should_create:
                date_tmdl_path = paths.sm_tables_dir(sm_dir) / "Date.tmdl"
                if not date_tmdl_path.exists():
                    date_tmdl = build_date_table_tmdl(fact_tbl, date_col)
                    atomic_write_text(date_tmdl_path, date_tmdl)
                    self.log.info(
                        f"auto-generated Date table (fact={fact_tbl}, col={date_col})"
                    )

    def _write_readme(self, ctx: AgentContext, report: RunReport) -> None:
        """Write a README.md at the root of the .pbip folder summarising the run."""
        schema = ctx.schema or {}
        columns = schema.get("columns", [])
        measures = ctx.measures or []
        pages = ctx.pages or []

        lines: list[str] = []
        lines.append(f"# {ctx.project_name}\n")
        lines.append(f"_Generated by PowerBI Builder on {report.finished_at or utc_now_iso()}._\n")
        lines.append(f"**Business description:** {ctx.business_description}\n")
        lines.append(f"**Source file:** `{Path(ctx.source_path).name}`\n")

        lines.append("## Data model\n")
        lines.append(f"- Table: **{schema.get('table_name', 'n/a')}**")
        lines.append(f"- Columns: {len(columns)}")
        if columns:
            lines.append("\n| Column | Data type | Summarize by |")
            lines.append("|---|---|---|")
            for c in columns:
                lines.append(f"| {c['name']} | {c['dataType']} | {c.get('summarizeBy','none')} |")
        lines.append("")

        lines.append("## DAX measures\n")
        if measures:
            lines.append("| Measure | Folder | Format |")
            lines.append("|---|---|---|")
            for m in measures:
                lines.append(f"| {m['name']} | {m['displayFolder']} | `{m['formatString']}` |")
        else:
            lines.append("_No measures generated._")
        lines.append("")

        lines.append("## Report\n")
        if pages:
            for p in pages:
                lines.append(f"- Page **{p.get('displayName')}** "
                             f"({len(p.get('visuals', []))} visuals)")
        else:
            lines.append("_No report generated._")
        lines.append("")

        if report.validation:
            v = report.validation
            lines.append("## Validation\n")
            lines.append(f"- Status: {'OK' if v.get('ok') else 'ISSUES FOUND'}")
            lines.append(f"- Tables: {v.get('tables',0)} | "
                         f"Measures: {v.get('measures',0)} | "
                         f"Pages: {v.get('pages',0)} | "
                         f"Visuals: {v.get('visuals',0)}")
            if v.get("fixes_applied"):
                lines.append("\nAuto-fixes applied:")
                for f in v["fixes_applied"]:
                    lines.append(f"- {f}")
            if v.get("warnings"):
                lines.append("\nWarnings:")
                for w in v["warnings"]:
                    lines.append(f"- {w}")
            if v.get("errors"):
                lines.append("\nErrors:")
                for e in v["errors"]:
                    lines.append(f"- {e}")
            lines.append("")

        lines.extend(self._render_insights_section(ctx.extra.get("insights")))

        lines.append("## How to open\n")
        lines.append("Open the `.SemanticModel` and `.Report` folders in "
                     "**Power BI Desktop** (File > Open report > Browse), or "
                     "open the `.Report` folder directly.\n")

        atomic_write_text(Path(ctx.pbip_root) / "README.md", "\n".join(lines))

    def _render_insights_section(self, insights: Any | None) -> list[str]:
        """Render the ``## Business Insights`` README section (fail-safe).

        Every subsection is skipped (not printed) when its list is empty —
        this agent never fabricates content, it only surfaces what
        ``InsightsAgent`` / ``utils.insights_engine`` actually found.
        """
        try:
            if insights is None:
                return []
            trends = getattr(insights, "trends", []) or []
            segments = getattr(insights, "segments", []) or []
            underperformers = getattr(insights, "underperformers", []) or []
            anomalies = getattr(insights, "anomalies", []) or []
            visual_explanations = getattr(insights, "visual_explanations", []) or []
            kpi_gaps = getattr(insights, "kpi_gap_suggestions", []) or []

            if not any([trends, segments, underperformers, anomalies,
                        visual_explanations, kpi_gaps]):
                return []

            lines: list[str] = ["## Business Insights\n"]

            if trends:
                lines.append("### Key Trends\n")
                for t in trends:
                    lines.append(f"- {t.narrative}")
                lines.append("")

            if segments:
                lines.append("### Segment Performance\n")
                lines.append(
                    "_Aggregate performance segmentation (group-by + quantile "
                    "tiering on the data) — not per-customer ML clustering; no "
                    "clustering library is a project dependency._\n"
                )
                lines.append("| Segment | Dimension | Metric | Value | Share | Tier |")
                lines.append("|---|---|---|---|---|---|")
                for s in segments:
                    lines.append(
                        f"| {s.segment_name} | {s.dimension} | {s.primary_metric} | "
                        f"{s.metric_value:,.2f} | {s.share_pct}% | {s.tier} |"
                    )
                lines.append("")

            if underperformers:
                lines.append("### Underperforming Categories & Recommended Actions\n")
                for u in underperformers:
                    lines.append(f"- **{u.segment_name}** ({u.gap_vs_avg_pct:+.1f}% vs average): "
                                 f"{u.recommended_action}")
                lines.append("")

            if anomalies:
                lines.append("### Data Anomalies\n")
                for a in anomalies:
                    lines.append(f"- {a.narrative}")
                lines.append("")

            if visual_explanations:
                lines.append("### Visual Explanations\n")
                lines.append("| Page | Visual | Explanation |")
                lines.append("|---|---|---|")
                for v in visual_explanations:
                    lines.append(f"| {v.page} | {v.visual_name} | {v.explanation} |")
                lines.append("")

            if kpi_gaps:
                lines.append("### Suggested Additional KPIs\n")
                for k in kpi_gaps:
                    lines.append(f"- {k.suggestion} _(reason: {k.reason})_")
                lines.append("")

            return lines
        except Exception as exc:  # noqa: BLE001 — README write must never fail on this
            self.log.warning(f"insights README section skipped: {exc}")
            return []

    def _write_spec(self, ctx: AgentContext, report: RunReport) -> None:
        """Write a versioned ``build.spec.json`` at the PBIP root.

        This is the *enduring, versioned asset* of spec-driven development:
        the generated TMDL/PBIR files are disposable (regenerable), but this
        specification captures *what was built and why* so the build can be
        audited or reproduced later without re-running it. It records the
        source, inferred schema, measures, pages, relationships, validation
        outcome, and the agent trajectory (one entry per step).

        Fail-safe: any error while assembling the spec is logged and swallowed
        — a spec-write failure must never fail the run.
        """

        def _json_default(obj: Any) -> Any:
            """``json.dumps`` default hook: coerce non-native scalar types
            (numpy/pandas scalars, dates, sets, bytes) to JSON-safe primitives.
            Called only for objects json.dumps cannot natively handle. We do
            NOT call ``model_dump`` here (the spec is already a dict from
            ``BuildSpec.model_dump``) to avoid any chance of a circular
            reference re-entering the encoder."""
            import numbers
            import datetime as _dt
            if isinstance(obj, bool) or obj is None:
                return obj
            if isinstance(obj, numbers.Integral):
                return int(obj)
            if isinstance(obj, numbers.Real):
                return float(obj)
            if isinstance(obj, _dt.datetime):
                return obj.isoformat()
            if isinstance(obj, _dt.date):
                return obj.isoformat()
            if isinstance(obj, (set, frozenset)):
                return list(obj)
            if isinstance(obj, bytes):
                return obj.decode("utf-8", errors="replace")
            # Pydantic models that slipped through: dump to dict (no recursion
            # back into this hook for their contents — json handles the dict).
            if hasattr(obj, "model_dump"):
                try:
                    return obj.model_dump()
                except Exception:
                    return str(obj)
            return str(obj)
        try:
            schema = ctx.schema or {}
            # ``schema["all_tables"]`` is a recursive structure (each entry
            # re-contains an ``all_tables`` key), which json cannot serialise
            # (circular reference). The spec only needs the flat per-table
            # schemas, so strip the nested ``all_tables`` from each entry.
            schema = dict(schema)  # shallow copy — never mutate ctx.schema
            if "all_tables" in schema:
                schema["all_tables"] = [
                    {k: v for k, v in t.items() if k != "all_tables"}
                    for t in schema["all_tables"]
                    if isinstance(t, dict)
                ]
            plan_raw = ctx.extra.get("plan", []) if ctx.extra else []
            # ``plan`` may be a BuildPlan (has .steps), a list of step dicts,
            # or a dict; normalize to a list of plain dicts for the spec.
            if hasattr(plan_raw, "steps"):
                plan_list = [s.model_dump() if hasattr(s, "model_dump") else dict(s) for s in plan_raw.steps]
            elif isinstance(plan_raw, list):
                plan_list = [s.model_dump() if hasattr(s, "model_dump") else dict(s) for s in plan_raw]
            elif isinstance(plan_raw, dict):
                plan_list = [plan_raw]
            else:
                plan_list = []
            spec = BuildSpec(
                project_name=ctx.project_name,
                source={
                    "path": str(ctx.source_path) if ctx.source_path else "",
                    "input_mode": getattr(ctx, "input_mode", "create"),
                },
                data_schema=schema,
                measures=ctx.measures or [],
                pages=ctx.pages or [],
                relationships=ctx.extra.get("relationships", []) if ctx.extra else [],
                validation=report.validation or {},
                plan=plan_list,
                insights=ctx.extra.get("insights_dict", {}) if ctx.extra else {},
                trajectory=[
                    {
                        "agent": s.get("agent", "?"),
                        "ok": s.get("ok", False),
                        "message": s.get("message", ""),
                        "errors": s.get("errors", []),
                    }
                    for s in report.steps
                ],
                business_description=ctx.business_description,
                started_at=report.started_at,
                finished_at=report.finished_at or utc_now_iso(),
                ok=bool(report.ok),
            )
            # by_alias=True so the JSON key is "schema" (not "data_schema").
            # Serialize with a default hook so numpy/pandas scalars in the
            # inferred schema serialize cleanly, then write atomically via the
            # module-level atomic_write_text (so tests can patch it).
            import json as _json
            spec_text = _json.dumps(
                spec.model_dump(by_alias=True),
                indent=2,
                ensure_ascii=False,
                sort_keys=False,
                default=_json_default,
            )
            atomic_write_text(Path(ctx.pbip_root) / "build.spec.json", spec_text)
        except Exception as exc:  # fail-safe: never break the run over the spec
            self.log.warning("build.spec.json write failed: %s", exc)

    def _write_decisions_log(self, ctx: AgentContext) -> None:
        """Write decisions.log.json with all explainability decisions (fail-safe)."""
        try:
            import json as _json
            from utils.explainability import get_tracker
            tracker = get_tracker()
            if tracker is None:
                return
            decisions = tracker.as_dicts()
            if not decisions:
                return
            log_data = {
                "project_name": ctx.project_name,
                "decision_count": len(decisions),
                "decisions": decisions,
            }
            atomic_write_text(
                Path(ctx.pbip_root) / "decisions.log.json",
                _json.dumps(log_data, indent=2, ensure_ascii=False),
            )
        except Exception as exc:  # fail-safe: never break the run over the log
            self.log.warning("decisions.log.json write failed: %s", exc)

    # ------------------------------------------------------------------
    # edit-mode helpers
    # ------------------------------------------------------------------

    def _copy_existing_pbip(self, src: Path, dst: Path) -> None:
        """Non-destructive copy of an existing PBIP folder into the output dir.

        Uses shutil.copytree with dirs_exist_ok=True so:
        - existing files in dst are overwritten by src (full sync)
        - files only in dst are kept (nothing is deleted)

        This gives us a clean working copy to apply edits on top of.
        """
        self.log.info(f"copying existing PBIP from {src} to {dst}")
        shutil.copytree(str(src), str(dst), dirs_exist_ok=True)

    def _clear_stale_project(self, run_dir: Path, project_name: str) -> None:
        """Remove stale .SemanticModel / .Report folders from a previous run.

        In create / edit_excel mode the project is rebuilt from scratch, so any
        leftover Report pages (especially orphan page folders not listed in
        pages.json from a prior `adk web` session) must be cleared — otherwise
        they accumulate on disk and the validator over-reports the page count
        while Desktop (which reads pages.json pageOrder) shows fewer pages.

        Only the PBIP item folders are removed; unrelated files (e.g. a copied
        source CSV) are left intact. Errors are logged but never fatal.
        """
        for suffix in (".SemanticModel", ".Report"):
            stale = run_dir / f"{project_name}{suffix}"
            if stale.exists():
                try:
                    shutil.rmtree(str(stale))
                    self.log.info(f"cleared stale {stale.name} before fresh run")
                except Exception as exc:  # noqa: BLE001 - best-effort
                    self.log.warning(f"could not clear {stale}: {exc}")


# ---------------------------------------------------------------------------
# small helpers (kept local so the orchestrator file is self-contained)
# ---------------------------------------------------------------------------

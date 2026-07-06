"""DataCleanerAgent -- applies cleaning steps based on the data profile.

Role
----
Runs AFTER DataAnalyzerAgent and BEFORE SchemaAgent. It reads
``ctx.extra["data_profile"]`` and ``ctx.extra["answers"]``, builds a cleaning
plan, applies it to the raw data file (non-destructively — a cleaned copy is
written), and redirects ``ctx.source_path`` to the cleaned file so downstream
agents build on clean data.

Self-verification: re-profiles the cleaned file and compares before/after to
confirm the quality score improved. If it didn't, a warning is logged (the
cleaned file is still used — the user can inspect the cleaning report).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agents.base import AgentResult, BaseAgent
from agents.schemas import CleaningStrategyDecision
from utils import AuditLogger

_log = AuditLogger.get("agent.data_cleaner")

# ---------------------------------------------------------------------------
# Optional LLM-assisted semantic decision layer (advisor-only).
#
# The LLM never touches raw data or performs cleaning itself -- it only
# proposes a null_handling_strategy for a column the rule-based heuristic
# is genuinely unsure about (a numeric column with an unrecognized name
# that needs imputation). Its decision is injected into the SAME
# `answers` dict `plan_cleaning()` already accepts from interactive user
# Q&A, so the deterministic executor (plan_cleaning/apply_cleaning) is
# unchanged. Gated by config.DEFAULT_SETTINGS.semantic_llm_assist_enabled
# (off by default) -- when off, none of this code path is even imported.
# ---------------------------------------------------------------------------

_IDENTIFIER_ROLE_TOKENS = frozenset({
    "id", "code", "key", "zip", "zipcode", "postal", "ssn", "phone",
    "account", "guid", "uuid",
})
_DATE_ROLE_HINTS = ("date", "day", "month", "year", "time", "timestamp")
_MEASURE_ROLE_HINTS = (
    "amount", "sales", "revenue", "price", "cost", "profit", "qty",
    "quantity", "total", "sum", "count", "value", "score", "rate",
)

_ALLOWED_SEMANTIC_ROLES = frozenset({
    "identifier", "categorical", "numeric_measure", "numeric_code",
    "date", "free_text", "ambiguous",
})
_ALLOWED_NULL_STRATEGIES = frozenset({
    "drop_column", "fill_mean", "fill_mode", "fill_median",
    "fill_sentinel", "leave_as_is",
})
_STRATEGY_TO_ANSWER = {
    "drop_column": "drop",
    "fill_mean": "impute_mean",
    "fill_mode": "impute_mode",
    "fill_median": "impute_median",
    "fill_sentinel": "impute_constant",
    "leave_as_is": "leave_as_is",
}


def _heuristic_role_confidence(name: str, data_type: str) -> float:
    """Deterministic, keyword-based confidence that the rule-based
    cleaner's default numeric-imputation choice (median, blindly, for any
    numeric dtype) is actually appropriate for this column.

    This NEVER decides the cleaning strategy itself -- it only decides
    whether the situation is ambiguous enough to escalate to the optional
    LLM advisor. Returns 1.0 (never escalate) on any internal error.
    """
    try:
        if data_type not in {"int64", "double", "decimal"}:
            return 1.0  # non-numeric columns aren't in the ambiguous-imputation path at all
        lname = name.lower().replace("-", "_")
        tokens = set(re.split(r"[^a-z0-9]+", lname))
        if tokens & _IDENTIFIER_ROLE_TOKENS:
            return 0.9  # confidently an identifier/code
        if any(h in lname for h in _DATE_ROLE_HINTS):
            return 0.9  # confidently a date-like numeric (epoch, year, ...)
        if any(h in lname for h in _MEASURE_ROLE_HINTS):
            return 0.85  # confidently a real measure -- mean/median imputation is right
        return 0.4  # numeric, generic/unrecognized name -- genuinely ambiguous
    except Exception:  # noqa: BLE001
        return 1.0


def _get_cleaning_strategy_llm(
    column_name: str,
    column_profile: dict[str, Any],
    data_type: str,
    business_description: str,
) -> CleaningStrategyDecision | None:
    """Ask the LLM to classify one ambiguous column and recommend a null-
    handling strategy. Returns ``None`` on ANY failure (flag off, no key,
    call failure/timeout, malformed response, or confidence below the
    configured threshold) so the caller falls back to the existing
    rule-based default -- exactly the same contract as every other
    optional-LLM agent in this codebase.
    """
    try:
        from config import DEFAULT_SETTINGS
        if not DEFAULT_SETTINGS.semantic_llm_assist_enabled:
            return None
        from utils.model_config import MissingAPIKeyError, get_llm_config
        from utils.retry import retry_sync
    except Exception:
        return None

    try:
        llm_config = get_llm_config()
    except MissingAPIKeyError as exc:
        _log.error(f"LLM provider misconfigured, skipping cleaning-strategy assist: {exc}")
        return None
    if llm_config is None:
        return None

    sample_values = column_profile.get("distinct_values") or []
    prompt = (
        "You are a data cleaning advisor. Given ONLY the statistics below "
        "for one column (never the raw dataset), classify its semantic "
        "role and recommend how to handle its missing values.\n\n"
        "Output ONLY valid JSON matching this schema (no prose, no code fences):\n"
        "{\n"
        '  "semantic_role": "identifier|categorical|numeric_measure|numeric_code|date|free_text|ambiguous",\n'
        '  "null_handling_strategy": "drop_column|fill_mean|fill_mode|fill_median|fill_sentinel|leave_as_is",\n'
        '  "confidence": 0.0,\n'
        '  "reasoning": "string"\n'
        "}\n\n"
        "Rules:\n"
        "- numeric_code means a number that IDENTIFIES something (e.g. a "
        "zip/postal code, an account number) and must NEVER be averaged -- "
        "prefer fill_mode or leave_as_is for it, never fill_mean.\n"
        "- numeric_measure means a real quantity (amount, price, count, "
        "score) where fill_mean/fill_median is appropriate.\n"
        "- confidence: your confidence in this classification (0.0-1.0).\n\n"
        f"BUSINESS_CONTEXT: {business_description}\n"
        f"COLUMN_NAME: {column_name}\n"
        f"APPARENT_DATA_TYPE: {data_type}\n"
        f"NULL_PCT: {column_profile.get('null_pct', 0)}\n"
        f"DISTINCT_COUNT: {column_profile.get('distinct_count', 0)}\n"
        f"UNIQUE_PCT: {column_profile.get('unique_pct', 0)}\n"
        f"SAMPLE_DISTINCT_VALUES: {json.dumps(sample_values[:15])}\n"
    )

    def _call_once() -> str:
        from utils.model_config import get_text_completion
        return get_text_completion(prompt, llm_config, timeout=12)

    try:
        text = retry_sync(_call_once, retries=1, base_delay=0.5, max_delay=2.0)
    except Exception as exc:
        _log.warning(f"cleaning-strategy LLM call failed, falling back to deterministic: {exc}")
        return None

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        _log.warning("cleaning-strategy LLM response had no JSON object, falling back to deterministic")
        return None

    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        _log.warning(f"cleaning-strategy LLM response failed JSON parsing: {exc}")
        return None

    role = str(raw.get("semantic_role", "ambiguous"))
    if role not in _ALLOWED_SEMANTIC_ROLES:
        role = "ambiguous"
    strategy = str(raw.get("null_handling_strategy", "leave_as_is"))
    if strategy not in _ALLOWED_NULL_STRATEGIES:
        strategy = "leave_as_is"

    try:
        result = CleaningStrategyDecision(
            column=column_name,
            semantic_role=role,
            null_handling_strategy=strategy,
            confidence=float(raw.get("confidence", 0.0)),
            reasoning=str(raw.get("reasoning", "")),
            source="llm",
        )
    except Exception as exc:
        _log.warning(f"cleaning-strategy LLM response failed schema validation: {exc}")
        return None

    if result.confidence < DEFAULT_SETTINGS.semantic_llm_confidence_threshold:
        _log.info(
            f"cleaning-strategy LLM confidence {result.confidence} below threshold "
            f"{DEFAULT_SETTINGS.semantic_llm_confidence_threshold} for column "
            f"'{column_name}' -- discarding"
        )
        return None

    return result


class DataCleanerAgent(BaseAgent):
    """Cleans raw data based on the analyzer's profile + user answers."""

    name = "DataCleanerAgent"
    description = (
        "You are the DataCleanerAgent. Given a data quality profile and the "
        "user's answers, build a cleaning plan (drop null-heavy columns, "
        "impute missing values, remove duplicates, cap outliers) and apply it "
        "to the data file. Write a cleaned copy — never modify the original. "
        "Self-verify that the cleaning improved the quality score. Redirect "
        "downstream agents to use the cleaned file."
    )

    def _run(self) -> AgentResult:
        ctx = self.context
        profile = ctx.extra.get("data_profile")

        # Skip in edit modes (no raw data to clean) or when no profile exists
        if ctx.input_mode in ("edit_pbip", "edit_pbix") or not profile:
            return AgentResult(
                agent=self.name, ok=True,
                message="No raw data to clean — skipping (edit mode or no profile).",
                data={"skipped": True},
            )

        source = ctx.source_path
        if not source or not source.is_file():
            return AgentResult(
                agent=self.name, ok=True,
                message="No data file to clean — skipping.",
                data={"skipped": True},
            )

        from adk.tools.data_cleaning_tools import plan_cleaning, apply_cleaning, verify_cleaning

        # 1) build cleaning plan from profile + answers
        answers = dict(ctx.extra.get("answers", {}))  # copy — don't mutate the caller's dict
        answers.update(self._llm_assisted_answers(profile, answers))
        plan_result = plan_cleaning(profile, answers=answers)
        if not plan_result.get("ok"):
            return AgentResult(
                agent=self.name, ok=False,
                message=f"Cleaning plan failed: {plan_result.get('errors', [])}",
                errors=plan_result.get("errors", []),
            )
        plan = plan_result["plan"]
        if not plan:
            self.log.info("no cleaning steps needed — data is already clean")
            ctx.extra["cleaning_report"] = {"steps": [], "improved": False, "skipped": True}
            return AgentResult(
                agent=self.name, ok=True,
                message="Data is already clean — no steps needed.",
                data={"step_count": 0, "skipped": True},
            )

        # 2) apply cleaning (writes a cleaned copy under _clean/)
        output_root = str(ctx.pbip_root.parent)  # output root, not the project dir
        clean_result = apply_cleaning(str(source), plan, output_root=output_root)
        if not clean_result.get("ok"):
            return AgentResult(
                agent=self.name, ok=False,
                message=f"Cleaning failed: {clean_result.get('errors', [])}",
                errors=clean_result.get("errors", []),
            )

        # 3) self-verify: compare before/after
        from mcp_server.schema_inference import profile_data_file
        before_profile = profile_data_file(str(source))
        after_profile = profile_data_file(clean_result["cleaned_path"])
        verify = verify_cleaning(before_profile, after_profile)

        # 4) redirect downstream to the cleaned file
        cleaned_path = Path(clean_result["cleaned_path"])
        ctx.source_path = cleaned_path
        ctx.extra["cleaned_source"] = str(cleaned_path)
        ctx.extra["cleaning_report"] = {
            "steps": plan,
            "actions_applied": clean_result.get("actions_applied", []),
            "before_score": clean_result.get("before_score"),
            "after_score": clean_result.get("after_score"),
            "improved": verify.get("improved", False),
            "deltas": verify.get("deltas", {}),
            "cleaned_path": str(cleaned_path),
        }

        self.log.info(
            f"cleaning: {len(plan)} steps, score {clean_result.get('before_score')}→"
            f"{clean_result.get('after_score')}, improved={verify.get('improved')}"
        )

        return AgentResult(
            agent=self.name,
            ok=True,
            message=(
                f"Cleaned data: {len(plan)} step(s), quality "
                f"{clean_result.get('before_score')}→{clean_result.get('after_score')}"
                + (" (improved)" if verify.get("improved") else " (no improvement — review)")
            ),
            data={
                "step_count": len(plan),
                "before_score": clean_result.get("before_score"),
                "after_score": clean_result.get("after_score"),
                "improved": verify.get("improved", False),
                "cleaned_path": str(cleaned_path),
            },
        )

    def _llm_assisted_answers(
        self, profile: dict[str, Any], existing_answers: dict[str, str],
    ) -> dict[str, str]:
        """Escalate genuinely ambiguous numeric columns to the optional LLM
        advisor, and translate any usable decision into the SAME
        ``nulls_<column>`` answer-key convention ``plan_cleaning()`` already
        understands from interactive user Q&A. No-op (empty dict, zero LLM
        calls) unless ``ENABLE_SEMANTIC_LLM_ASSIST`` is on.

        Records every column that reaches the null-handling decision point
        in ``ctx.extra["cleaning_strategy_decisions"]`` — including ones
        the heuristic was confident enough to resolve WITHOUT ever calling
        the LLM (``source="deterministic_confident"``, ``escalated=False``)
        — so it's possible to tell after the fact *why* a given column
        never reached the LLM, not just which ones did. Purely for
        observability/testing — never read by ``plan_cleaning`` itself.
        """
        ctx = self.context
        overrides: dict[str, str] = {}
        decisions: list[dict[str, Any]] = []
        try:
            from config import DEFAULT_SETTINGS
            if not DEFAULT_SETTINGS.semantic_llm_assist_enabled:
                return overrides

            cols_by_name = (profile.get("quality") or {}).get("columns") or {}
            schema_cols = (profile.get("schema") or {}).get("columns", [])
            for col in schema_cols:
                cname = col.get("name")
                dtype = col.get("dataType", "string")
                if not cname or dtype not in {"int64", "double", "decimal"}:
                    continue
                cp = cols_by_name.get(cname, {})
                null_pct = cp.get("null_pct", 0)
                if not (0 < null_pct <= 60):
                    continue  # not in the auto-impute decision range at all
                if existing_answers.get(f"nulls_{cname}"):
                    continue  # user (or an earlier stage) already answered this

                confidence = _heuristic_role_confidence(cname, dtype)
                if confidence >= DEFAULT_SETTINGS.semantic_llm_confidence_threshold:
                    # Heuristic is confident enough on its own — no LLM call
                    # made. Recorded (escalated=False) so it's possible to
                    # tell, after the fact, WHY a given column never reached
                    # the LLM (as opposed to it being silently skipped).
                    decisions.append({
                        "column": cname, "source": "deterministic_confident",
                        "escalated": False, "semantic_role": None,
                        "confidence": confidence,
                    })
                    continue

                decision = _get_cleaning_strategy_llm(
                    cname, cp, dtype, ctx.business_description or "",
                )
                if decision is not None:
                    overrides[f"nulls_{cname}"] = _STRATEGY_TO_ANSWER.get(
                        decision.null_handling_strategy, "leave_as_is",
                    )
                    decisions.append({
                        "column": cname, "source": "llm",
                        "escalated": True, "semantic_role": decision.semantic_role,
                        "confidence": decision.confidence,
                    })
                else:
                    decisions.append({
                        "column": cname, "source": "deterministic_fallback",
                        "escalated": True, "semantic_role": "ambiguous",
                        "confidence": confidence,
                    })
        except Exception as exc:  # noqa: BLE001 — must never block cleaning
            _log.warning(f"semantic cleaning-strategy assist skipped: {exc}")
            return {}

        if decisions:
            ctx.extra["cleaning_strategy_decisions"] = decisions
        return overrides

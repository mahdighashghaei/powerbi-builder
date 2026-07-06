"""MeasureSelectorAgent -- intent-aware DAX measure selection (Phase 3).

Role
----
Sits between the deterministic candidate generator (``DAXAgent._build_measures``
+ the pattern library) and the final measure write. It takes the candidate
pool + the user's business description + schema, and decides which measures are
actually relevant to what the user asked for — optionally dropping irrelevant
ones or authoring a custom measure the pattern library does not cover.

Design: heuristic candidate generator + LLM selector/ranker
-----------------------------------------------------------
The deterministic ``DAXAgent`` remains the **candidate generator**: it produces
a strong, schema-validated pool of measures. This agent is the **selector**:
when an LLM is available it ranks/prunes/extends that pool based on the user's
intent; when no LLM is available (or validation fails) it returns the pool
unchanged — so the offline baseline stays byte-identical (fail-safe).

Every selected measure carries a ``rationale`` (why it was kept/added) so the
Phase 4 feedback loop can understand the decision when a measure fails
validation.

This agent does NOT write files — it returns a ``MeasureSet``. The caller
(``DAXAgent``) is responsible for persisting via ``write_tmdl_measures`` so the
template-based write layer stays the single source of TMDL emission.
"""
from __future__ import annotations

import json
from typing import Any

from agents.schemas import Measure, MeasureSet
from utils import AuditLogger

_log = AuditLogger.get("agent.measure_selector")


def _select_with_llm(
    candidates: list[dict[str, Any]],
    description: str,
    schema: dict[str, Any],
) -> MeasureSet | None:
    """Ask the LLM to select/rank/extend the candidate measures.

    Returns ``None`` when no API key is set or the call/validation fails so the
    caller falls back to the full candidate pool (fail-safe).
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

    table = schema.get("table_name", "Table")
    cols = [{"name": c["name"], "dataType": c["dataType"]} for c in schema.get("columns", [])]

    cand_brief = [{"name": m["name"], "folder": m.get("displayFolder", ""),
                   "expression": m.get("expression", "")} for m in candidates]

    prompt = (
        "You are a DAX measure selector for a Power BI model. Given the user's "
        "business description, the table schema, and a pool of candidate measures, "
        "decide which measures to KEEP (relevant to the intent) and optionally add "
        "ONE custom measure the pool is missing. Output ONLY JSON matching:\n"
        "{\n"
        '  "measures": [\n'
        '    {"name": "string", "expression": "string", "table": "string", '
        '"displayFolder": "string", "description": "string", "formatString": "string", '
        '"rationale": "why kept/added"}\n'
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Keep 5-10 measures total. Prefer measures directly relevant to the "
        "user's stated goal.\n"
        "- Every expression MUST only reference columns that exist in the schema. "
        "Never invent column names.\n"
        "- Add a custom measure only if the intent clearly needs one the pool lacks.\n"
        "- Fill 'rationale' for every measure (one short sentence).\n\n"
        f"BUSINESS_DESCRIPTION:\n{description}\n\n"
        f"TABLE: {table}\nCOLUMNS:\n{json.dumps(cols)}\n\n"
        f"CANDIDATE_POOL:\n{json.dumps(cand_brief)}\n"
    )

    def _call_once() -> str:
        from utils.model_config import get_text_completion
        return get_text_completion(prompt, llm_config)

    try:
        text = retry_sync(_call_once, retries=2, base_delay=1.0, max_delay=8.0)
    except Exception:
        return None

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None

    # validate against MeasureSet schema
    try:
        ms = MeasureSet(
            measures=[Measure(**m) for m in raw.get("measures", [])]
        )
    except Exception:
        return None

    # safety: must produce at least 3 measures and reference only real columns
    if ms.count < 3:
        return None
    col_names = {c["name"] for c in schema.get("columns", [])}
    import re
    for m in ms.measures:
        for col_ref in re.findall(r"\[([A-Za-z_][\w .]*)\]", m.expression):
            # allow measure self-refs ([Measure Name]) — only flag column refs
            # that look like real column names but aren't in the schema
            if col_ref not in col_names and col_ref not in {mm.name for mm in ms.measures}:
                return None  # references a non-existent column → reject, fall back
    return ms


def _fallback_measureset(
    candidates: list[dict[str, Any]],
) -> MeasureSet:
    """Wrap the candidate pool in a MeasureSet (no LLM, no enrichment).

    Offline baseline: return exactly the candidates the deterministic generator
    produced, with a default rationale so the field is never empty.
    """
    measures = []
    for c in candidates:
        c2 = dict(c)
        c2.setdefault("rationale", "deterministic candidate (no LLM selection)")
        measures.append(Measure(**_coerce_measure(c2)))
    return MeasureSet(measures=measures)


def _coerce_measure(d: dict[str, Any]) -> dict[str, Any]:
    """Ensure a candidate dict has the keys Measure requires (with defaults)."""
    return {
        "name": d["name"],
        "expression": d.get("expression", ""),
        "table": d.get("table", ""),
        "displayFolder": d.get("displayFolder", ""),
        "description": d.get("description", ""),
        "formatString": d.get("formatString", ""),
        "rationale": d.get("rationale", ""),
    }


class MeasureSelectorAgent:
    """Selects/ranks DAX measure candidates based on user intent.

    Not a ``BaseAgent`` (no file writes, no AgentContext) — it is a pure
    selector called by ``DAXAgent`` so the deterministic write path stays intact.
    """

    name = "MeasureSelectorAgent"
    description = (
        "Select which DAX measures are relevant to the user's business intent "
        "from a candidate pool, and optionally author a custom measure. Always "
        "validate expressions reference real schema columns. Fall back to the "
        "full candidate pool when no LLM is available."
    )

    def select(
        self,
        candidates: list[dict[str, Any]],
        description: str,
        schema: dict[str, Any],
    ) -> MeasureSet:
        """Return a ``MeasureSet`` of selected measures with rationale."""
        if not candidates:
            return MeasureSet(measures=[])
        ms = _select_with_llm(candidates, description, schema)
        if ms is None:
            ms = _fallback_measureset(candidates)
        return ms


__all__ = ["MeasureSelectorAgent"]

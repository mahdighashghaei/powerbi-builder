"""Optional LLM helper for refining relationship detection (F7 → Phase 1).

The heuristic in ``agents/relationship_agent.detect_relationships`` works on
name-suffix matching and is fast + offline, but it mis-detects on
non-standard naming and misses semantic FKs (e.g. ``Orders.Cust`` →
``Customers.Id``). When a Google API key is available this module asks Gemini
to confirm/prune the heuristic candidates and suggest any the heuristic missed.

Phase 1 changes
---------------
* The LLM response is now **validated** against the ``RelationshipSet`` Pydantic
  schema before being accepted — a malformed response is dropped and the
  heuristic result is returned instead (fail-safe, not fail-open).
* Transient API errors (429/503/timeout) are retried with exponential backoff
  via :func:`utils.retry.retry_sync` rather than a single try/except.
* The LLM is **always optional**: with no key, ``refine_relationships`` returns
  the heuristic list unchanged. It never raises — a Gemini failure falls back
  to the heuristic result so the pipeline stays deterministic-offline-safe.

A typed variant :func:`refine_relationships_typed` returns a ``RelationshipSet``
(including ``confidence_score`` + ``source_reasoning`` per relationship) for
the Phase 3 flow that needs the richer shape.
"""
from __future__ import annotations

import json
from typing import Any

from agents.schemas import Relationship, RelationshipSet
from utils import AuditLogger
from utils.retry import is_retryable_error, retry_sync

_log = AuditLogger.get("utils.llm_client")


def _call_llm_once(config: Any, prompt: str) -> str:
    """Single LLM call — raises on failure so retry_sync can handle it."""
    from utils.model_config import get_text_completion
    return get_text_completion(prompt, config)


def _build_prompt(
    tables: list[dict[str, Any]], heuristic: list[dict[str, Any]]
) -> str:
    """Build the compact JSON prompt for the relationship-refinement call."""
    table_summary = [
        {"table": t["table_name"], "columns": [c["name"] for c in t.get("columns", [])]}
        for t in tables
    ]
    cand_summary = [
        {"from": f"{r['from_table']}.{r['from_column']}",
         "to": f"{r['to_table']}.{r['to_column']}"}
        for r in heuristic
    ]
    return (
        "You are a data-modelling assistant. Given these tables and a set of "
        "candidate foreign-key relationships detected by a heuristic, return the "
        "FINAL list of correct relationships as JSON. Drop false positives, and "
        "add any obvious FK the heuristic missed. For each relationship include "
        "a confidence_score (0.0-1.0) and a short source_reasoning explaining "
        "why it is (or is not) a real FK. Output ONLY a JSON array of objects "
        "with keys: from_table, from_column, to_table, to_column, "
        "to_cardinality, confidence_score, source_reasoning. No prose.\n\n"
        f"TABLES:\n{json.dumps(table_summary, indent=2)}\n\n"
        f"CANDIDATES:\n{json.dumps(cand_summary, indent=2)}\n"
    )


def _parse_relationships(text: str) -> list[dict[str, Any]]:
    """Extract + validate a JSON array of relationships from the LLM text.

    Returns ``[]`` when the text contains no parseable JSON array. Each item is
    validated to carry the four required keys; items missing keys are dropped.
    """
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for r in raw:
        if all(k in r for k in ("from_table", "from_column", "to_table", "to_column")):
            out.append({
                "from_table": str(r["from_table"]),
                "from_column": str(r["from_column"]),
                "to_table": str(r["to_table"]),
                "to_column": str(r["to_column"]),
                "to_cardinality": str(r.get("to_cardinality", "one")),
                "confidence_score": float(r.get("confidence_score", 1.0)),
                "source_reasoning": str(r.get("source_reasoning", "")),
            })
    return out


def _enrich_heuristic(
    heuristic: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add the Phase 1 fields (confidence_score, source_reasoning) to heuristic
    relationships that lack them, so the output shape is consistent whether or
    not the LLM ran."""
    out = []
    for r in heuristic:
        r2 = dict(r)
        r2.setdefault("confidence_score", 1.0)
        r2.setdefault("source_reasoning", "heuristic name-suffix match")
        out.append(r2)
    return out


def refine_relationships(
    tables: list[dict[str, Any]],
    heuristic: list[dict[str, Any]],
    *,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Use an LLM to confirm/prune/extend heuristic relationship candidates.

    Args:
        tables:    the full table schema list (as built by SchemaAgent).
        heuristic: relationships detected by the name-suffix heuristic.
        model:     override model name; defaults to POWERBI_MODEL/MODEL_NAME.

    Returns:
        A refined list of relationship dicts. When the LLM ran successfully
        each dict is enriched with ``confidence_score`` + ``source_reasoning``.
        When no API key is set or the LLM call fails, ``heuristic`` is returned
        unchanged so the pipeline stays deterministic-offline-safe and the
        Phase 0 baseline snapshots remain byte-identical.
    """
    from utils.model_config import MissingAPIKeyError, get_llm_config

    try:
        llm_config = get_llm_config()
    except MissingAPIKeyError as exc:
        _log.error(f"LLM provider misconfigured, falling back to heuristic: {exc}")
        return heuristic
    if llm_config is None:
        # No provider configured — return the heuristic unchanged so the
        # offline baseline (Phase 0 snapshots) stays byte-identical.
        # Enrichment with confidence_score/source_reasoning only happens
        # when the LLM ran.
        return heuristic

    if model:
        import dataclasses
        prefix = llm_config.litellm_model.split("/", 1)[0]
        llm_config = dataclasses.replace(llm_config, model=model, litellm_model=f"{prefix}/{model}")

    prompt = _build_prompt(tables, heuristic)

    # Retry only transient errors (429/503/timeout). A permanent error
    # (bad key, invalid argument) raises immediately and is caught below.
    try:
        text = retry_sync(
            _call_llm_once, llm_config, prompt,
            retries=3, base_delay=1.0, max_delay=10.0,
        )
    except Exception as exc:  # noqa: BLE001 - never break the pipeline
        if is_retryable_error(exc):
            # exhausted retries on a transient error — fall back gracefully
            pass
        return heuristic

    parsed = _parse_relationships(text)
    if not parsed:
        # LLM returned nothing parseable / valid — fail-safe to heuristic.
        return heuristic

    # Validate against the RelationshipSet schema; if validation fails, fall
    # back. This guarantees downstream code never sees an invalid shape.
    try:
        RelationshipSet(
            relationships=[Relationship(**r) for r in parsed],
            table_count=len(tables),
        )
    except Exception:
        return heuristic

    return parsed


def refine_relationships_typed(
    tables: list[dict[str, Any]],
    heuristic: list[dict[str, Any]],
    *,
    model: str | None = None,
) -> RelationshipSet:
    """Typed variant of :func:`refine_relationships` returning a ``RelationshipSet``.

    Used by the Phase 3 flow that needs the validated, typed shape (with
    ``confidence_score`` + ``source_reasoning`` per relationship).
    """
    rels = refine_relationships(tables, heuristic, model=model)
    return RelationshipSet(
        relationships=[Relationship(**r) for r in rels],
        table_count=len(tables),
    )

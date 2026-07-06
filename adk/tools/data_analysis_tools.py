"""ADK tools for data analysis / profiling (Data Analyzer agent).

Exposes:
  * ``analyze_data``     — profile a CSV/Excel file: schema + quality stats +
                           issues + questions for the user.
  * ``verify_analysis``  — re-read a sample and cross-check the profile
                           (self-verification).
  * ``ask_user``         — pose a question to the user via session state so
                           the agent can clarify ambiguous decisions before
                           proceeding.

These complement the schema tools (``read_csv_schema`` / ``read_pbip_schema``)
by adding a *quality* layer: nulls, outliers, duplicates, single-value columns.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_server.schema_inference import profile_data_file  # noqa: E402


def analyze_data(source: str, sample_rows: int = 1000) -> dict[str, Any]:
    """Analyse a CSV / Excel data file for quality issues.

    Returns a structured profile::

        {
            "ok": True,
            "schema": {...},          # standard schema dict
            "quality": {duplicate_rows, columns: {name: {...}}},
            "quality_score": 0-100,
            "issues": ["...", ...],
            "questions": [{id, question, options, default}, ...],
            "answers": {id: default_choice, ...},
        }

    The ``questions`` list captures ambiguous decisions (e.g. a column with
    50% nulls) that the agent should ask the user about via ``ask_user``.
    ``answers`` provides best-effort defaults for non-interactive runs.
    """
    p = Path(source).expanduser().resolve()
    if not p.is_file():
        return {"ok": False, "errors": [f"File not found: {source}"]}
    try:
        result = profile_data_file(p, sample_rows=sample_rows)
    except Exception as exc:
        return {"ok": False, "errors": [f"Analysis failed: {exc}"]}

    # Build questions for ambiguous issues (same logic as the legacy agent)
    questions: list[dict[str, Any]] = []
    quality = result.get("quality", {})
    cols = quality.get("columns", {})
    schema = result.get("schema", {})
    for col in schema.get("columns", []):
        name = col["name"]
        cp = cols.get(name, {})
        null_pct = cp.get("null_pct", 0)
        if 40 < null_pct <= 60:
            questions.append({
                "id": f"nulls_{name}",
                "question": f"Column '{name}' has {null_pct}% nulls — drop or impute?",
                "options": ["drop", "impute_median", "impute_mean", "impute_mode", "keep"],
                "default": "impute_median",
            })
        if cp.get("distinct_count", 0) <= 1 and null_pct < 100:
            questions.append({
                "id": f"single_{name}",
                "question": f"Column '{name}' has a single value — drop it?",
                "options": ["drop", "keep"],
                "default": "drop",
            })
        if cp.get("outlier_count", 0) > 5 and col["dataType"] in {"int64", "double", "decimal"}:
            questions.append({
                "id": f"outliers_{name}",
                "question": f"Column '{name}' has {cp['outlier_count']} outliers — cap, remove, or keep?",
                "options": ["keep", "cap", "remove"],
                "default": "cap",
            })
    answers = {q["id"]: q["default"] for q in questions}
    return {
        "ok": True,
        "schema": schema,
        "quality": quality,
        "quality_score": result.get("quality_score", 100),
        "issues": result.get("issues", []),
        "questions": questions,
        "answers": answers,
    }


def verify_analysis(profile: dict[str, Any], source: str) -> dict[str, Any]:
    """Self-verify a data profile by re-reading a small sample.

    Compares null percentages between the original profile and a fresh
    200-row sample. Returns ``{ok, verified, mismatches}``.
    """
    p = Path(source).expanduser().resolve()
    if not p.is_file():
        return {"ok": False, "errors": [f"File not found: {source}"]}
    try:
        second = profile_data_file(p, sample_rows=200)
    except Exception as exc:
        return {"ok": False, "errors": [f"Re-read failed: {exc}"]}
    q1 = profile.get("quality", {}).get("columns", {})
    q2 = second.get("quality", {}).get("columns", {})
    mismatches: list[str] = []
    for name, cp1 in q1.items():
        cp2 = q2.get(name, {})
        if abs(cp1.get("null_pct", 0) - cp2.get("null_pct", 0)) > 10:
            mismatches.append(
                f"{name}: null_pct {cp1.get('null_pct')} vs {cp2.get('null_pct')}"
            )
    return {
        "ok": True,
        "verified": len(mismatches) == 0,
        "mismatches": mismatches,
    }


def ask_user(question: str, options: list[str] | None = None,
             tool_context: Any = None) -> dict[str, Any]:
    """Pose a clarifying question to the user.

    In the REPL / ``adk web``, this writes the question into session state
    (``tool_context.state["pending_question"]``) so the conversation loop can
    surface it. The agent should end its turn after calling this; the user's
    next message is the answer. When no ``tool_context`` is available (direct
    in-process calls), the question is returned in the result for the caller
    to handle.
    """
    pending = {"question": question, "options": options or []}
    if tool_context is not None:
        try:
            tool_context.state["pending_question"] = pending
        except Exception:
            pass
    return {
        "ok": True,
        "question": question,
        "options": options or [],
        "note": ("Question recorded in session state — end your turn and wait "
                 "for the user's answer."),
    }


__all__ = ["analyze_data", "verify_analysis", "ask_user"]

"""BPA engine — runs rules and aggregates findings."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .rules import BPA_RULES


def run_bpa(
    model: dict[str, Any],
    rule_ids: list[str] | None = None,
    min_severity: str = "info",
) -> dict[str, Any]:
    """Run BPA rules against a parsed model.

    Args:
        model:        the dict from ``utils.tmdl_parser.read_semantic_model``.
        rule_ids:     optional list of rule_ids to run (default: all).
        min_severity: filter out findings below this level. Order is
                      ``info`` < ``warning`` < ``error``.

    Returns:
        {
            "findings":    [{rule_id, severity, category, summary, target, detail, fix_hint}, ...],
            "by_severity": {"info": int, "warning": int, "error": int},
            "by_category": {"performance": int, "metadata": int, ...},
            "by_rule":     {"PERF_USE_SUMMARIZECOLUMNS": int, ...},
            "rules_ran":   [rule_id, ...],
            "total":       int,
        }
    """
    severity_order = {"info": 0, "warning": 1, "error": 2}
    threshold = severity_order.get(min_severity, 0)

    findings: list[dict[str, Any]] = []
    rules_ran: list[str] = []

    rule_filter = set(rule_ids) if rule_ids else None

    for rid, _cat, fn in BPA_RULES:
        if rule_filter is not None and rid not in rule_filter:
            continue
        rules_ran.append(rid)
        try:
            for f in fn(model):
                if severity_order.get(f["severity"], 0) >= threshold:
                    findings.append(f)
        except Exception as exc:  # noqa: BLE001
            findings.append(
                {
                    "rule_id": rid,
                    "severity": "error",
                    "category": "internal",
                    "summary": f"Rule {rid} raised: {exc.__class__.__name__}",
                    "target": "",
                    "detail": str(exc),
                    "fix_hint": "",
                }
            )

    by_severity = Counter(f["severity"] for f in findings)
    by_category = Counter(f["category"] for f in findings)
    by_rule = Counter(f["rule_id"] for f in findings)

    return {
        "findings": findings,
        "by_severity": dict(by_severity),
        "by_category": dict(by_category),
        "by_rule": dict(by_rule),
        "rules_ran": rules_ran,
        "total": len(findings),
    }

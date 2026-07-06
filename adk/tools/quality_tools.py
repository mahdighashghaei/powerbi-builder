"""ADK tools for Model Quality (Phase 5).

Exposes:
    list_bpa_rules     — enumerate available Best Practice Analyzer rules
    run_bpa_validation — run BPA against a PBIP project and return findings
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.tmdl_parser import read_semantic_model  # noqa: E402
from validators.bpa import list_bpa_rules as _list_rules  # noqa: E402
from validators.bpa import run_bpa as _run_bpa  # noqa: E402


def list_bpa_rules() -> dict:
    """Enumerate available BPA rules.

    Returns:
        {"rules": [{rule_id, category, description}, ...], "count": int}
    """
    rules = _list_rules()
    return {"rules": rules, "count": len(rules)}


def run_bpa_validation(
    pbip_dir: str,
    rule_ids: list[str] | None = None,
    min_severity: str = "info",
) -> dict:
    """Run BPA against a PBIP project's semantic model.

    Args:
        pbip_dir:     path to the PBIP project folder OR directly to the
                      ``*.SemanticModel`` folder. The function autodetects.
        rule_ids:     optional list of rule_ids to run (default: all).
        min_severity: filter findings — ``info`` | ``warning`` | ``error``.

    Returns:
        {
            "findings":    [{rule_id, severity, category, summary, target, detail, fix_hint}],
            "by_severity": {...},
            "by_category": {...},
            "by_rule":     {...},
            "rules_ran":   [...],
            "total":       int,
            "model_path":  str,
        }
    """
    root = Path(pbip_dir)
    if not root.is_dir():
        return {
            "ok": False,
            "errors": [f"path not found or not a directory: {pbip_dir}"],
            "findings": [],
            "total": 0,
        }

    # Locate the SemanticModel folder
    sm_dir: Path | None = None
    if root.name.endswith(".SemanticModel"):
        sm_dir = root
    else:
        for child in root.iterdir():
            if child.is_dir() and child.name.endswith(".SemanticModel"):
                sm_dir = child
                break

    if sm_dir is None:
        return {
            "ok": False,
            "errors": [f"no *.SemanticModel folder under {root}"],
            "findings": [],
            "total": 0,
        }

    model = read_semantic_model(sm_dir)
    result = _run_bpa(model, rule_ids=rule_ids, min_severity=min_severity)
    result["ok"] = True
    result["model_path"] = str(sm_dir)
    return result


__all__ = ["list_bpa_rules", "run_bpa_validation"]

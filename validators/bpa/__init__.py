"""Best Practice Analyzer (BPA) — rule-based quality checks for PBIP models.

Public API:
    list_bpa_rules()  → list[dict]   — enumerate available rules + metadata
    run_bpa(model)    → dict         — run all rules against a parsed model and
                                       return findings grouped by severity

The model is the dict returned by ``utils.tmdl_parser.read_semantic_model``.
"""
from __future__ import annotations

from .engine import run_bpa
from .rules import BPA_RULES, list_bpa_rules

__all__ = ["run_bpa", "list_bpa_rules", "BPA_RULES"]

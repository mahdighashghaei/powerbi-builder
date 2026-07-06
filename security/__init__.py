"""Security teams (red / blue / green) for agent security testing (Wave C1).

This package implements the red/blue/green security-team pattern:
  * ``red_team``  — generates adversarial inputs (attacks).
  * ``blue_team`` — runs them against the real controls and observes outcomes.
  * ``green_team``— produces a remediation report (confirm or harden).
  * ``run_security_drill`` — orchestrates the full red→blue→green cycle.

It reuses the project's real security controls (``utils/security.py``,
``utils/identifiers.py``) so the drill exercises actual defences, not mocks.
"""
from security.blue_team import Observation, observe
from security.green_team import Remediation, RemediationReport, remediate
from security.red_team import AdversarialInput, generate_attacks
from security.run_security_drill import run_security_drill, run_security_drill_summary
from security.trust_score import TrustScore, TrustSignal, compute_trust_score, trust_score_from_drill

__all__ = [
    "AdversarialInput",
    "generate_attacks",
    "Observation",
    "observe",
    "Remediation",
    "RemediationReport",
    "remediate",
    "run_security_drill",
    "run_security_drill_summary",
    "TrustScore",
    "TrustSignal",
    "compute_trust_score",
    "trust_score_from_drill",
]

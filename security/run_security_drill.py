"""Run a full red → blue → green security drill (Wave C1).

Orchestrates the three security teams in order:
  1. Red team generates adversarial inputs.
  2. Blue team runs them against the real controls and observes outcomes.
  3. Green team produces a remediation report.

Usage::

    from security.run_security_drill import run_security_drill
    report = run_security_drill()
    print(report.to_dict())
"""
from __future__ import annotations

from typing import Any

from security.blue_team import observe
from security.green_team import RemediationReport, remediate
from security.red_team import generate_attacks


def run_security_drill() -> RemediationReport:
    """Run red → blue → green and return the remediation report."""
    # Red: generate the attack catalogue.
    _attacks = generate_attacks()
    # Blue: observe the defences against each attack.
    observations = observe()
    # Green: remediate / confirm.
    return remediate(observations)


def run_security_drill_summary() -> dict[str, Any]:
    """Run the drill and return a serializable summary."""
    return run_security_drill().to_dict()


__all__ = ["run_security_drill", "run_security_drill_summary"]

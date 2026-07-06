"""Effective Trust — a continuous, dynamic security score per run (Wave C3).

Effective Trust treats security as a **continuous, dynamic metric**, not a
one-time deployment gate. For each build run we compute a trust score from the
controls that were *actually exercised* during the run (path containment
calls, identifier escapes, JSON validations, sandbox mode) and the security-
drill outcome. The score is emitted into the run report and ``build.spec.json``
so trust is observable and trendable over time — not just "passed at deploy".

The score is a 0–100 weighted sum of control coverage signals:

  * Path containment (safe_join) exercised ............. 30
  * Identifier escaping exercised ...................... 20
  * JSON validation exercised .......................... 15
  * Sandbox mode active (local/container) .............. 15
  * Security drill outcome (red→blue→green pass) ....... 20

A run that exercises every control and passes the drill scores 100. Missing
signals reduce the score proportionally. The score never *gates* a run (a low
score does not fail the build) — it is an observation, true to the "continuous
metric" framing.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@dataclass
class TrustSignal:
    """One control-coverage signal contributing to the trust score."""

    name: str
    weight: int
    active: bool
    detail: str = ""


@dataclass
class TrustScore:
    """The effective-trust score for one run."""

    score: int  # 0–100
    signals: list[TrustSignal] = field(default_factory=list)
    overall: str = "high"  # high | medium | low

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "overall": self.overall,
            "signals": [
                {"name": s.name, "weight": s.weight, "active": s.active, "detail": s.detail}
                for s in self.signals
            ],
        }


def _sandbox_active() -> bool:
    mode = os.getenv("POWERBI_SANDBOX_MODE", "local").lower()
    return mode in ("local", "container")


def compute_trust_score(
    *,
    safe_join_calls: int = 0,
    identifier_escape_calls: int = 0,
    json_validation_calls: int = 0,
    drill_passed: bool | None = None,
) -> TrustScore:
    """Compute the effective-trust score for a run.

    Each ``*_calls`` count is how many times that control was actually invoked
    during the run (observed from the audit log / trajectory). A count of 0
    means the control was not exercised — not necessarily a failure, but a
    coverage gap the score reflects.

    Args:
        safe_join_calls: number of path-containment checks performed.
        identifier_escape_calls: number of identifier escapes performed.
        json_validation_calls: number of JSON validations performed.
        drill_passed: whether the red→blue→green security drill passed
            (None = drill not run for this build).
    """
    signals: list[TrustSignal] = []

    signals.append(TrustSignal(
        name="path_containment",
        weight=30,
        active=safe_join_calls > 0,
        detail=f"{safe_join_calls} safe_join calls",
    ))
    signals.append(TrustSignal(
        name="identifier_escaping",
        weight=20,
        active=identifier_escape_calls > 0,
        detail=f"{identifier_escape_calls} escape calls",
    ))
    signals.append(TrustSignal(
        name="json_validation",
        weight=15,
        active=json_validation_calls > 0,
        detail=f"{json_validation_calls} validation calls",
    ))
    signals.append(TrustSignal(
        name="sandbox_isolation",
        weight=15,
        active=_sandbox_active(),
        detail=f"mode={os.getenv('POWERBI_SANDBOX_MODE', 'local')}",
    ))
    # The drill signal is active only when a drill was actually run.
    if drill_passed is not None:
        signals.append(TrustSignal(
            name="security_drill",
            weight=20,
            active=bool(drill_passed),
            detail="passed" if drill_passed else "failed",
        ))
    else:
        # Drill not run — weight is still counted but not earned.
        signals.append(TrustSignal(
            name="security_drill",
            weight=20,
            active=False,
            detail="not run for this build",
        ))

    earned = sum(s.weight for s in signals if s.active)
    total = sum(s.weight for s in signals)
    score = round(100 * earned / total) if total else 0
    overall = "high" if score >= 80 else ("medium" if score >= 50 else "low")
    return TrustScore(score=score, signals=signals, overall=overall)


def trust_score_from_drill() -> TrustScore:
    """Convenience: compute a trust score anchored on a fresh security drill.

    Runs the red→blue→green drill and derives the drill signal from its
    outcome. The other control-coverage signals default to 0 (the caller can
    recompute with real call counts if desired).
    """
    from security.run_security_drill import run_security_drill  # noqa: E402

    report = run_security_drill()
    return compute_trust_score(drill_passed=(report.overall == "pass"))


__all__ = ["TrustSignal", "TrustScore", "compute_trust_score", "trust_score_from_drill"]

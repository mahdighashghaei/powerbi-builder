"""Green team — remediation: act on blue-team observations.

The green team *fixes*: it reviews the blue team's observations and either
confirms a defence is adequate or proposes/records a remediation for any gap.
In this showcase it does not patch code at runtime (that would be unsafe); it
produces a ``RemediationReport`` summarising the drill outcome and any
recommended hardening — the human (or a later agent) applies the fixes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from security.blue_team import Observation, observe


@dataclass
class Remediation:
    """A remediation item: either a confirmation or a recommended fix."""

    attack_id: str
    category: str
    status: str  # "defended" | "gap" | "needs_review"
    recommendation: str = ""


@dataclass
class RemediationReport:
    """The green team's output from a security drill."""

    total_attacks: int
    defended: int
    gaps: int
    needs_review: int
    items: list[Remediation] = field(default_factory=list)
    overall: str = "pass"  # pass | fail | needs_review

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_attacks": self.total_attacks,
            "defended": self.defended,
            "gaps": self.gaps,
            "needs_review": self.needs_review,
            "overall": self.overall,
            "items": [
                {
                    "attack_id": r.attack_id,
                    "category": r.category,
                    "status": r.status,
                    "recommendation": r.recommendation,
                }
                for r in self.items
            ],
        }


def remediate(observations: list[Observation] | None = None) -> RemediationReport:
    """Produce a remediation report from blue-team observations.

    For each observation: if defended → ``defended``; otherwise → ``gap`` with a
    recommendation. The overall status is ``fail`` if any gap is found,
    ``needs_review`` if only needs-review items remain, else ``pass``.
    """
    obs = observations if observations is not None else observe()
    items: list[Remediation] = []
    defended = gaps = needs_review = 0
    for o in obs:
        if o.defended:
            defended += 1
            items.append(
                Remediation(
                    attack_id=o.attack_id,
                    category=o.category,
                    status="defended",
                    recommendation=f"Defence held: {o.expected_defense}",
                )
            )
        else:
            gaps += 1
            items.append(
                Remediation(
                    attack_id=o.attack_id,
                    category=o.category,
                    status="gap",
                    recommendation=(
                        f"Defence failed ({o.detail}). Harden the control: "
                        f"{o.expected_defense}."
                    ),
                )
            )
    overall = "fail" if gaps else ("needs_review" if needs_review else "pass")
    return RemediationReport(
        total_attacks=len(obs),
        defended=defended,
        gaps=gaps,
        needs_review=needs_review,
        items=items,
        overall=overall,
    )


__all__ = ["Remediation", "RemediationReport", "remediate"]

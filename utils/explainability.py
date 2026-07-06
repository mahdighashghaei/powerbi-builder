"""utils/explainability.py — Decision logging for pipeline explainability.

Every agent that makes a non-trivial planning decision can call
``log_decision`` so the entire reasoning chain is captured in a structured
log.  At the end of each orchestrator run the decisions are written to
``decisions.log.json`` inside the PBIP root.

Design goals
------------
* **Zero-coupling**: agents import only ``log_decision``; they do not need to
  manage the tracker's lifecycle.
* **Fail-safe**: logging a decision must never raise — errors are silently
  swallowed so a telemetry bug never breaks a build.
* **Thread-safe**: ``ExplainabilityTracker`` uses a lock so parallel ADK
  sub-agents can log safely.
* **Testable**: the tracker is a plain singleton that can be reset between
  tests via ``reset()``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DecisionLog:
    """One recorded planning decision."""

    agent: str
    decision_type: str
    """Coarse category: visual_selection | kpi_recommendation | page_creation |
    measure_rationale | relationship_inferred | clarification | business_analysis."""
    subject: str
    """The entity being decided about (visual name, KPI name, measure name, …)."""
    rationale: str
    alternatives: list[str] = field(default_factory=list)
    confidence: float = 1.0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "decision_type": self.decision_type,
            "subject": self.subject,
            "rationale": self.rationale,
            "alternatives": self.alternatives,
            "confidence": self.confidence,
            "extra": self.extra,
        }


# ---------------------------------------------------------------------------
# Singleton tracker
# ---------------------------------------------------------------------------


class ExplainabilityTracker:
    """Thread-safe, per-run accumulator of ``DecisionLog`` entries.

    One global instance (``_tracker``) is created at module load time.
    Call ``reset()`` between runs (the orchestrator does this automatically).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._decisions: list[DecisionLog] = []

    def reset(self) -> None:
        """Clear all recorded decisions (call at the start of each run)."""
        with self._lock:
            self._decisions = []

    def record(
        self,
        agent: str,
        decision_type: str,
        subject: str,
        rationale: str,
        alternatives: list[str] | None = None,
        confidence: float = 1.0,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Add one decision log entry (fail-safe — never raises)."""
        try:
            entry = DecisionLog(
                agent=agent,
                decision_type=decision_type,
                subject=subject,
                rationale=rationale,
                alternatives=alternatives or [],
                confidence=confidence,
                extra=extra or {},
            )
            with self._lock:
                self._decisions.append(entry)
        except Exception:  # noqa: BLE001
            pass  # telemetry must never crash the pipeline

    def all_decisions(self) -> list[DecisionLog]:
        """Return a snapshot of all recorded decisions."""
        with self._lock:
            return list(self._decisions)

    def as_dicts(self) -> list[dict[str, Any]]:
        """Return JSON-serialisable list of all decision entries."""
        with self._lock:
            return [d.as_dict() for d in self._decisions]

    def __len__(self) -> int:
        with self._lock:
            return len(self._decisions)


# ---------------------------------------------------------------------------
# Module-level singleton + convenience helper
# ---------------------------------------------------------------------------

_tracker = ExplainabilityTracker()


def get_tracker() -> ExplainabilityTracker:
    """Return the module-level singleton tracker."""
    return _tracker


def log_decision(
    agent: str,
    decision_type: str,
    subject: str,
    rationale: str,
    alternatives: list[str] | None = None,
    confidence: float = 1.0,
    extra: dict[str, Any] | None = None,
) -> None:
    """Convenience wrapper — log one decision to the global tracker.

    Usage::

        from utils.explainability import log_decision

        log_decision(
            agent="BIReasoningAgent",
            decision_type="kpi_recommendation",
            subject="Total Revenue",
            rationale="Amount column detected; primary executive KPI.",
            confidence=0.9,
        )
    """
    _tracker.record(agent, decision_type, subject, rationale,
                    alternatives=alternatives, confidence=confidence, extra=extra)


__all__ = ["DecisionLog", "ExplainabilityTracker", "get_tracker", "log_decision"]

"""ADK tools for trajectory evaluation (Wave A4).

These tools let the model (and external evaluators) retrieve the step-by-step
trajectory of an agent run — every tool/agent execution recorded as a span — so
the *path* the agent took to an answer can be inspected, not just the final
output. This is the OpenTelemetry-based trajectory-evaluation pattern.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adk import telemetry  # noqa: E402


def get_trajectory(trace_id: str = "") -> dict[str, Any]:
    """Retrieve the recorded step-by-step trajectory of an agent run.

    Returns every tool/agent execution as a span (name, kind, start, end,
    duration, attributes, status, events) so the agent's *path* to the result
    can be replayed and evaluated — not just the final output. If ``trace_id``
    is omitted, the most recent run's trajectory is returned.

    Args:
        trace_id: Optional run trace id (from ``session.state['otel_run_id']``).
            If empty, the latest run is used.

    Returns:
        ``{"ok": True, "trace_id": str, "count": N, "spans": [...]}`` or
        ``{"ok": True, "trace_id": None, "count": 0, "spans": []}`` if no run
        has been recorded yet.
    """
    data = telemetry.get_trajectory(trace_id or None)
    data["tool"] = "get_trajectory"
    return data


def list_trajectory_runs() -> dict[str, Any]:
    """List all recorded agent-run trace ids (most-recent last).

    Use this to discover which runs have a recorded trajectory before fetching
    one with ``get_trajectory``.

    Returns:
        ``{"ok": True, "tool": "list_trajectory_runs", "runs": [...],
        "count": N}``
    """
    runs = telemetry.list_runs()
    return {
        "ok": True,
        "tool": "list_trajectory_runs",
        "runs": runs,
        "count": len(runs),
    }


__all__ = ["get_trajectory", "list_trajectory_runs"]

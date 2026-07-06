"""ADK tools for run status / progress tracking (Status agent).

Exposes:
  * ``get_project_status`` — read the current project + run state from session
                             state (project name, root, profile, plan, review).
  * ``get_run_summary``    — summarise all agent results known to session state
                             into a single progress report.

The Status agent is callable at any point in the pipeline to surface what has
happened so far and what remains.
"""
from __future__ import annotations

from typing import Any


def get_project_status(tool_context: Any = None) -> dict[str, Any]:
    """Return the current project + run status from session state.

    Reads ``current_project`` / ``current_project_root`` (set by
    ``track_project``) plus any ``data_profile`` / ``plan`` / ``review`` /
    ``cleaning_report`` stored in state by the new agents.
    """
    if tool_context is None:
        return {"ok": False, "errors": ["No tool_context — status requires session state"]}
    try:
        state = tool_context.state
    except Exception:
        return {"ok": False, "errors": ["Could not read session state"]}

    status: dict[str, Any] = {
        "ok": True,
        "project": state.get("current_project"),
        "project_root": state.get("current_project_root"),
    }
    # surface optional artefacts the new agents store
    for key in ("data_profile", "plan", "answers", "cleaning_report", "review"):
        if key in state:
            val = state[key]
            # summarise rather than dump everything
            if key == "data_profile":
                status["quality_score"] = val.get("quality_score") if isinstance(val, dict) else None
                status["issues_count"] = len(val.get("issues", [])) if isinstance(val, dict) else 0
            elif key == "plan":
                status["plan_steps"] = len(val) if isinstance(val, list) else 0
            elif key == "review":
                status["review_score"] = val.get("score") if isinstance(val, dict) else None
            elif key == "cleaning_report":
                status["cleaning_improved"] = val.get("improved") if isinstance(val, dict) else None
    return status


def get_run_summary(tool_context: Any = None) -> dict[str, Any]:
    """Summarise all agent results recorded in session state.

    Returns ``{ok, agents_run: [{agent, ok, message}], total, succeeded, failed}``.
    """
    if tool_context is None:
        return {"ok": False, "errors": ["No tool_context — summary requires session state"]}
    try:
        state = tool_context.state
    except Exception:
        return {"ok": False, "errors": ["Could not read session state"]}

    agents_run: list[dict[str, Any]] = []
    # The orchestrator stores step results; in the ADK path we track via state
    raw_steps = state.get("run_steps", [])
    for step in raw_steps:
        if isinstance(step, dict):
            agents_run.append({
                "agent": step.get("agent", "?"),
                "ok": step.get("ok", False),
                "message": step.get("message", ""),
            })
    total = len(agents_run)
    succeeded = sum(1 for a in agents_run if a["ok"])
    failed = total - succeeded
    return {
        "ok": True,
        "agents_run": agents_run,
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "progress_pct": round(succeeded / max(total, 1) * 100, 1),
    }


__all__ = ["get_project_status", "get_run_summary"]

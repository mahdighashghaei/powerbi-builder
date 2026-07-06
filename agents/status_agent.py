"""StatusAgent -- collects and reports the current run status.

Role
----
Callable at any point in the pipeline (not just the end). It gathers which
agents have run, their results, errors, warnings, and the progress percentage,
then stores a status summary in ``ctx.extra["status"]``.

In the legacy pipeline the orchestrator's ``RunReport`` already holds step
results; this agent formats them into a concise, user-facing summary.
"""
from __future__ import annotations

from typing import Any

from agents.base import AgentResult, BaseAgent


class StatusAgent(BaseAgent):
    """Collects and reports the current run status."""

    name = "StatusAgent"
    description = (
        "You are the StatusAgent. Gather the status of the current run — "
        "which agents have executed, their results, errors, warnings, and "
        "overall progress. Produce a concise summary the user can act on."
    )

    def _run(self) -> AgentResult:
        ctx = self.context
        # Read the run steps from ctx.extra (set by orchestrator before
        # calling StatusAgent). Falls back to an empty list when called early.
        run_steps = ctx.extra.get("run_steps", [])
        agents_run: list[dict[str, Any]] = []
        for step in run_steps:
            agents_run.append({
                "agent": step.get("agent", "?"),
                "ok": step.get("ok", False),
                "message": step.get("message", ""),
                "errors": step.get("errors", []),
            })

        total = len(agents_run)
        succeeded = sum(1 for a in agents_run if a["ok"])
        failed = total - succeeded
        progress = round(succeeded / max(total, 1) * 100, 1) if total else 0.0

        # Gather extras from new agents
        profile = ctx.extra.get("data_profile", {})
        plan = ctx.extra.get("plan", [])
        review = ctx.extra.get("review", {})
        cleaning = ctx.extra.get("cleaning_report", {})

        status = {
            "agents_run": agents_run,
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "progress_pct": progress,
            "quality_score": profile.get("quality_score") if profile else None,
            "plan_steps": len(plan) if plan else 0,
            "review_score": review.get("score") if review else None,
            "cleaning_improved": cleaning.get("improved") if cleaning else None,
        }
        ctx.extra["status"] = status

        summary_lines = [f"Run status: {succeeded}/{total} agents succeeded ({progress}%)"]
        if profile:
            summary_lines.append(f"  Data quality: {profile.get('quality_score', '?')}/100")
        if cleaning:
            summary_lines.append(
                f"  Cleaning: {'improved' if cleaning.get('improved') else 'no improvement'}"
            )
        if review:
            summary_lines.append(f"  Review score: {review.get('score', '?')}/100")
        for a in agents_run:
            mark = "✓" if a["ok"] else "✗"
            summary_lines.append(f"  {mark} {a['agent']}: {a['message'][:60]}")
        summary = "\n".join(summary_lines)

        self.log.info(f"status: {succeeded}/{total} ok, {progress}% progress")
        return AgentResult(
            agent=self.name, ok=True,
            message=summary,
            data=status,
        )

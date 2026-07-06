"""ReportReviewerAgent -- semantic review of the generated report.

Role
----
Runs AFTER ValidatorAgent. While the Validator checks *structure* (files
exist, JSON is valid, TMDL is well-formed), the Reviewer checks *semantics*:
  * Do visuals reference real measures/columns (no ghost references)?
  * Are visual types appropriate for the data (lineChart for dates, etc.)?
  * Is the visual count per page reasonable?
  * Do any visuals overlap on the canvas?
  * Are key measures (Total, Count) present?

It stores the review in ``ctx.extra["review"]`` and sets ``ok=False`` when
critical issues (ghost references, empty pages) are found.

Issues are output as structured dicts with the schema:
  {
    "severity": "info | warning | high | critical",
    "agent_responsible": "string",
    "message": "string",
    "context": {}
  }
This enables the feedback loop in _run_feedback_loop to correctly route
issues to the responsible agent for targeted fixes.
"""
from __future__ import annotations

from pathlib import Path

from agents.base import AgentResult, BaseAgent
from utils import pbip_paths as paths


def _classify_issue(issue_str: str) -> dict:
    """Convert a plain-text review issue string into a structured issue dict.

    Assigns severity and agent_responsible based on issue content so the
    feedback loop can route the issue to the correct agent for fixing.
    """
    s = issue_str.lower()

    # Ghost references → ReportAgent must fix (wrong visual bindings)
    if "ghost reference" in s or "ghost ref" in s:
        return {
            "severity": "critical",
            "agent_responsible": "ReportAgent",
            "message": issue_str,
            "context": {"issue_type": "ghost_reference"},
        }

    # Empty page (no visuals) → ReportAgent must add visuals
    #
    # NOTE: only match the literal phrase review_tools.py actually emits
    # ("has no visuals.") — NOT a bare "0 visuals" substring check, which
    # false-positives on any legitimate "N0 visuals" count message (e.g.
    # "has 10 visuals", "has 20 visuals" both contain "0 visuals" as a
    # substring). That bug misclassified a rich-style page holding a
    # genuinely large (but valid) visual count as a critical empty page.
    if "has no visuals" in s:
        return {
            "severity": "critical",
            "agent_responsible": "ReportAgent",
            "message": issue_str,
            "context": {"issue_type": "empty_page"},
        }

    # Too many visuals on a page — advisory only, NOT auto-retryable: the
    # orchestrator's feedback loop treats "warning"+"error" severities as
    # routable (worth a rerun), but ReportAgent's page-packing
    # (split_to_pages) is deterministic and this issue_type never bumps
    # any scoring weight (unlike "layout_overlap" bumping visual_quality),
    # so a rerun with identical inputs reproduces the identical visual
    # count every time. Classifying this as "warning" wasted the ENTIRE
    # feedback-loop retry budget (3 attempts) on every build with a
    # >10-visual page — confirmed live via a real adk web session that
    # appeared to hang. Still surfaced in the review's issue list, just
    # not auto-retried.
    if "too many visuals" in s or "consider splitting" in s:
        return {
            "severity": "info",
            "agent_responsible": "ReportAgent",
            "message": issue_str,
            "context": {"issue_type": "visual_count"},
        }

    # Layout overlap → ReportAgent must fix layout
    if "overlap" in s:
        return {
            "severity": "warning",
            "agent_responsible": "ReportAgent",
            "message": issue_str,
            "context": {"issue_type": "layout_overlap"},
        }

    # Missing measures → DAXAgent should add them
    if "total" in s or "count" in s or "measure" in s:
        return {
            "severity": "warning",
            "agent_responsible": "DAXAgent",
            "message": issue_str,
            "context": {"issue_type": "missing_measure"},
        }

    # Default: informational, no responsible agent
    return {
        "severity": "info",
        "agent_responsible": "",
        "message": issue_str,
        "context": {},
    }


class ReportReviewerAgent(BaseAgent):
    """Semantic review of the generated PBIP report."""

    name = "ReportReviewerAgent"
    description = (
        "You are the ReportReviewerAgent. After the report is built and "
        "structurally validated, review it semantically: check for ghost "
        "references (visuals pointing to non-existent measures/columns), "
        "visual/data-type compatibility, layout overlaps, and measure "
        "coverage. Produce a review with a score, strengths, issues, and "
        "suggestions. Flag critical issues as errors."
    )

    def _run(self) -> AgentResult:
        ctx = self.context
        pbip_root = Path(ctx.pbip_root)

        from adk.tools.review_tools import review_report
        result = review_report(str(pbip_root))

        if not result.get("ok"):
            return AgentResult(
                agent=self.name, ok=False,
                message=f"Review failed: {result.get('errors', [])}",
                errors=result.get("errors", []),
            )

        # Convert plain-text issue strings into structured dicts so the
        # feedback loop can route them to the responsible agent.
        raw_issues = result.get("issues", [])
        structured_issues = []
        for issue in raw_issues:
            if isinstance(issue, dict):
                # Already structured — ensure required keys exist
                structured_issues.append({
                    "severity": issue.get("severity", "warning"),
                    "agent_responsible": issue.get("agent_responsible", ""),
                    "message": issue.get("message", str(issue)),
                    "context": issue.get("context", {}),
                })
            else:
                # Plain string — classify into a structured dict
                structured_issues.append(_classify_issue(str(issue)))

        review = {
            "score": result.get("score", 100),
            "strengths": result.get("strengths", []),
            "issues": structured_issues,
            "suggestions": result.get("suggestions", []),
            "total_pages": result.get("total_pages", 0),
            "total_visuals": result.get("total_visuals", 0),
            "ghost_count": result.get("ghost_count", 0),
        }
        ctx.extra["review"] = review

        # Critical issues: ghost references or empty pages block ok=True
        critical = [
            i for i in structured_issues
            if i.get("severity") in ("critical",)
        ]
        ok = len(critical) == 0

        # Build plain error strings for AgentResult.errors (backward compat)
        error_msgs = [i["message"] for i in critical]

        self.log.info(
            f"review: score={review['score']}, issues={len(structured_issues)}, "
            f"ghosts={review['ghost_count']}, critical={len(critical)}"
        )

        return AgentResult(
            agent=self.name,
            ok=ok,
            message=(
                f"Report review: score {review['score']}/100, "
                f"{len(structured_issues)} issue(s)"
                + (f", {len(critical)} critical" if critical else "")
            ),
            data=review,
            errors=error_msgs,
        )

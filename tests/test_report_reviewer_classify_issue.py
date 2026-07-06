"""tests/test_report_reviewer_classify_issue.py — regression for a
substring false-positive in ``agents.report_reviewer_agent._classify_issue``.

Bug: the empty-page check was ``"no visuals" in s or "0 visuals" in s``.
``review_tools.py`` never actually emits the literal string "0 visuals" —
it emits "has no visuals." for a genuinely empty page. But "0 visuals" as
a bare substring also matches "has **1**0 visuals", "has **2**0 visuals",
etc., so any legitimately large (but valid) per-page visual count ending
in a zero digit got misclassified as a CRITICAL empty page, failing the
whole build. This surfaced when a rich-style report's first page
legitimately held 20 visuals (14 cards + 6 charts).
"""
from __future__ import annotations

import unittest


class TestClassifyIssue(unittest.TestCase):
    def test_genuinely_empty_page_is_critical(self):
        from agents.report_reviewer_agent import _classify_issue

        result = _classify_issue("Page 'summary-page' has no visuals.")
        self.assertEqual(result["severity"], "critical")
        self.assertEqual(result["context"]["issue_type"], "empty_page")

    def test_twenty_visuals_is_not_misclassified_as_empty(self):
        from agents.report_reviewer_agent import _classify_issue

        result = _classify_issue("Page 'summary-page' has 20 visuals — consider splitting.")
        # "info", not "critical" -- see TestVisualCountIsNotRoutable below for
        # why it's also not "warning" (not auto-retryable).
        self.assertNotEqual(result["severity"], "critical")
        self.assertEqual(result["context"]["issue_type"], "visual_count")

    def test_ten_visuals_is_not_misclassified_as_empty(self):
        """Same bug class: '1' + '0 visuals' also contains the substring."""
        from agents.report_reviewer_agent import _classify_issue

        result = _classify_issue("Page 'summary-page' has 10 visuals — consider splitting.")
        self.assertNotEqual(result["severity"], "critical")

    def test_thirty_visuals_is_not_misclassified_as_empty(self):
        from agents.report_reviewer_agent import _classify_issue

        result = _classify_issue("Page 'p2' has 30 visuals — consider splitting.")
        self.assertNotEqual(result["severity"], "critical")

    def test_ghost_reference_is_still_critical(self):
        from agents.report_reviewer_agent import _classify_issue

        result = _classify_issue(
            "Ghost reference: visual 'card-x' on page 'p1' references measure "
            "'Foo' which does not exist in the model."
        )
        self.assertEqual(result["severity"], "critical")
        self.assertEqual(result["context"]["issue_type"], "ghost_reference")

    def test_overlap_is_still_warning(self):
        from agents.report_reviewer_agent import _classify_issue

        result = _classify_issue("Visuals 'a' and 'b' on page 'p1' overlap.")
        self.assertEqual(result["severity"], "warning")
        self.assertEqual(result["context"]["issue_type"], "layout_overlap")


class TestVisualCountIsNotRoutable(unittest.TestCase):
    """Regression: a real adk web session appeared to hang because every
    build with a >10-visual page (increasingly common after the
    visual_variety="all" feature added more candidate types) wasted the
    orchestrator's ENTIRE 3-attempt feedback-loop budget retrying a
    "too many visuals — consider splitting" warning. ReportAgent's
    page-packing (split_to_pages) is deterministic given the same inputs,
    and this issue_type never adjusts any scoring weight between retries
    (unlike "layout_overlap", which bumps visual_quality) -- so a rerun is
    guaranteed to reproduce the identical outcome every single time.
    Demoted to "info" so it's still reported but never auto-retried."""

    def test_classified_as_info_not_warning(self):
        from agents.report_reviewer_agent import _classify_issue

        result = _classify_issue("Page 'summary-page' has 12 visuals — consider splitting.")
        self.assertEqual(result["severity"], "info")

    def test_excluded_from_orchestrator_routable_filter(self):
        """Mirrors the orchestrator's own routable-issue filter
        (agents/orchestrator.py's feedback loop:
        i.get("severity") in ("error", "warning") and i.get("agent_responsible"))."""
        from agents.report_reviewer_agent import _classify_issue

        result = _classify_issue("Page 'summary-page' has 12 visuals — consider splitting.")
        is_routable = (
            result.get("severity") in ("error", "warning")
            and result.get("agent_responsible")
        )
        self.assertFalse(is_routable)


if __name__ == "__main__":
    unittest.main()

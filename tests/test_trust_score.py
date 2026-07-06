"""Tests for the Effective Trust score (Wave C3).

Verifies:
  * compute_trust_score returns 0–100 with weighted signals.
  * Full control coverage + passing drill → score 100 (high).
  * No coverage → low score.
  * Partial coverage → medium score.
  * The drill-not-run case is scored honestly (signal inactive).
  * trust_score_from_drill derives the drill signal from a real drill.
  * The score is serializable.

Stdlib unittest — runs under ``python -m pytest tests/ -v``.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class TestComputeTrustScore(unittest.TestCase):
    """Weighted control-coverage scoring."""

    def tearDown(self):
        os.environ.pop("POWERBI_SANDBOX_MODE", None)

    def test_full_coverage_and_drill_pass_scores_100(self):
        from security.trust_score import compute_trust_score  # noqa: E402

        score = compute_trust_score(
            safe_join_calls=10,
            identifier_escape_calls=5,
            json_validation_calls=3,
            drill_passed=True,
        )
        self.assertEqual(score.score, 100)
        self.assertEqual(score.overall, "high")
        # Every signal is active.
        self.assertTrue(all(s.active for s in score.signals))

    def test_no_coverage_scores_low(self):
        from security.trust_score import compute_trust_score  # noqa: E402

        os.environ["POWERBI_SANDBOX_MODE"] = "disabled"
        score = compute_trust_score(drill_passed=False)
        self.assertLess(score.score, 50)
        self.assertEqual(score.overall, "low")

    def test_partial_coverage_scores_medium(self):
        from security.trust_score import compute_trust_score  # noqa: E402

        # Only path containment + sandbox active.
        score = compute_trust_score(safe_join_calls=5, drill_passed=None)
        # path(30) + sandbox(15) = 45 of 100 → medium-ish.
        self.assertGreaterEqual(score.score, 40)
        self.assertLess(score.score, 80)

    def test_drill_not_run_is_honest(self):
        from security.trust_score import compute_trust_score  # noqa: E402

        score = compute_trust_score(
            safe_join_calls=1,
            identifier_escape_calls=1,
            json_validation_calls=1,
            drill_passed=None,  # drill not run
        )
        drill = next(s for s in score.signals if s.name == "security_drill")
        self.assertFalse(drill.active)
        self.assertIn("not run", drill.detail)
        # Score is < 100 because the drill signal is unearned.
        self.assertLess(score.score, 100)

    def test_score_is_serializable(self):
        from security.trust_score import compute_trust_score  # noqa: E402

        d = compute_trust_score(safe_join_calls=1, drill_passed=True).to_dict()
        self.assertIn("score", d)
        self.assertIn("overall", d)
        self.assertIsInstance(d["signals"], list)
        self.assertTrue(d["signals"])

    def test_score_never_exceeds_100(self):
        from security.trust_score import compute_trust_score  # noqa: E402

        score = compute_trust_score(
            safe_join_calls=999,
            identifier_escape_calls=999,
            json_validation_calls=999,
            drill_passed=True,
        )
        self.assertLessEqual(score.score, 100)


class TestTrustScoreFromDrill(unittest.TestCase):
    """trust_score_from_drill anchors the drill signal on a real drill."""

    def test_drill_pass_yields_drill_signal_active(self):
        from security.trust_score import trust_score_from_drill  # noqa: E402

        score = trust_score_from_drill()
        drill = next(s for s in score.signals if s.name == "security_drill")
        # The real controls defend every attack, so the drill passes.
        self.assertTrue(drill.active)
        self.assertEqual(drill.detail, "passed")


if __name__ == "__main__":
    unittest.main()

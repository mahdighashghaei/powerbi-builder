"""Tests for the red/blue/green security teams (Wave C1).

Verifies:
  * Red team generates a non-empty catalogue of adversarial inputs.
  * Blue team observations cover every red-team attack.
  * Path-traversal attacks are defended (safe_join holds).
  * Identifier-injection attacks are defended (quoting holds).
  * Malformed-JSON attacks are defended (validation holds).
  * Green team produces a remediation report with a pass/fail verdict.
  * The full drill (run_security_drill) yields all attacks defended.

Stdlib unittest — runs under ``python -m pytest tests/ -v``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class TestRedTeam(unittest.TestCase):
    """Red team generates adversarial inputs."""

    def test_generates_attacks(self):
        from security.red_team import generate_attacks  # noqa: E402

        attacks = generate_attacks()
        self.assertGreater(len(attacks), 0)
        ids = {a.id for a in attacks}
        self.assertFalse(any(a.id == "" for a in attacks))
        # Each attack has a category, description, and expected defence.
        for a in attacks:
            self.assertTrue(a.category)
            self.assertTrue(a.description)
            self.assertTrue(a.expected_defense)

    def test_covers_key_categories(self):
        from security.red_team import generate_attacks  # noqa: E402

        cats = {a.category for a in generate_attacks()}
        self.assertIn("path_traversal", cats)
        self.assertIn("identifier_injection", cats)
        self.assertIn("malformed_json", cats)


class TestBlueTeam(unittest.TestCase):
    """Blue team observes defence outcomes."""

    def test_observations_cover_all_attacks(self):
        from security.blue_team import observe  # noqa: E402
        from security.red_team import generate_attacks  # noqa: E402

        obs = observe()
        self.assertEqual(len(obs), len(generate_attacks()))

    def test_path_traversal_defended(self):
        from security.blue_team import observe  # noqa: E402

        obs = observe()
        pt = [o for o in obs if o.category == "path_traversal"]
        self.assertTrue(pt)
        for o in pt:
            self.assertTrue(o.defended, f"traversal not defended: {o.description} ({o.detail})")

    def test_identifier_injection_defended(self):
        from security.blue_team import observe  # noqa: E402

        obs = observe()
        ii = [o for o in obs if o.category == "identifier_injection"]
        self.assertTrue(ii)
        for o in ii:
            self.assertTrue(o.defended, f"injection not defended: {o.description} ({o.detail})")

    def test_malformed_json_defended(self):
        from security.blue_team import observe  # noqa: E402

        obs = observe()
        mj = [o for o in obs if o.category == "malformed_json"]
        self.assertTrue(mj)
        for o in mj:
            self.assertTrue(o.defended, f"malformed JSON not defended: {o.description}")


class TestGreenTeam(unittest.TestCase):
    """Green team produces a remediation report."""

    def test_remediate_report_shape(self):
        from security.green_team import remediate  # noqa: E402
        from security.blue_team import observe  # noqa: E402

        report = remediate(observe())
        self.assertGreater(report.total_attacks, 0)
        self.assertEqual(report.total_attacks, report.defended + report.gaps + report.needs_review)
        self.assertIn(report.overall, {"pass", "fail", "needs_review"})
        # Each item has a status + recommendation.
        for item in report.items:
            self.assertIn(item.status, {"defended", "gap", "needs_review"})

    def test_remediate_serializable(self):
        from security.green_team import remediate  # noqa: E402
        from security.blue_team import observe  # noqa: E402

        d = remediate(observe()).to_dict()
        self.assertIn("total_attacks", d)
        self.assertIn("items", d)
        self.assertIsInstance(d["items"], list)


class TestFullDrill(unittest.TestCase):
    """The orchestrated red→blue→green drill."""

    def test_run_security_drill_all_defended(self):
        from security.run_security_drill import run_security_drill  # noqa: E402

        report = run_security_drill()
        # The real controls should defend every attack in the catalogue.
        self.assertEqual(report.gaps, 0, f"security gaps found: {report.to_dict()}")
        self.assertEqual(report.overall, "pass")

    def test_summary_is_serializable(self):
        from security.run_security_drill import run_security_drill_summary  # noqa: E402

        summary = run_security_drill_summary()
        self.assertIn("overall", summary)
        self.assertGreater(summary["total_attacks"], 0)


if __name__ == "__main__":
    unittest.main()

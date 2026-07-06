"""tests/test_concept_coverage.py — Unit tests for Concept Coverage Enforcement.

Covers:
  - utils.concept_coverage.extract_concepts
  - utils.concept_coverage.check_concept_coverage / missing_concepts
  - utils.concept_coverage.concept_coverage_score (including the neutral
    1.0 default when no concepts are named)
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace


class TestExtractConcepts(unittest.TestCase):
    def test_extracts_named_concepts(self):
        from utils.concept_coverage import extract_concepts

        desc = "track revenue, profit margin, and discount impact"
        concepts = extract_concepts(desc)
        self.assertIn("revenue", concepts)
        self.assertIn("profit", concepts)
        self.assertIn("margin", concepts)
        self.assertIn("discount", concepts)
        self.assertNotIn("growth", concepts)
        self.assertNotIn("cost", concepts)

    def test_no_concepts_named_returns_empty(self):
        from utils.concept_coverage import extract_concepts

        self.assertEqual(extract_concepts("build a report"), [])

    def test_empty_description_is_fail_safe(self):
        from utils.concept_coverage import extract_concepts

        self.assertEqual(extract_concepts(""), [])
        self.assertEqual(extract_concepts(None), [])


class TestCheckConceptCoverage(unittest.TestCase):
    def test_covered_and_missing_concepts(self):
        from utils.concept_coverage import check_concept_coverage, missing_concepts

        measures = [
            {"name": "Total Sales"},
            {"name": "Profit Margin %"},
        ]
        coverage = check_concept_coverage(["revenue", "margin", "discount"], measures)
        self.assertTrue(coverage["revenue"]["covered"])
        self.assertTrue(coverage["margin"]["covered"])
        self.assertFalse(coverage["discount"]["covered"])
        self.assertEqual(missing_concepts(coverage), ["discount"])

    def test_measure_coverage_implies_visual_coverage(self):
        from utils.concept_coverage import check_concept_coverage

        coverage = check_concept_coverage(["revenue"], [{"name": "Total Sales"}])
        self.assertTrue(coverage["revenue"]["has_measure"])
        self.assertTrue(coverage["revenue"]["has_visual"])

    def test_insight_coverage_detected(self):
        from utils.concept_coverage import check_concept_coverage

        insights = SimpleNamespace(
            trends=[], segments=[], underperformers=[],
            kpi_gap_suggestions=[SimpleNamespace(suggestion="Add a discount rate measure")],
        )
        coverage = check_concept_coverage(["discount"], [], insights=insights)
        self.assertTrue(coverage["discount"]["has_insight"])
        # an insight mention alone (no measure yet) does not count as "covered"
        self.assertFalse(coverage["discount"]["covered"])

    def test_no_concepts_returns_empty_dict(self):
        from utils.concept_coverage import check_concept_coverage

        self.assertEqual(check_concept_coverage([], [{"name": "Total Sales"}]), {})


class TestConceptCoverageScore(unittest.TestCase):
    def test_partial_coverage_fraction(self):
        from utils.concept_coverage import check_concept_coverage, concept_coverage_score

        coverage = check_concept_coverage(
            ["revenue", "margin"], [{"name": "Total Sales"}],
        )
        self.assertAlmostEqual(concept_coverage_score(coverage), 0.5)

    def test_no_concepts_named_scores_neutral(self):
        from utils.concept_coverage import concept_coverage_score

        self.assertEqual(concept_coverage_score({}), 1.0)
        self.assertEqual(concept_coverage_score(None), 1.0)

    def test_full_coverage_scores_one(self):
        from utils.concept_coverage import check_concept_coverage, concept_coverage_score

        coverage = check_concept_coverage(["revenue"], [{"name": "Total Sales"}])
        self.assertEqual(concept_coverage_score(coverage), 1.0)


class TestConversionConcept(unittest.TestCase):
    """Binary-Outcome KPI Synthesis: the non-financial-domain counterpart to
    revenue/profit/margin — datasets with no monetary column (marketing
    response, churn, fraud) still get concept coverage tracked."""

    def test_extracts_conversion_from_description(self):
        from utils.concept_coverage import extract_concepts

        concepts = extract_concepts("track subscription conversion rate and campaign effectiveness")
        self.assertIn("conversion", concepts)

    def test_extracts_conversion_via_churn_synonym(self):
        from utils.concept_coverage import extract_concepts

        self.assertIn("conversion", extract_concepts("monitor customer churn"))

    def test_generic_outcome_measure_satisfies_conversion_concept(self):
        from utils.concept_coverage import check_concept_coverage

        coverage = check_concept_coverage(
            ["conversion"], [{"name": "Conversion Rate %"}],
        )
        self.assertTrue(coverage["conversion"]["covered"])

    def test_column_specific_outcome_measure_is_a_disclosed_limitation(self):
        """A column-specific guaranteed measure name (e.g. "Subscribed Rate
        %") does NOT match the "conversion rate" marker -- disclosed,
        accepted gap (see utils/concept_coverage.py module docstring): the
        measure is still generated unconditionally by
        DAXAgent._ensure_outcome_rate_measure regardless of this check."""
        from utils.concept_coverage import check_concept_coverage

        coverage = check_concept_coverage(
            ["conversion"], [{"name": "Subscribed Rate %"}],
        )
        self.assertFalse(coverage["conversion"]["covered"])

    def test_conversion_marker_does_not_false_match_unrelated_rate_measures(self):
        """The "conversion" concept marker must not be accidentally satisfied
        by unrelated ratio measures already guaranteed for other concepts."""
        from utils.concept_coverage import check_concept_coverage

        coverage = check_concept_coverage(
            ["conversion"],
            [{"name": "Discount Rate %"}, {"name": "Cost Ratio %"}, {"name": "Gross Margin %"}],
        )
        self.assertFalse(coverage["conversion"]["covered"])


if __name__ == "__main__":
    unittest.main()

"""tests/test_outcome_detection.py — Unit tests for Binary-Outcome KPI Synthesis
(`utils.kpi_prioritizer.detect_outcome_column`).

Covers the gap found running the full pipeline against a real bank-marketing
dataset: no monetary "amount" column exists, so the pipeline fell back to
generic filler measures (Order Count, Min/Max age) instead of a conversion-
rate measure derived from the binary "y" (yes/no) outcome column.
"""
from __future__ import annotations

import unittest


def _profile(**col_specs):
    """Build a minimal data_profile dict: {col_name: (distinct_count, [values])}."""
    columns = {}
    for name, (distinct_count, values) in col_specs.items():
        columns[name] = {"distinct_count": distinct_count, "distinct_values": values}
    return {"quality": {"columns": columns}}


class TestDetectOutcomeColumn(unittest.TestCase):
    def test_detects_placeholder_named_column(self):
        from utils.kpi_prioritizer import detect_outcome_column

        schema = [{"name": n} for n in ("age", "job", "y")]
        profile = _profile(y=(2, ["no", "yes"]))
        result = detect_outcome_column(schema, profile, "bank marketing dashboard")
        self.assertIsNotNone(result)
        self.assertEqual(result["column"], "y")
        self.assertEqual(result["positive_value"], "yes")
        self.assertEqual(result["measure_name"], "Conversion Rate %")

    def test_prefers_strong_hint_over_weak_hint_on_tie(self):
        """Regression: the real bank-marketing schema has 'default' (a weak
        hint, ambiguous domain word) appearing BEFORE 'y' (the true target,
        a strong placeholder hint) in column order — 'y' must still win via
        the strong-hint + last-column bonus, not lose to raw column order."""
        from utils.kpi_prioritizer import detect_outcome_column

        schema = [{"name": n} for n in ("age", "default", "housing", "y")]
        profile = _profile(
            default=(2, ["no", "yes"]),
            housing=(2, ["no", "yes"]),
            y=(2, ["no", "yes"]),
        )
        result = detect_outcome_column(schema, profile, "marketing dashboard")
        self.assertIsNotNone(result)
        self.assertEqual(result["column"], "y")

    def test_column_specific_name_gets_titled_rate_label(self):
        from utils.kpi_prioritizer import detect_outcome_column

        schema = [{"name": n} for n in ("age", "subscribed")]
        profile = _profile(subscribed=(2, ["False", "True"]))
        result = detect_outcome_column(schema, profile, "subscription tracking")
        self.assertIsNotNone(result)
        self.assertEqual(result["column"], "subscribed")
        self.assertEqual(result["measure_name"], "Subscribed Rate %")

    def test_positive_value_preserves_original_casing(self):
        from utils.kpi_prioritizer import detect_outcome_column

        schema = [{"name": "target"}]
        profile = _profile(target=(2, ["No", "Yes"]))  # capitalized in the source data
        result = detect_outcome_column(schema, profile, "")
        self.assertEqual(result["positive_value"], "Yes")

    def test_description_hint_alone_is_not_sufficient(self):
        """A binary value-vocab match alone (no name hint, not the last
        column, no description hint) is not enough to clear the fire bar --
        otherwise almost any yes/no feature column would be mistaken for
        the outcome."""
        from utils.kpi_prioritizer import detect_outcome_column

        # "married" is NOT the last column here (unlike a single-column
        # schema, where it would get an unearned last-column bonus).
        schema = [{"name": n} for n in ("married", "other")]
        profile = _profile(married=(2, ["no", "yes"]))
        result = detect_outcome_column(schema, profile, "")
        # value-vocab (2) alone < _OUTCOME_MIN_FIRE_SCORE (3) -- must not fire
        self.assertIsNone(result)

    def test_false_positive_guard_gender_column(self):
        """The exact scenario Part 2's philosophy warns about: a plain
        demographic binary must never be mistaken for an outcome column."""
        from utils.kpi_prioritizer import detect_outcome_column

        schema = [{"name": n} for n in ("Gender", "Age", "Salary")]
        profile = _profile(Gender=(2, ["F", "M"]))
        result = detect_outcome_column(schema, profile, "employee dashboard")
        self.assertIsNone(result)

    def test_no_data_profile_returns_none(self):
        from utils.kpi_prioritizer import detect_outcome_column

        schema = [{"name": "y"}]
        self.assertIsNone(detect_outcome_column(schema, None, ""))
        self.assertIsNone(detect_outcome_column(schema, {}, ""))

    def test_no_schema_columns_returns_none(self):
        from utils.kpi_prioritizer import detect_outcome_column

        self.assertIsNone(detect_outcome_column([], _profile(y=(2, ["no", "yes"])), ""))

    def test_no_two_value_column_returns_none(self):
        from utils.kpi_prioritizer import detect_outcome_column

        schema = [{"name": "job"}]
        profile = _profile(job=(12, ["admin", "blue-collar", "technician"]))
        self.assertIsNone(detect_outcome_column(schema, profile, ""))

    def test_alternate_binary_vocab_true_false(self):
        from utils.kpi_prioritizer import detect_outcome_column

        schema = [{"name": "converted"}]
        profile = _profile(converted=(2, ["false", "true"]))
        result = detect_outcome_column(schema, profile, "")
        self.assertIsNotNone(result)
        self.assertEqual(result["positive_value"], "true")

    def test_last_column_convention_breaks_ties_among_equal_hints(self):
        """When neither column has a name-hint match, the ML/BI convention
        'the label is the last column' should decide, not raw order."""
        from utils.kpi_prioritizer import detect_outcome_column

        schema = [{"name": n} for n in ("purchased", "clicked")]
        profile = _profile(
            purchased=(2, ["no", "yes"]),
            clicked=(2, ["no", "yes"]),
        )
        result = detect_outcome_column(schema, profile, "")
        # both are weak hints + value-vocab match (score 3 each) -- "clicked"
        # is last, so it gets the +1 last-column bonus and wins outright.
        self.assertEqual(result["column"], "clicked")


if __name__ == "__main__":
    unittest.main()

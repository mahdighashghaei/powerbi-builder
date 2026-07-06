"""tests/test_semantic_model.py — Unit tests for the Semantic Truth Layer.

Covers:
  - utils.semantic_model.compute_schema_fingerprint (stability, order-independence)
  - utils.semantic_model.discover_semantic_relationships (sum-decomposition +
    product-chain discovery, correct role binding on the exact SampleData.csv
    shape, fail-safe on bad/missing data)
  - utils.learning_memory.LearningMemory semantic-model persistence
    (get/set, save/load round-trip) — the "don't re-learn" requirement.

All tests use synthetic pandas DataFrames — no dependency on repo-root
sample data.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd


def _financial_dataframe(n: int = 30) -> pd.DataFrame:
    """Synthetic data with the exact SampleData.csv relationship shape:
    Gross Sales = Units Sold * Sale Price
    Sales = Gross Sales - Discounts
    Profit = Sales - COGS
    """
    rows = []
    for i in range(n):
        units = 10.0 + i
        sale_price = 5.0
        gross_sales = units * sale_price
        discounts = gross_sales * 0.05
        sales = gross_sales - discounts
        cogs = sales * 0.6
        profit = sales - cogs
        rows.append({
            "Units Sold": units, "Sale Price": sale_price, "Gross Sales": gross_sales,
            "Discounts": discounts, "Sales": sales, "COGS": cogs, "Profit": profit,
        })
    return pd.DataFrame(rows)


class TestComputeSchemaFingerprint(unittest.TestCase):
    def test_same_columns_any_order_same_fingerprint(self):
        from utils.semantic_model import compute_schema_fingerprint

        cols_a = [{"name": "Sales", "dataType": "double"}, {"name": "Profit", "dataType": "double"}]
        cols_b = [{"name": "Profit", "dataType": "double"}, {"name": "Sales", "dataType": "double"}]
        self.assertEqual(compute_schema_fingerprint(cols_a), compute_schema_fingerprint(cols_b))

    def test_different_columns_different_fingerprint(self):
        from utils.semantic_model import compute_schema_fingerprint

        cols_a = [{"name": "Sales", "dataType": "double"}]
        cols_b = [{"name": "Revenue", "dataType": "double"}]
        self.assertNotEqual(compute_schema_fingerprint(cols_a), compute_schema_fingerprint(cols_b))

    def test_empty_input_does_not_crash(self):
        from utils.semantic_model import compute_schema_fingerprint

        self.assertIsInstance(compute_schema_fingerprint([]), str)


class TestDiscoverSemanticRelationships(unittest.TestCase):
    def _cols(self, *names):
        return [{"name": n} for n in names]

    def test_discovers_correct_roles_on_financial_shape(self):
        from utils.semantic_model import discover_semantic_relationships

        df = _financial_dataframe()
        cols = self._cols("Gross Sales", "Discounts", "Sales", "COGS", "Profit")
        model = discover_semantic_relationships(df, cols)
        canonical = model["canonical_metrics"]
        self.assertEqual(canonical["gross_revenue"], "Gross Sales")
        self.assertEqual(canonical["net_revenue"], "Sales")
        self.assertEqual(canonical["deduction"], "Discounts")
        self.assertEqual(canonical["profit"], "Profit")
        self.assertEqual(canonical["cost"], "COGS")

    def test_discovers_product_relationship(self):
        from utils.semantic_model import discover_semantic_relationships

        df = _financial_dataframe()
        cols = self._cols("Units Sold", "Sale Price", "Gross Sales")
        model = discover_semantic_relationships(df, cols)
        products = [r for r in model["relationships"] if r["type"] == "product"]
        self.assertTrue(products)
        self.assertEqual(products[0]["total"], "Gross Sales")
        self.assertEqual(set([products[0]["factor_a"], products[0]["factor_b"]]),
                          {"Units Sold", "Sale Price"})

    def test_no_relationship_found_for_unrelated_columns(self):
        from utils.semantic_model import discover_semantic_relationships

        df = pd.DataFrame({
            "A": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "B": [9.0, 1.0, 4.0, 2.0, 7.0, 3.0],
        })
        model = discover_semantic_relationships(df, self._cols("A", "B"))
        self.assertEqual(model["relationships"], [])
        self.assertEqual(model["canonical_metrics"], {})

    def test_none_dataframe_is_fail_safe(self):
        from utils.semantic_model import discover_semantic_relationships

        model = discover_semantic_relationships(None, self._cols("A", "B"))
        self.assertEqual(model, {"entities": {}, "relationships": [], "canonical_metrics": {}})

    def test_too_few_rows_is_fail_safe(self):
        from utils.semantic_model import discover_semantic_relationships

        df = pd.DataFrame({"A": [1.0], "B": [2.0], "C": [3.0]})
        model = discover_semantic_relationships(df, self._cols("A", "B", "C"))
        self.assertEqual(model["relationships"], [])

    def test_single_column_is_fail_safe(self):
        from utils.semantic_model import discover_semantic_relationships

        df = _financial_dataframe()
        model = discover_semantic_relationships(df, self._cols("Sales"))
        self.assertEqual(model["relationships"], [])


class TestLearningMemorySemanticModelPersistence(unittest.TestCase):
    def _memory(self):
        from utils.learning_memory import LearningMemory

        tmp = tempfile.mkdtemp()
        return LearningMemory(Path(tmp) / "learning_memory.json")

    def test_set_and_get_round_trip(self):
        lm = self._memory()
        model = {"canonical_metrics": {"profit": "Profit", "net_revenue": "Sales"}}
        lm.set_semantic_model("fp123", model)
        self.assertEqual(lm.get_semantic_model("fp123"), model)

    def test_unknown_fingerprint_returns_none(self):
        lm = self._memory()
        self.assertIsNone(lm.get_semantic_model("never-seen"))

    def test_save_load_preserves_semantic_model(self):
        lm = self._memory()
        model = {"canonical_metrics": {"profit": "Profit"}}
        lm.set_semantic_model("fp456", model)
        lm.save()

        from utils.learning_memory import LearningMemory
        lm2 = LearningMemory(lm._path)  # noqa: SLF001
        lm2.load()
        self.assertEqual(lm2.get_semantic_model("fp456"), model)

    def test_empty_model_is_not_stored(self):
        lm = self._memory()
        lm.set_semantic_model("fp789", {})
        self.assertIsNone(lm.get_semantic_model("fp789"))


class TestValidateCachedSemanticModel(unittest.TestCase):
    """Part 2 robustness fix: a schema fingerprint is (name, dataType) only,
    so two genuinely different datasets that happen to share a column-name/
    type signature (e.g. a project name reused for unrelated data) collide
    on the same fingerprint. A cached model must be re-validated against the
    CURRENT data before being trusted, not reused blindly."""

    def test_valid_cache_against_its_own_data(self):
        from utils.semantic_model import discover_semantic_relationships, validate_cached_semantic_model

        df = _financial_dataframe()
        cols = [{"name": n} for n in
                ("Manufacturing Price", "Sale Price", "Gross Sales", "Discounts", "Sales", "COGS", "Profit")]
        model = discover_semantic_relationships(df, cols)
        self.assertTrue(validate_cached_semantic_model(model, df))

    def test_rejects_fingerprint_collision_with_unrelated_data(self):
        """The exact scenario Part 2 was asked to stress-test: identical
        schema (same column names/types -> same fingerprint), completely
        different real relationships (inventory stock levels relabeled with
        financial column names)."""
        from utils.semantic_model import discover_semantic_relationships, validate_cached_semantic_model

        financial_df = _financial_dataframe()
        cols = [{"name": n} for n in ("Gross Sales", "Discounts", "Sales", "COGS", "Profit")]
        financial_model = discover_semantic_relationships(financial_df, cols)
        self.assertIn("profit", financial_model["canonical_metrics"])

        n = len(financial_df)
        reserved = [float(i + 1) for i in range(n)]        # arbitrary, unrelated series
        available = [float(n - i) for i in range(n)]
        unrelated_df = pd.DataFrame({
            "Gross Sales": [r + a for r, a in zip(reserved, available)],  # relabeled "Total Stock"
            "Discounts": reserved,   # relabeled "Reserved" -- coincidentally additive with "Sales"
            "Sales": available,      # relabeled "Available"
            "COGS": [1.0] * n,        # unrelated noise, breaks the second chain link
            "Profit": [2.0] * n,
        })
        self.assertFalse(validate_cached_semantic_model(financial_model, unrelated_df))

    def test_empty_model_is_always_valid(self):
        from utils.semantic_model import validate_cached_semantic_model

        df = _financial_dataframe()
        self.assertTrue(validate_cached_semantic_model({"relationships": []}, df))

    def test_none_model_is_invalid(self):
        from utils.semantic_model import validate_cached_semantic_model

        df = _financial_dataframe()
        self.assertFalse(validate_cached_semantic_model(None, df))

    def test_missing_columns_in_current_data_is_invalid(self):
        from utils.semantic_model import validate_cached_semantic_model

        model = {"relationships": [
            {"type": "sum_decomposition", "whole": "A", "parts": ["B", "C"], "match_fraction": 1.0},
        ]}
        df = pd.DataFrame({"X": [1, 2, 3], "Y": [4, 5, 6]})  # none of A/B/C present
        self.assertFalse(validate_cached_semantic_model(model, df))


if __name__ == "__main__":
    unittest.main()

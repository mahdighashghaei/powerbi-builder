"""Regression tests for identifier-injection / escaping fixes in the file
generation layer (code-review 2026-07-03).

These verify the three bugs found in the production file-generation layer:

  1. TMDL template ``column 'Name'`` did not escape single quotes — a column
     named ``A'mount`` produced ``column 'A'mount'`` (broken-out quoting).
  2. ``_build_m_partition`` did not escape double quotes in M string literals —
     a column named ``A"x`` produced ``{{"A"x", ...}}`` (broken M parser).
  3. ``write_tmdl_measures`` escaped measure names with backslash (``\\'``)
     instead of the DAX/TMDL rule of doubling quotes (``''``), which left names
     unescaped AND broke ``_strip_existing_measures`` dedup (duplicate measures
     on re-run).

Stdlib unittest — runs under ``python -m pytest tests/ -v``.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_server.server import PbipToolbox, _strip_existing_measures  # noqa: E402


def _write_table(out: Path, **overrides) -> Path:
    """Write a minimal table TMDL and return its path."""
    tb = PbipToolbox(str(out))
    table_def = {"name": "T", "columns": [{"name": "A", "dataType": "double"}]}
    table_def.update(overrides)
    res = tb.write_tmdl_table(str(out), table_def)
    assert res.ok, res.errors
    return out / "tables" / "T.tmdl"


class TestTmdlColumnEscaping(unittest.TestCase):
    """Fix #1: column names with single quotes are properly escaped in TMDL."""

    def test_column_name_with_single_quote_is_doubled(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            tb = PbipToolbox(str(out))
            res = tb.write_tmdl_table(str(out), {
                "name": "T",
                "columns": [{"name": "A'mount", "dataType": "double"}],
            })
            self.assertTrue(res.ok, res.errors)
            tmdl = (out / "tables" / "T.tmdl").read_text(encoding="utf-8")
            # The column line must use doubled quotes: column 'A''mount'
            self.assertIn("column 'A''mount'", tmdl)
            # It must NOT contain the unescaped break-out form.
            self.assertNotIn("column 'A'mount'", tmdl)

    def test_source_column_with_single_quote_is_escaped(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            tb = PbipToolbox(str(out))
            res = tb.write_tmdl_table(str(out), {
                "name": "T",
                "columns": [{"name": "Bob's Amount", "dataType": "double",
                             "sourceColumn": "Bob's Amount"}],
            })
            self.assertTrue(res.ok, res.errors)
            tmdl = (out / "tables" / "T.tmdl").read_text(encoding="utf-8")
            self.assertIn("sourceColumn: Bob''s Amount", tmdl)
            self.assertNotIn("sourceColumn: Bob's Amount", tmdl)

    def test_normal_column_name_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            _write_table(out)
            tmdl = (out / "tables" / "T.tmdl").read_text(encoding="utf-8")
            self.assertIn("column 'A'", tmdl)


class TestMPartitionEscaping(unittest.TestCase):
    """Fix #2: double quotes in column names are escaped in M string literals."""

    def test_double_quote_in_column_name_doubled_in_m(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            tb = PbipToolbox(str(out))
            # sql source forces a partition to be built.
            res = tb.write_tmdl_table(str(out), {
                "name": "T",
                "columns": [{"name": 'A"x', "dataType": "int64"}],
                "source_type": "sql",
                "connection_params": {"server": "s", "database": "d", "table": "t"},
            })
            self.assertTrue(res.ok, res.errors)
            tmdl = (out / "tables" / "T.tmdl").read_text(encoding="utf-8")
            # The M literal must contain the doubled quote: {"A""x", ...}
            self.assertIn('{"A""x", Int64.Type}', tmdl)
            # It must NOT contain the broken form: {"A"x", ...}
            self.assertNotIn('{"A"x",', tmdl)

    def test_normal_column_name_in_m_partition(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            tb = PbipToolbox(str(out))
            res = tb.write_tmdl_table(str(out), {
                "name": "T",
                "columns": [{"name": "Amount", "dataType": "int64"}],
                "source_type": "sql",
                "connection_params": {"server": "s", "database": "d", "table": "t"},
            })
            self.assertTrue(res.ok)
            tmdl = (out / "tables" / "T.tmdl").read_text(encoding="utf-8")
            self.assertIn('{"Amount", Int64.Type}', tmdl)


class TestMeasureEscaping(unittest.TestCase):
    """Fix #3: measure names use quote-doubling, not backslash."""

    def test_measure_name_with_single_quote_is_doubled(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            _write_table(out)
            tb = PbipToolbox(str(out))
            res = tb.write_tmdl_measures(str(out), [
                {"name": "Bob's Measure", "expression": "SUM(T[A])", "table": "T"},
            ])
            self.assertTrue(res.ok, res.errors)
            tmdl = (out / "tables" / "T.tmdl").read_text(encoding="utf-8")
            self.assertIn("measure 'Bob''s Measure'", tmdl)
            # The backslash-escaped form must NOT appear.
            self.assertNotIn("measure 'Bob\\'s Measure'", tmdl)

    def test_measure_dedup_with_single_quote_name(self):
        """Re-writing a measure with a quoted name replaces, not duplicates."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            _write_table(out)
            tb = PbipToolbox(str(out))
            tb.write_tmdl_measures(str(out), [
                {"name": "Bob's Measure", "expression": "SUM(T[A])", "table": "T"},
            ])
            tb.write_tmdl_measures(str(out), [
                {"name": "Bob's Measure", "expression": "SUM(T[A])", "table": "T"},
            ])
            tmdl = (out / "tables" / "T.tmdl").read_text(encoding="utf-8")
            self.assertEqual(tmdl.count("Bob''s Measure"), 1,
                             "duplicate measure not deduped")

    def test_measure_dedup_normal_name(self):
        """Re-writing a normal measure still dedups (no regression)."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            _write_table(out)
            tb = PbipToolbox(str(out))
            tb.write_tmdl_measures(str(out), [
                {"name": "Total", "expression": "SUM(T[A])", "table": "T"},
            ])
            tb.write_tmdl_measures(str(out), [
                {"name": "Total", "expression": "SUM(T[A])", "table": "T"},
            ])
            tmdl = (out / "tables" / "T.tmdl").read_text(encoding="utf-8")
            self.assertEqual(tmdl.count("measure 'Total'"), 1)

    def test_calc_group_name_uses_quote_doubling(self):
        """Calculation group + item names use quote-doubling (not backslash)."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            tb = PbipToolbox(str(out))
            res = tb.write_tmdl_calc_group(str(out), {
                "name": "Time's Group",
                "items": [{"name": "YoY's", "expression": "1"}],
            })
            self.assertTrue(res.ok, res.errors)
            tmdl = next(out.rglob("*.tmdl")).read_text(encoding="utf-8")
            self.assertIn("table 'Time''s Group'", tmdl)
            self.assertIn("calculationItem 'YoY''s'", tmdl)


class TestStripExistingMeasures(unittest.TestCase):
    """The dedup helper matches the doubled-quote escape rule."""

    def test_strips_quoted_name_measure(self):
        tmdl = (
            "table T\n"
            "\tmeasure 'Bob''s Measure' = SUM(T[A])\n"
            "\t\tformatString: 0.00\n"
            "\tannotation PBI_ResultType = Table\n"
        )
        out = _strip_existing_measures(tmdl, {"Bob's Measure"})
        self.assertNotIn("Bob", out)

    def test_leaves_unrelated_measures(self):
        tmdl = (
            "table T\n"
            "\tmeasure 'Total' = SUM(T[A])\n"
            "\tmeasure 'Avg''s' = DIVIDE([Total],2)\n"
            "\tannotation PBI_ResultType = Table\n"
        )
        out = _strip_existing_measures(tmdl, {"Total"})
        # The 'Total' measure block (header line) is gone...
        self.assertNotIn("measure 'Total'", out)
        # ...but the unrelated quoted measure survives.
        self.assertIn("measure 'Avg''s'", out)


if __name__ == "__main__":
    unittest.main()

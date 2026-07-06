"""pytest configuration + shared fixtures.

This conftest keeps the shared ``tmp_workdir`` / ``sales_csv`` fixtures used by
the BDD scenarios in ``tests/test_bdd_build.py`` and ensures the project root
is importable. The existing stdlib ``unittest`` suites are unaffected — pytest
auto-collects them as before.

The BDD layer is optional: if ``pytest_bdd`` is not installed, the BDD test
module is a no-op (fail-safe) so the project still installs and tests without
the BDD dependency.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SALES_CSV = (
    "OrderDate,Region,Product,Quantity,Amount\n"
    "2024-01-05,North,Widget,10,250.50\n"
    "2024-01-07,South,Gadget,5,99.99\n"
    "2024-02-10,East,Widget,8,200.00\n"
    "2024-02-15,West,Gadget,12,239.88\n"
)


@pytest.fixture
def tmp_workdir():
    """A fresh temporary working directory for a single scenario."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def sales_csv(tmp_workdir):
    """A CSV file with sales data written into the temp workdir."""
    p = tmp_workdir / "sample.csv"
    p.write_text(SALES_CSV, encoding="utf-8")
    return p

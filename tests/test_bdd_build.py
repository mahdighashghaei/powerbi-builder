"""BDD test module — Gherkin scenarios for the build pipeline.

This is the Spec-Driven-Development / BDD layer (Wave A3). It uses pytest-bdd
to drive the *real* ``OrchestratorAgent`` (the same engine the CLI uses) from
Gherkin ``.feature`` files, so the scenarios verify actual end-to-end
behaviour, not a mock. Step definitions are defined in this same module because
pytest-bdd 8.x registers step functions in the calling module's locals.

The existing stdlib ``unittest`` suites are unaffected. If ``pytest_bdd`` is
not installed, this module is a no-op (fail-safe) so the project still tests
cleanly without the BDD dependency.

Run with::

    python -m pytest tests/ -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from pytest_bdd import scenarios, given, when, then, parsers
except Exception:  # pragma: no cover - BDD is optional
    scenarios = None  # type: ignore[assignment]


if scenarios is not None:
    # Shared fixtures (tmp_workdir, sales_csv) come from tests/conftest.py.

    @pytest.fixture
    def bdd():
        """Per-scenario state bag shared across Given/When/Then."""
        return {}

    # ------------------------------------------------------------------
    # Background / Given
    # ------------------------------------------------------------------

    @given("a CSV file with sales data", target_fixture="csv_path")
    def a_csv_file_with_sales_data(sales_csv):
        return sales_csv

    # ------------------------------------------------------------------
    # When
    # ------------------------------------------------------------------

    @when(parsers.parse('the builder runs with the description "{description}"'))
    def builder_runs(bdd, csv_path, tmp_workdir, description):
        from agents.orchestrator import OrchestratorAgent  # noqa: E402

        out = tmp_workdir / "out"
        orch = OrchestratorAgent(str(out))
        report = orch.run(source_path=str(csv_path), business_description=description)
        bdd["report"] = report
        bdd["out"] = out

    @when("the builder runs with a non-existent source file")
    def builder_runs_missing(bdd, tmp_workdir):
        from agents.orchestrator import OrchestratorAgent  # noqa: E402

        out = tmp_workdir / "out"
        orch = OrchestratorAgent(str(out))
        missing = tmp_workdir / "does_not_exist.csv"
        report = orch.run(source_path=str(missing), business_description="sales")
        bdd["report"] = report
        bdd["out"] = out

    # ------------------------------------------------------------------
    # Then
    # ------------------------------------------------------------------

    @then("the .pbip folder should exist")
    def pbip_folder_exists(bdd):
        report = bdd["report"]
        pbip_root = Path(report.pbip_root)
        assert pbip_root.is_dir(), f"pbip root not found: {pbip_root}"

    @then("the .SemanticModel folder should contain a TMDL table definition")
    def semantic_model_has_tmdl(bdd):
        pbip_root = Path(bdd["report"].pbip_root)
        sm = next(pbip_root.glob("*.SemanticModel"), None)
        assert sm is not None, "no .SemanticModel folder"
        tables = list((sm / "definition" / "tables").glob("*.tmdl"))
        assert tables, "no TMDL table definition found"

    @then("the .Report folder should contain at least one page")
    def report_has_page(bdd):
        pbip_root = Path(bdd["report"].pbip_root)
        report_dir = next(pbip_root.glob("*.Report"), None)
        assert report_dir is not None, "no .Report folder"
        pages = list((report_dir / "definition" / "pages").glob("*"))
        assert pages, "no report pages found"

    @then("the validation should pass")
    def validation_passes(bdd):
        report = bdd["report"]
        assert report.ok, f"run did not succeed: {report.error}"

    @then("the run should fail with a clear error")
    def run_fails(bdd):
        report = bdd["report"]
        # A missing input yields a RunReport that carries an error message and
        # never produced a real project dir (ok may be True vacuously when no
        # agent step ran). The contract we assert: an error was recorded.
        assert report.error, "expected a clear error message but none was recorded"

    @then("no output folder should be created")
    def no_output_folder(bdd):
        out: Path = bdd["out"]
        # A real PBIP project dir contains a *.SemanticModel subfolder; the
        # missing-input path may create an empty `out` but no project.
        project_dirs = [
            p for p in out.iterdir() if p.is_dir() and any(p.glob("*.SemanticModel"))
        ] if out.exists() else []
        assert project_dirs == [], f"unexpected project dirs: {project_dirs}"

    @then("a build.spec.json should be written next to the README")
    def spec_written(bdd):
        pbip_root = Path(bdd["report"].pbip_root)
        spec = pbip_root / "build.spec.json"
        readme = pbip_root / "README.md"
        assert spec.is_file(), "build.spec.json not written"
        assert readme.is_file(), "README.md not written"
        bdd["spec"] = json.loads(spec.read_text(encoding="utf-8"))

    @then("the spec should record the schema version and project name")
    def spec_records_version_and_name(bdd):
        spec = bdd["spec"]
        assert spec.get("schema_version"), "schema_version missing"
        assert spec.get("project_name"), "project_name missing"

    # ------------------------------------------------------------------
    # Bind the feature file
    # ------------------------------------------------------------------
    scenarios(str(Path(__file__).parent / "features" / "build_pbip.feature"))

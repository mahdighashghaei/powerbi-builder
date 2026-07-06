"""Tests for the versioned BuildSpec artifact (Wave A2 — Spec-Driven Dev).

Verifies:
  * BuildSpec is a valid Pydantic model with schema_version + required fields.
  * The orchestrator writes ``build.spec.json`` next to README.md on a run.
  * The spec records source, schema, measures, pages, validation, trajectory.
  * BuildSpec round-trips through model_dump / model_validate.
  * Fail-safe: a spec write error never fails the run.

Stdlib unittest — runs under ``python -m pytest tests/ -v``.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.schemas import BuildSpec  # noqa: E402


class TestBuildSpecModel(unittest.TestCase):
    """BuildSpec Pydantic model basics."""

    def test_minimal_build_spec_validates(self):
        spec = BuildSpec(project_name="SalesDashboard")
        self.assertEqual(spec.schema_version, "1.0")
        self.assertEqual(spec.project_name, "SalesDashboard")
        self.assertFalse(spec.ok)
        self.assertEqual(spec.measures, [])
        self.assertEqual(spec.pages, [])
        self.assertEqual(spec.trajectory, [])
        self.assertEqual(spec.builder_version, "powerbi-builder")

    def test_round_trip(self):
        spec = BuildSpec(
            project_name="P",
            source={"path": "/x.csv", "input_mode": "create"},
            schema={"table_name": "t", "columns": []},
            measures=[{"name": "Total", "displayFolder": "Rev", "formatString": "$#,##0"}],
            pages=[{"displayName": "Summary", "visuals": []}],
            validation={"ok": True, "tables": 1},
            trajectory=[{"agent": "SchemaAgent", "ok": True, "message": "done", "errors": []}],
            business_description="sales by region",
            ok=True,
        )
        dumped = spec.model_dump()
        self.assertEqual(dumped["project_name"], "P")
        self.assertEqual(len(dumped["measures"]), 1)
        # Round-trip: dict -> model -> dict is stable.
        spec2 = BuildSpec.model_validate(dumped)
        self.assertEqual(spec2.model_dump(), dumped)

    def test_schema_version_is_versioned(self):
        # The version field exists and is a string so future readers can migrate.
        self.assertIsInstance(BuildSpec(project_name="x").schema_version, str)


class TestOrchestratorWritesSpec(unittest.TestCase):
    """The orchestrator writes build.spec.json on a real (offline) run."""

    def _write_csv(self, path: Path) -> None:
        path.write_text(
            "OrderDate,Region,Product,Quantity,Amount\n"
            "2024-01-05,North,Widget,10,250.50\n"
            "2024-01-07,South,Gadget,5,99.99\n",
            encoding="utf-8",
        )

    def test_run_produces_build_spec_json(self):
        from agents.orchestrator import OrchestratorAgent  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            csv = td_path / "sample.csv"
            self._write_csv(csv)
            out = td_path / "out"
            orch = OrchestratorAgent(str(out))
            report = orch.run(
                source_path=str(csv),
                business_description="Monthly sales by region",
            )
            # Locate the project dir (output/<ProjectName>/).
            projects = [p for p in out.iterdir() if p.is_dir() and (p / "build.spec.json").is_file()]
            self.assertTrue(projects, "build.spec.json was not written")
            spec_path = projects[0] / "build.spec.json"
            spec_data = json.loads(spec_path.read_text(encoding="utf-8"))

            self.assertEqual(spec_data["schema_version"], "1.0")
            self.assertEqual(spec_data["builder_version"], "powerbi-builder")
            self.assertIn("source", spec_data)
            self.assertIn("schema", spec_data)
            self.assertIsInstance(spec_data["measures"], list)
            self.assertIsInstance(spec_data["pages"], list)
            self.assertIsInstance(spec_data["trajectory"], list)
            # The trajectory records every agent step.
            agents = [t["agent"] for t in spec_data["trajectory"]]
            self.assertTrue(agents, "trajectory is empty")
            # README.md is also present (spec sits next to it).
            self.assertTrue((projects[0] / "README.md").is_file())
            # ok flag mirrors the report.
            self.assertEqual(spec_data["ok"], bool(report.ok))

    def test_fail_safe_spec_write_error_does_not_fail_run(self):
        """If build.spec.json cannot be written, the run still succeeds."""
        from agents.orchestrator import OrchestratorAgent  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            csv = td_path / "sample.csv"
            self._write_csv(csv)
            out = td_path / "out"
            orch = OrchestratorAgent(str(out))

            # The spec is written via atomic_write_text (a JSON string), so
            # patch that helper for the orchestrator module and fail only when
            # the target is build.spec.json. The run must still complete.
            import agents.orchestrator as orch_mod  # noqa: E402

            original_text = orch_mod.atomic_write_text
            calls = []

            def raising_write_text(path, data):
                calls.append(str(path))
                if str(path).endswith("build.spec.json"):
                    raise OSError("simulated spec write failure")
                return original_text(path, data)

            orch_mod.atomic_write_text = raising_write_text
            try:
                report = orch.run(
                    source_path=str(csv),
                    business_description="sales by region",
                )
            finally:
                orch_mod.atomic_write_text = original_text
            # The spec write was attempted (and failed) but the run completed.
            self.assertTrue(any(str(p).endswith("build.spec.json") for p in calls))
            # And the PBIP files (README.md) were still written.
            projects = [p for p in out.iterdir() if p.is_dir()]
            self.assertTrue(projects)
            self.assertTrue(any((p / "README.md").is_file() for p in projects))


if __name__ == "__main__":
    unittest.main()

"""Tests for OpenTelemetry trajectory evaluation (Wave A4).

Verifies:
  * spans are recorded (in-memory) when telemetry is enabled.
  * spans are no-ops when telemetry is disabled (fail-safe, no overhead).
  * the trajectory can be retrieved per-run via get_trajectory.
  * span attributes, events, and exception recording work.
  * the get_trajectory / list_trajectory_runs ADK tools return the envelope.
  * the plugin records tool spans on callback (when enabled).
  * file exporter writes a JSONL trajectory line per span.

Stdlib unittest — runs under ``python -m pytest tests/ -v``.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adk import telemetry  # noqa: E402


class TestSpanRecording(unittest.TestCase):
    """Spans are recorded when enabled; no-op when disabled."""

    def setUp(self):
        telemetry.reset_for_tests()

    def tearDown(self):
        telemetry.reset_for_tests()
        os.environ.pop("POWERBI_OTEL_ENABLED", None)

    def test_disabled_telemetry_is_noop(self):
        os.environ["POWERBI_OTEL_ENABLED"] = "0"
        with telemetry.span("tool.x") as s:
            s.set_attribute("a", 1)
        # No run recorded.
        self.assertEqual(telemetry.list_runs(), [])
        traj = telemetry.get_trajectory()
        self.assertEqual(traj["count"], 0)

    def test_enabled_span_is_recorded(self):
        os.environ["POWERBI_OTEL_ENABLED"] = "1"
        with telemetry.span("tool.write", attributes={"project": "P"}) as s:
            s.set_attribute("rows", 42)
        runs = telemetry.list_runs()
        self.assertEqual(len(runs), 1)
        traj = telemetry.get_trajectory(runs[0])
        self.assertEqual(traj["count"], 1)
        span = traj["spans"][0]
        self.assertEqual(span["name"], "tool.write")
        self.assertEqual(span["kind"], "tool")
        self.assertEqual(span["attributes"]["project"], "P")
        self.assertEqual(span["attributes"]["rows"], 42)
        self.assertEqual(span["status"], "ok")
        self.assertIsNotNone(span["duration_ms"])

    def test_exception_marks_span_error_and_reraises(self):
        os.environ["POWERBI_OTEL_ENABLED"] = "1"
        with self.assertRaises(ValueError):
            with telemetry.span("tool.bad") as s:
                raise ValueError("boom")
        runs = telemetry.list_runs()
        traj = telemetry.get_trajectory(runs[0])
        span = traj["spans"][0]
        self.assertEqual(span["status"], "error")
        self.assertTrue(span["events"])
        self.assertEqual(span["events"][0]["name"], "exception")

    def test_run_trace_id_groups_spans(self):
        os.environ["POWERBI_OTEL_ENABLED"] = "1"
        rid = telemetry.start_run()
        with telemetry.span("tool.a", trace_id=rid):
            pass
        with telemetry.span("tool.b", trace_id=rid):
            pass
        traj = telemetry.get_trajectory(rid)
        self.assertEqual(traj["count"], 2)
        self.assertEqual({s["name"] for s in traj["spans"]}, {"tool.a", "tool.b"})
        # All spans share the trace id.
        for s in traj["spans"]:
            self.assertEqual(s["trace_id"], rid)


class TestFileExporter(unittest.TestCase):
    """The file exporter writes one JSONL line per span."""

    def setUp(self):
        telemetry.reset_for_tests()

    def tearDown(self):
        telemetry.reset_for_tests()
        for v in ("POWERBI_OTEL_ENABLED", "POWERBI_OTEL_EXPORTER", "POWERBI_OTEL_FILE"):
            os.environ.pop(v, None)

    def test_file_exporter_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            traj_file = Path(td) / "traj.jsonl"
            os.environ["POWERBI_OTEL_ENABLED"] = "1"
            os.environ["POWERBI_OTEL_EXPORTER"] = "file"
            os.environ["POWERBI_OTEL_FILE"] = str(traj_file)
            with telemetry.span("tool.x", attributes={"k": "v"}):
                pass
            self.assertTrue(traj_file.is_file())
            lines = traj_file.read_text(encoding="utf-8").strip().split("\n")
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["name"], "tool.x")
            self.assertEqual(rec["attributes"]["k"], "v")


class TestTrajectoryTools(unittest.TestCase):
    """The ADK tools return the standard envelope."""

    def setUp(self):
        telemetry.reset_for_tests()
        os.environ["POWERBI_OTEL_ENABLED"] = "1"

    def tearDown(self):
        telemetry.reset_for_tests()
        os.environ.pop("POWERBI_OTEL_ENABLED", None)

    def test_get_trajectory_tool(self):
        from adk.tools.trajectory_tools import get_trajectory  # noqa: E402

        with telemetry.span("tool.x"):
            pass
        r = get_trajectory("")
        self.assertTrue(r["ok"])
        self.assertEqual(r["tool"], "get_trajectory")
        self.assertEqual(r["count"], 1)

    def test_list_trajectory_runs_tool(self):
        from adk.tools.trajectory_tools import list_trajectory_runs  # noqa: E402

        telemetry.start_run()
        with telemetry.span("tool.x"):
            pass
        r = list_trajectory_runs()
        self.assertTrue(r["ok"])
        self.assertEqual(r["tool"], "list_trajectory_runs")
        self.assertEqual(r["count"], 1)


class TestPluginSpans(unittest.TestCase):
    """The PowerBIBuilderPlugin records tool spans when telemetry is enabled."""

    def setUp(self):
        telemetry.reset_for_tests()
        # Ensure a clean env state per test (isolation from other test files).
        os.environ.pop("POWERBI_OTEL_ENABLED", None)

    def tearDown(self):
        telemetry.reset_for_tests()
        os.environ.pop("POWERBI_OTEL_ENABLED", None)

    def test_plugin_before_after_tool_records_span(self):
        os.environ["POWERBI_OTEL_ENABLED"] = "1"
        import asyncio  # noqa: E402

        from adk.plugin import PowerBIBuilderPlugin  # noqa: E402

        plugin = PowerBIBuilderPlugin()

        class _FakeTool:
            name = "write_tmdl"

        class _FakeCtx:
            pass

        tool = _FakeTool()
        ctx = _FakeCtx()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                plugin.before_tool_callback(
                    tool=tool, tool_args={"table": "sales"}, tool_context=ctx
                )
            )
            loop.run_until_complete(
                plugin.after_tool_callback(
                    tool=tool, tool_args={"table": "sales"}, tool_context=ctx,
                    result={"ok": True, "data": {}},
                )
            )
        finally:
            loop.close()
        runs = telemetry.list_runs()
        self.assertTrue(runs)
        traj = telemetry.get_trajectory(runs[0])
        self.assertGreaterEqual(traj["count"], 1)
        names = {s["name"] for s in traj["spans"]}
        self.assertIn("tool.write_tmdl", names)

    def test_plugin_no_overhead_when_disabled(self):
        os.environ["POWERBI_OTEL_ENABLED"] = "0"
        import asyncio  # noqa: E402

        from adk.plugin import PowerBIBuilderPlugin  # noqa: E402

        plugin = PowerBIBuilderPlugin()

        class _FakeTool:
            name = "x"

        class _FakeCtx:
            pass

        tool = _FakeTool()
        ctx = _FakeCtx()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx)
            )
            loop.run_until_complete(
                plugin.after_tool_callback(
                    tool=tool, tool_args={}, tool_context=ctx, result={"ok": True}
                )
            )
        finally:
            loop.close()
        # No trajectory recorded when disabled.
        self.assertEqual(telemetry.list_runs(), [])


if __name__ == "__main__":
    unittest.main()

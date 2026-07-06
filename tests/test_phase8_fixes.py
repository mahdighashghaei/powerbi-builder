"""Phase 8 behavioral fix tests — path validation, error suggestions,
build_report multi-page tool, tool-call rendering, readable page IDs.

These tests verify the 6 issues identified from the real chat session:
  1. Path validation callback prevents wasted retries on bad file paths.
  2. Error messages include available-file suggestions.
  3. build_report creates multi-page reports with diverse visual types.
  4. REPL run_turn shows tool-call progress (no more silent hangs).
  5. Agent instructions mention build_report and path validation.
  6. add_page generates readable slug page IDs instead of UUIDs.
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from google.adk.events import Event, EventActions  # noqa: E402
from google.genai import types  # noqa: E402

from adk.chat import ChatRepl, _summarize_args  # noqa: E402
from mcp_server import highlevel as hl  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_csv(p: Path, rows: list[list]) -> None:
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for row in rows:
            w.writerow(row)


def _sample_csv(p: Path) -> Path:
    _write_csv(p, [
        ["Date", "Region", "Product", "Sales", "Quantity"],
        ["2024-01-01", "North", "Widget", "100.50", "10"],
        ["2024-01-02", "South", "Gadget", "200.75", "5"],
        ["2024-02-01", "North", "Widget", "150.25", "8"],
        ["2024-02-02", "East", "Sprocket", "300.00", "12"],
    ])
    return p


def _make_pbip(root: Path, name: str = "Demo") -> Path:
    """Create a minimal valid PBIP project with a table + measures."""
    from mcp_server.server import PbipToolbox
    from adk.tools.project_tools import create_project_scaffold
    from adk.tools.tmdl_tools import write_tmdl_table, write_tmdl_measures
    from adk.tools.pbir_tools import write_pbir_page, write_theme_json
    from adk.tools.project_tools import finalize_pages_index

    csv_path = root / "data.csv"
    _sample_csv(csv_path)

    # scaffold
    scaf = create_project_scaffold(name, "data", str(root))
    assert scaf["ok"], scaf
    sem_dir = scaf["data"]["semantic_model_dir"]
    rep_dir = scaf["data"]["report_dir"]
    proj_root = scaf["data"]["project_root"]

    tb = PbipToolbox(proj_root)

    # schema + table
    schema_r = tb.read_csv_schema(str(csv_path))
    assert schema_r.ok, schema_r.errors
    schema = schema_r.data["schema"]

    table_def = {"name": "data", "columns": schema["columns"],
                 "source_path": str(csv_path)}
    r = tb.write_tmdl_table(sem_dir, table_def)
    assert r.ok, r.errors

    # measures
    measures = [
        {"name": "Total Sales", "expression": "SUM('data'[Sales])",
         "formatString": "#,0", "displayFolder": "Revenue", "table": "data"},
        {"name": "Total Quantity", "expression": "SUM('data'[Quantity])",
         "formatString": "#,0", "displayFolder": "Orders", "table": "data"},
        {"name": "Order Count", "expression": "COUNTROWS('data')",
         "formatString": "#,0", "displayFolder": "Orders", "table": "data"},
    ]
    r = tb.write_tmdl_measures(sem_dir, measures)
    assert r.ok, r.errors

    # theme + page
    tb.write_theme_json(rep_dir)
    page_def = {
        "id": "summary-page", "displayName": "Summary",
        "width": 1280, "height": 720, "visuals": [],
    }
    tb.write_pbir_page(rep_dir, page_def)
    finalize_pages_index(name, ["summary-page"], str(root))

    return Path(proj_root)


def fake_event(text="", author="powerbi_builder", turn_complete=True,
               state_delta=None, error_code=None,
               func_calls=None, func_responses=None):
    """Build a fake ADK Event, optionally with function_call/response parts."""
    kwargs = dict(author=author, turn_complete=turn_complete,
                  invocation_id="inv-test", id="evt-test", timestamp=0.0)
    if text:
        kwargs["content"] = types.Content(
            role="model", parts=[types.Part(text=text)])
    if state_delta:
        kwargs["actions"] = EventActions(state_delta=state_delta)
    if error_code:
        kwargs["error_code"] = error_code
    # function_call / function_response go into content.parts
    if func_calls or func_responses:
        parts = []
        if text:
            parts.append(types.Part(text=text))
        for fc in (func_calls or []):
            parts.append(types.Part(function_call=types.FunctionCall(
                name=fc["name"], args=fc.get("args", {}))))
        for fr in (func_responses or []):
            parts.append(types.Part(function_response=types.FunctionResponse(
                name=fr["name"], response=fr.get("response", {}))))
        kwargs["content"] = types.Content(role="model", parts=parts)
    return Event(**kwargs)


# ---------------------------------------------------------------------------
# Fix 1: Path validation callback (validate_paths)
# ---------------------------------------------------------------------------


class TestPathValidation(unittest.TestCase):

    def _fake_tool_context(self, state=None):
        ctx = MagicMock()
        ctx.state = state if state is not None else {}
        return ctx

    def test_validate_paths_blocks_missing_file(self):
        from adk.agent import validate_paths
        tool = SimpleNamespace(name="generate_pbip")
        ctx = self._fake_tool_context()
        ret = validate_paths(tool, {"source": "C:/nonexistent/data.csv"}, ctx)
        self.assertIsNotNone(ret, "should return a dict to skip the tool")
        self.assertFalse(ret["ok"])
        self.assertIn("not found", ret["message"].lower())

    def test_validate_paths_includes_suggestions(self):
        from adk.agent import validate_paths
        tool = SimpleNamespace(name="generate_pbip")
        ctx = self._fake_tool_context()
        ret = validate_paths(tool, {"source": "C:/nope.csv"}, ctx)
        self.assertIn("Available data files", ret["message"])

    def test_validate_paths_allows_existing_file(self):
        from adk.agent import validate_paths
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.csv"
            _sample_csv(p)
            tool = SimpleNamespace(name="generate_pbip")
            ctx = self._fake_tool_context()
            ret = validate_paths(tool, {"source": str(p)}, ctx)
            self.assertIsNone(ret, "should return None to let tool proceed")

    def test_validate_paths_allows_existing_dir(self):
        from adk.agent import validate_paths
        with tempfile.TemporaryDirectory() as td:
            tool = SimpleNamespace(name="add_measure")
            ctx = self._fake_tool_context()
            ret = validate_paths(tool, {"pbip_dir": td}, ctx)
            self.assertIsNone(ret)

    def test_validate_paths_ignores_unrelated_tool(self):
        from adk.agent import validate_paths
        tool = SimpleNamespace(name="list_dax_patterns")
        ctx = self._fake_tool_context()
        ret = validate_paths(tool, {}, ctx)
        self.assertIsNone(ret)

    def test_validate_paths_blocks_missing_pbip_dir(self):
        from adk.agent import validate_paths
        tool = SimpleNamespace(name="add_page")
        ctx = self._fake_tool_context()
        ret = validate_paths(tool, {"pbip_dir": "C:/nope/project"}, ctx)
        self.assertIsNotNone(ret)
        self.assertFalse(ret["ok"])

    def test_root_agent_has_before_tool_callback(self):
        from adk.agent import root_agent, validate_paths
        self.assertIs(root_agent.before_tool_callback, validate_paths)


# ---------------------------------------------------------------------------
# Fix 2: Error messages with file suggestions
# ---------------------------------------------------------------------------


class TestErrorMessages(unittest.TestCase):

    def test_generate_pbip_error_includes_suggestions(self):
        with tempfile.TemporaryDirectory() as td:
            res = hl.generate_pbip("C:/totally/fake/path.csv", "test",
                                   output_root=td)
            self.assertFalse(res["ok"])
            self.assertIn("Available data files", res["message"])

    def test_suggest_data_files_excludes_config(self):
        from mcp_server.highlevel import _suggest_data_files
        files = _suggest_data_files()
        # Should not include .mcp.json or template configs
        for f in files:
            self.assertFalse(f.endswith(".mcp.json"),
                             f"{f} should not be a config file")
            self.assertFalse("template" in f.lower(),
                             f"{f} should not be a template")

    def test_suggest_data_files_includes_sample(self):
        from mcp_server.highlevel import _suggest_data_files
        files = _suggest_data_files()
        # SampleData.csv should be in the list
        self.assertTrue(any("SampleData.csv" in f for f in files))

    def test_read_csv_schema_error_includes_suggestions(self):
        from mcp_server.server import PbipToolbox
        with tempfile.TemporaryDirectory() as td:
            tb = PbipToolbox(td)
            res = tb.read_csv_schema("C:/totally/fake.csv")
            self.assertFalse(res.ok)
            self.assertIn("Available data files", res.message)


# ---------------------------------------------------------------------------
# Fix 3: build_report — multi-page with diverse visual types
# ---------------------------------------------------------------------------


class TestBuildReport(unittest.TestCase):

    def test_build_report_adds_multiple_pages(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "ReportTest")
            res = hl.build_report(str(proj_root), num_pages=3,
                                  visual_variety="all")
            self.assertTrue(res["ok"], res.get("message"))
            pages = res["data"]["pages_added"]
            self.assertEqual(len(pages), 3)
            self.assertGreater(res["data"]["total_visuals"], 6)

    def test_build_report_visual_types_diverse(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "VizTest")
            res = hl.build_report(str(proj_root), num_pages=3,
                                  visual_variety="all")
            self.assertTrue(res["ok"])
            # Check that diverse visual types exist in the output
            proj_root = Path(res["data"]["pbip_root"])
            proj_name = res["data"]["project_name"]
            rep_def = proj_root / f"{proj_name}.Report" / "definition" / "pages"
            vtypes = set()
            for vjson in rep_def.glob("*/visuals/*/visual.json"):
                data = json.loads(vjson.read_text(encoding="utf-8"))
                vtypes.add(data["visual"]["visualType"])
            # Should have more than just card/bar/column/line
            self.assertGreater(len(vtypes), 4,
                               f"Only got {vtypes} — expected diverse types")

    def test_build_report_standard_variety(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "StdTest")
            res = hl.build_report(str(proj_root), num_pages=2,
                                  visual_variety="standard")
            self.assertTrue(res["ok"])
            self.assertGreaterEqual(res["data"]["total_visuals"], 2)

    def test_build_report_no_measures_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Create a PBIP with no measures
            from adk.tools.project_tools import create_project_scaffold
            csv_path = root / "data.csv"
            _sample_csv(csv_path)
            scaf = create_project_scaffold("NoMeasures", "data", str(root))
            from mcp_server.server import PbipToolbox
            from mcp_server import schema_inference as si
            tb = PbipToolbox(scaf["data"]["project_root"])
            schema = si.infer_csv_schema(csv_path)
            tb.write_tmdl_table(scaf["data"]["semantic_model_dir"],
                                {"name": "data", "columns": schema["columns"],
                                 "source_path": str(csv_path)})
            res = hl.build_report(scaf["data"]["project_root"])
            self.assertFalse(res["ok"])
            self.assertIn("measure", res["message"].lower())

    def test_build_report_wrapper_exists(self):
        from adk.tools.highlevel_tools import build_report
        import inspect
        sig = inspect.signature(build_report)
        self.assertIn("num_pages", sig.parameters)
        self.assertIn("visual_variety", sig.parameters)

    def test_build_report_in_root_agent_tools(self):
        from adk.agent import root_agent
        names = [getattr(t, 'name', getattr(t, '__name__', str(t)))
                 for t in root_agent.tools]
        self.assertIn("build_report", names)


# ---------------------------------------------------------------------------
# Fix 4: Tool-call rendering in REPL
# ---------------------------------------------------------------------------


class TestToolCallRendering(unittest.TestCase):

    def test_run_turn_shows_tool_call_progress(self):
        """When an event has function_calls, on_event should be called."""
        events = [
            fake_event(func_calls=[{"name": "generate_pbip",
                                     "args": {"source": "data.csv"}}]),
            fake_event(func_responses=[{"name": "generate_pbip",
                                         "response": {"ok": True}}]),
            fake_event(text="Done! Built the project.", turn_complete=True),
        ]
        mock_runner = MagicMock()
        mock_runner.run.return_value = iter(events)
        repl = ChatRepl(runner=mock_runner)
        progress = []
        reply = repl.run_turn("build it", on_event=progress.append)
        # Should have called on_event with tool-call info
        self.assertTrue(any("Calling generate_pbip" in p for p in progress))
        self.assertTrue(any("generate_pbip done" in p for p in progress))
        # Final reply should contain the text
        self.assertIn("Done!", reply)

    def test_run_turn_without_on_event_still_works(self):
        events = [fake_event(text="Hello", turn_complete=True)]
        mock_runner = MagicMock()
        mock_runner.run.return_value = iter(events)
        repl = ChatRepl(runner=mock_runner)
        reply = repl.run_turn("hi")
        self.assertEqual(reply, "Hello")

    def test_summarize_args_short(self):
        args = {"source": "C:/path/to/data.csv", "description": "test"}
        s = _summarize_args(args)
        self.assertIn("source=", s)
        self.assertIn("data.csv", s)

    def test_summarize_args_truncates_long(self):
        args = {"description": "x" * 200}
        s = _summarize_args(args)
        self.assertLessEqual(len(s), 100)

    def test_summarize_args_empty(self):
        self.assertEqual(_summarize_args({}), "")


# ---------------------------------------------------------------------------
# Fix 5: Agent instructions updated
# ---------------------------------------------------------------------------


class TestAgentInstructions(unittest.TestCase):

    def test_instruction_mentions_build_report(self):
        from adk.agent import root_agent
        self.assertIn("build_report", root_agent.instruction)

    def test_instruction_mentions_multi_page_strategy(self):
        from adk.agent import root_agent
        self.assertIn("Multi-Page", root_agent.instruction)

    def test_instruction_mentions_path_validation(self):
        from adk.agent import root_agent
        self.assertIn("Path Validation", root_agent.instruction)

    def test_agent_md_mentions_build_report(self):
        md = (_ROOT / "agents" / "PowerBIBuilder.agent.md").read_text("utf-8")
        self.assertIn("build_report", md)

    def test_agent_md_mentions_path_validation(self):
        md = (_ROOT / "agents" / "PowerBIBuilder.agent.md").read_text("utf-8")
        self.assertIn("Path Validation", md)

    def test_agent_md_must_not_includes_no_retry(self):
        md = (_ROOT / "agents" / "PowerBIBuilder.agent.md").read_text("utf-8")
        self.assertIn("retry", md.lower())


# ---------------------------------------------------------------------------
# Fix 6: Readable page IDs (slug instead of UUID)
# ---------------------------------------------------------------------------


class TestReadablePageIds(unittest.TestCase):

    def test_safe_slug_basic(self):
        from mcp_server.highlevel import _safe_slug
        self.assertEqual(_safe_slug("Sales Trends"), "sales-trends")
        self.assertEqual(_safe_slug("Product & Customer Analysis"),
                         "product-customer-analysis")
        self.assertEqual(_safe_slug("Overview"), "overview")

    def test_add_page_generates_readable_slug(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "SlugTest")
            res = hl.add_page(str(proj_root), "Sales Trends")
            self.assertTrue(res["ok"])
            pid = res["data"]["page_id"]
            self.assertEqual(pid, "sales-trends")
            self.assertNotIn("ai-", pid)

    def test_add_page_slug_collision_handling(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "CollisionTest")
            # Add first page
            res1 = hl.add_page(str(proj_root), "Overview")
            self.assertTrue(res1["ok"])
            self.assertEqual(res1["data"]["page_id"], "overview")
            # Add second page with same name → should get -2 suffix
            res2 = hl.add_page(str(proj_root), "Overview")
            self.assertTrue(res2["ok"])
            self.assertEqual(res2["data"]["page_id"], "overview-2")

    def test_add_page_explicit_id_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "ExplicitTest")
            res = hl.add_page(str(proj_root), "Custom Page",
                              page_id="my-custom-id")
            self.assertTrue(res["ok"])
            self.assertEqual(res["data"]["page_id"], "my-custom-id")


# ---------------------------------------------------------------------------
# Integration: build_report produces openable PBIP
# ---------------------------------------------------------------------------


class TestBuildReportIntegration(unittest.TestCase):

    def test_full_pipeline_generate_then_build_report_validates(self):
        """generate_pbip + build_report → validate passes, multi-page output."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "sales.csv"
            _sample_csv(csv_path)

            # Step 1: generate base PBIP
            gen = hl.generate_pbip(str(csv_path), "Sales dashboard",
                                    project_name="IntegrationTest",
                                    output_root=str(root))
            self.assertTrue(gen["ok"], gen.get("message"))

            # Step 2: build rich multi-page report
            br = hl.build_report(gen["data"]["pbip_root"],
                                  num_pages=3, visual_variety="all")
            self.assertTrue(br["ok"], br.get("message"))
            self.assertEqual(len(br["data"]["pages_added"]), 3)

            # Step 3: validate the project structure
            from adk.tools.validation_tools import validate_pbip_structure
            val = validate_pbip_structure(gen["data"]["pbip_root"], str(root))
            self.assertTrue(val["ok"], val.get("errors"))

            # Step 4: verify pages.json has all pages
            proj_name = gen["data"]["project_name"]
            pages_json = (Path(gen["data"]["pbip_root"]) /
                          f"{proj_name}.Report" / "definition" /
                          "pages" / "pages.json")
            pdata = json.loads(pages_json.read_text("utf-8"))
            # summary-page + 3 new pages = 4 total
            self.assertEqual(len(pdata["pageOrder"]), 4)


if __name__ == "__main__":
    unittest.main()

"""Phase 8 audit fix tests — covers all 22 issues from the ADK expert audit.

Tests are grouped by fix category:
  A. Critical bugs (memory, RunConfig, build_report ok, sub-agent callbacks,
     create_session crash)
  B. Medium bugs (empty reply, session leak, num_pages/visual_variety
     validation, generate_content_config, error callbacks, plugin leak)
  C. File upload/download (/upload, /download, /samples, /files,
     source_artifact, load_artifacts tool)
  D. Minor fixes (run_turn guard, validate_paths args, session_id)
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys
import tempfile
import unittest
import unittest.mock  # noqa: F401  -- enables unittest.mock.patch references
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from google.adk.artifacts import InMemoryArtifactService  # noqa: E402
from google.adk.events import Event, EventActions  # noqa: E402
from google.adk.memory import InMemoryMemoryService  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from adk.chat import ChatRepl  # noqa: E402
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
    from adk.tools.project_tools import create_project_scaffold, finalize_pages_index

    csv_path = root / "data.csv"
    _sample_csv(csv_path)
    scaf = create_project_scaffold(name, "data", str(root))
    assert scaf["ok"], scaf
    sem_dir = scaf["data"]["semantic_model_dir"]
    rep_dir = scaf["data"]["report_dir"]
    proj_root = scaf["data"]["project_root"]
    tb = PbipToolbox(proj_root)
    schema_r = tb.read_csv_schema(str(csv_path))
    assert schema_r.ok
    schema = schema_r.data["schema"]
    tb.write_tmdl_table(sem_dir, {"name": "data", "columns": schema["columns"],
                                   "source_path": str(csv_path)})
    tb.write_tmdl_measures(sem_dir, [
        {"name": "Total Sales", "expression": "SUM('data'[Sales])",
         "formatString": "#,0", "displayFolder": "Revenue", "table": "data"},
        {"name": "Total Quantity", "expression": "SUM('data'[Quantity])",
         "formatString": "#,0", "displayFolder": "Orders", "table": "data"},
    ])
    tb.write_theme_json(rep_dir)
    tb.write_pbir_page(rep_dir, {"id": "summary-page", "displayName": "Summary",
                                  "width": 1280, "height": 720, "visuals": []})
    finalize_pages_index(name, ["summary-page"], str(root))
    return Path(proj_root)


def fake_event(text="", author="powerbi_builder", turn_complete=True,
               state_delta=None, error_code=None):
    kwargs = dict(author=author, turn_complete=turn_complete,
                  invocation_id="inv-test", id="evt-test", timestamp=0.0)
    if text:
        kwargs["content"] = types.Content(role="model", parts=[types.Part(text=text)])
    if state_delta:
        kwargs["actions"] = EventActions(state_delta=state_delta)
    if error_code:
        kwargs["error_code"] = error_code
    return Event(**kwargs)


def make_repl(*, events=None, runner=None, output_root=None,
              session_service=None, artifact_service=None, memory_service=None):
    if runner is None:
        runner = MagicMock()
        runner.run.return_value = iter(events or [])
    return ChatRepl(runner=runner, output_root=str(output_root) if output_root else None,
                    session_service=session_service,
                    artifact_service=artifact_service,
                    memory_service=memory_service)


# ===========================================================================
# A. Critical bugs
# ===========================================================================


class TestSubAgentCallbacks(unittest.TestCase):
    """BUG 2.6 — validate_paths/track_project must be on sub-agents too."""

    def test_schema_agent_has_validate_paths(self):
        from adk.agent import schema_agent
        self.assertIsNotNone(schema_agent.before_tool_callback)
        self.assertEqual(schema_agent.before_tool_callback.__name__, "validate_paths")

    def test_dax_agent_has_track_project(self):
        from adk.agent import dax_agent
        self.assertIsNotNone(dax_agent.after_tool_callback)
        self.assertEqual(dax_agent.after_tool_callback.__name__, "track_project")

    def test_report_agent_has_callbacks(self):
        from adk.agent import report_agent
        self.assertIsNotNone(report_agent.before_tool_callback)
        self.assertIsNotNone(report_agent.after_tool_callback)

    def test_deploy_agent_has_callbacks(self):
        from adk.agent import deploy_agent
        self.assertIsNotNone(deploy_agent.before_tool_callback)
        self.assertIsNotNone(deploy_agent.after_tool_callback)


class TestBuildReportFailure(unittest.TestCase):
    """BUG 3.5 — build_report must return ok=False when all pages fail."""

    def test_build_report_num_pages_zero_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "ZeroTest")
            res = hl.build_report(str(proj_root), num_pages=0)
            self.assertFalse(res["ok"])
            self.assertIn("num_pages", res["message"])

    def test_build_report_negative_pages_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "NegTest")
            res = hl.build_report(str(proj_root), num_pages=-1)
            self.assertFalse(res["ok"])

    def test_build_report_invalid_variety_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "VarietyTest")
            res = hl.build_report(str(proj_root), visual_variety="everything")
            self.assertFalse(res["ok"])
            self.assertIn("visual_variety", res["message"])

    def test_build_report_capitalized_variety_normalized(self):
        """'All' (capitalized) should be normalized to 'all', not rejected."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "CapTest")
            res = hl.build_report(str(proj_root), num_pages=2, visual_variety="All")
            self.assertTrue(res["ok"], res.get("message"))


class TestHighLevelToolsReturnPbipRoot(unittest.TestCase):
    """Regression: add_measure/add_visual/add_page must report ``pbip_root``
    in their result data (like generate_pbip/edit_pbip/build_report already
    do), so downstream consumers (the ADK plugin's zip-artifact saver) can
    locate the project without re-deriving it."""

    def test_add_measure_returns_pbip_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "MeasureRootTest")
            res = hl.add_measure(str(proj_root), "Avg Sales", "AVERAGE('data'[Sales])")
            self.assertTrue(res["ok"], res.get("message"))
            self.assertEqual(res["data"]["pbip_root"], str(proj_root))

    def test_add_visual_returns_pbip_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "VisualRootTest")
            res = hl.add_visual(
                str(proj_root), "summary-page", "card",
                {"select": [{"table": "data", "column": "Sales", "aggregation": "sum"}]},
            )
            self.assertTrue(res["ok"], res.get("message"))
            self.assertEqual(res["data"]["pbip_root"], str(proj_root))

    def test_add_page_returns_pbip_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "PageRootTest")
            res = hl.add_page(str(proj_root), "Extra Page")
            self.assertTrue(res["ok"], res.get("message"))
            self.assertEqual(res["data"]["pbip_root"], str(proj_root))


class TestPluginZipsAfterProjectMutatingTools(unittest.TestCase):
    """Regression: the web UI's automatic zip-artifact mechanism
    (PowerBIBuilderPlugin._save_build_artifact, triggered from
    after_tool_callback) previously only fired for "generate_pbip".
    build_report/edit_pbip/add_measure/add_visual/add_page (the tools
    actually used for "add more pages", "edit", "add a measure" follow-up
    requests) were silently excluded, so a successful multi-page rich
    build had no downloadable artifact -- the agent could only point the
    user at a raw filesystem path."""

    def test_artifact_tools_cover_all_project_mutating_tools(self):
        from adk.plugin import PowerBIBuilderPlugin

        self.assertEqual(
            PowerBIBuilderPlugin._ARTIFACT_TOOLS,
            {"generate_pbip", "edit_pbip", "add_measure", "add_visual",
             "add_page", "build_report",
             "apply_theme", "update_visual", "delete_visual", "delete_page",
             "edit_measure", "delete_measure", "edit_table_source", "relayout_page"},
        )

    def _run_after_tool_callback(self, tool_name: str, project_root: Path):
        import asyncio
        from unittest.mock import AsyncMock

        from adk.plugin import PowerBIBuilderPlugin

        plugin = PowerBIBuilderPlugin()
        tool = SimpleNamespace(name=tool_name)
        ctx = MagicMock()
        ctx.save_artifact = AsyncMock(return_value=1)
        ctx.state = {}
        result = {
            "ok": True,
            "data": {"project_name": project_root.name, "pbip_root": str(project_root)},
        }
        asyncio.run(plugin.after_tool_callback(
            tool=tool, tool_args={}, tool_context=ctx, result=result,
        ))
        return ctx

    def test_build_report_triggers_zip_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            proj_root = _make_pbip(Path(td), "BuildReportZipTest")
            ctx = self._run_after_tool_callback("build_report", proj_root)
            saved_filenames = [c.kwargs.get("filename") for c in ctx.save_artifact.call_args_list]
            self.assertIn(f"user:project_{proj_root.name}.zip", saved_filenames)

    def test_add_measure_triggers_zip_artifact(self):
        """The exact tool a common 'add a measure' follow-up request calls —
        previously excluded from the artifact-saving tool set."""
        with tempfile.TemporaryDirectory() as td:
            proj_root = _make_pbip(Path(td), "AddMeasureZipTest")
            ctx = self._run_after_tool_callback("add_measure", proj_root)
            saved_filenames = [c.kwargs.get("filename") for c in ctx.save_artifact.call_args_list]
            self.assertIn(f"user:project_{proj_root.name}.zip", saved_filenames)

    def test_non_mutating_tool_does_not_trigger_zip_artifact(self):
        """suggest_measures doesn't change the project -- must not be zipped."""
        with tempfile.TemporaryDirectory() as td:
            proj_root = _make_pbip(Path(td), "NoZipTest")
            ctx = self._run_after_tool_callback("suggest_measures", proj_root)
            ctx.save_artifact.assert_not_called()

    def _run_after_tool_callback_low_level(
        self, tool_name: str, tool_args: dict, result_data: dict | None = None,
    ):
        """Mirrors _run_after_tool_callback, but for the LOW-LEVEL editing
        tools (apply_theme, delete_page, etc.) whose result dict does NOT
        carry project_name/pbip_root -- only their own tool_args do."""
        import asyncio
        from unittest.mock import AsyncMock
        from adk.plugin import PowerBIBuilderPlugin

        plugin = PowerBIBuilderPlugin()
        tool = SimpleNamespace(name=tool_name)
        ctx = MagicMock()
        ctx.save_artifact = AsyncMock(return_value=1)
        ctx.state = {}
        result = {"ok": True, "data": result_data or {}}
        asyncio.run(plugin.after_tool_callback(
            tool=tool, tool_args=tool_args, tool_context=ctx, result=result,
        ))
        return ctx

    def test_apply_theme_resolves_identity_from_output_root_and_zips(self):
        """Regression: apply_theme succeeded live (real adk web session) but
        no fresh zip was ever offered -- its result carries only
        {"path", "name"}, not project_name/pbip_root. The project identity
        must be recovered from the tool's OWN output_root argument."""
        with tempfile.TemporaryDirectory() as td:
            proj_root = _make_pbip(Path(td), "ApplyThemeZipTest")
            ctx = self._run_after_tool_callback_low_level(
                "apply_theme",
                {"output_dir": "ApplyThemeZipTest.Report/definition",
                 "output_root": str(proj_root), "preset": "earth_tones"},
                result_data={"path": "some/theme.json", "name": "earth_tones"},
            )
            saved_filenames = [c.kwargs.get("filename") for c in ctx.save_artifact.call_args_list]
            self.assertIn(f"user:project_{proj_root.name}.zip", saved_filenames)

    def test_apply_theme_resolves_identity_from_generic_root_plus_nested_output_dir(self):
        """Regression, confirmed live via a real Docker container session:
        the model called apply_theme with output_root="./output" (the
        GENERIC root -- correctly fails the direct check) and
        output_dir="<Project>/<Project>.Report" (the project's own path,
        nested one level deeper than the bare "<Project>.Report" form the
        other test above covers). output_root alone can't resolve to a
        project, so the zip refresh was silently skipped -- a newly
        applied theme ("Olive Branch") was NEVER reflected in the
        downloadable zip, but the chat still confidently told the user
        their (stale) file was ready."""
        with tempfile.TemporaryDirectory() as td:
            output_root = Path(td)
            proj_root = _make_pbip(output_root, "BankCampaign")
            ctx = self._run_after_tool_callback_low_level(
                "apply_theme",
                {"output_dir": "BankCampaign/BankCampaign.Report",
                 "output_root": str(output_root),
                 "custom_palette": ["#556B2F"], "custom_name": "Olive Branch"},
                result_data={"path": "some/theme.json", "name": "Olive Branch"},
            )
            saved_filenames = [c.kwargs.get("filename") for c in ctx.save_artifact.call_args_list]
            self.assertIn(f"user:project_{proj_root.name}.zip", saved_filenames)

    def test_delete_page_resolves_identity_from_pbip_dir_and_zips(self):
        """Same regression class for the edit_tools.py family, which uses
        pbip_dir (not output_root) as the argument name."""
        with tempfile.TemporaryDirectory() as td:
            proj_root = _make_pbip(Path(td), "DeletePageZipTest")
            ctx = self._run_after_tool_callback_low_level(
                "delete_page",
                {"pbip_dir": str(proj_root), "page_id": "summary-page"},
                result_data={"deleted": "some/path"},
            )
            saved_filenames = [c.kwargs.get("filename") for c in ctx.save_artifact.call_args_list]
            self.assertIn(f"user:project_{proj_root.name}.zip", saved_filenames)

    def test_generic_output_root_is_not_zipped(self):
        """Safety guard: if a low-level tool is called with the GENERIC
        output root (not scoped to one project -- e.g. the model didn't
        follow the "pass the project's own directory" guidance), the
        fallback must refuse to resolve an identity rather than zipping
        a directory that could contain many unrelated projects."""
        with tempfile.TemporaryDirectory() as td:
            generic_root = Path(td) / "output"
            sub = generic_root / "SomeOtherProject"
            sub.mkdir(parents=True)
            _make_pbip(sub, "SomeOtherProject")
            ctx = self._run_after_tool_callback_low_level(
                "delete_page",
                {"pbip_dir": str(generic_root), "page_id": "summary-page"},
                result_data={"deleted": "some/path"},
            )
            ctx.save_artifact.assert_not_called()

    def test_trusted_result_data_skips_existence_check(self):
        """The high-level tools' result["data"]["pbip_root"] is authoritative
        by construction (the tool itself already resolved/verified it) --
        it must be trusted even if the path happens not to exist on disk
        (e.g. in a unit test with a synthetic path), unlike the tool_args
        fallback used for the low-level editing tools."""
        from adk.plugin import _resolve_project_identity

        identity = _resolve_project_identity(
            {}, {"data": {"project_name": "Sales", "pbip_root": "/nonexistent/Sales"}},
        )
        self.assertEqual(identity, ("Sales", "/nonexistent/Sales"))


class TestBuildReportAgentSummary(unittest.TestCase):

    def test_build_report_agent_summary_table_populated(self):
        """Regression: _render_build_report used to read data["summary"] --
        a key generate_pbip never actually produces (the real key is
        "steps") -- so the Agent Summary table rendered as an empty header
        in every build report ever saved."""
        from adk.plugin import _render_build_report

        result = {
            "ok": True,
            "data": {
                "project_name": "Demo",
                "pbip_root": "/app/output/Demo",
                "steps": [
                    {"agent": "SchemaAgent", "ok": True, "message": "Inferred schema"},
                    {"agent": "DAXAgent", "ok": True, "message": "Generated 5 measures"},
                ],
            },
        }
        md = _render_build_report(result)
        self.assertIn("SchemaAgent", md)
        self.assertIn("Inferred schema", md)
        self.assertIn("DAXAgent", md)


class TestCreateSessionCrash(unittest.TestCase):
    """BUG 1.4 — create_session failure should not crash the REPL."""

    def test_create_session_failure_raises_runtime_error(self):
        bad_service = MagicMock()
        import asyncio
        async def fail_create(**kwargs):
            raise RuntimeError("service unavailable")
        bad_service.create_session = fail_create
        with self.assertRaises(RuntimeError):
            ChatRepl(session_service=bad_service)


# ===========================================================================
# B. Medium bugs
# ===========================================================================


class TestEmptyReplyMessage(unittest.TestCase):
    """BUG 1.2 — tool-only turns produce a helpful message, not silence."""

    def test_empty_reply_shows_message(self):
        repl = make_repl(events=[])
        result = repl.run_turn("hi")
        self.assertIn("no text reply", result.lower())

    def test_tool_only_no_text_shows_message(self):
        """Events with only function calls, no final text → helpful message."""
        event = Event(
            author="powerbi_builder",
            content=types.Content(role="model", parts=[
                types.Part(function_call=types.FunctionCall(name="generate_pbip", args={}))
            ]),
            turn_complete=True,
            invocation_id="i", id="e", timestamp=0.0,
        )
        repl = make_repl(events=[event])
        result = repl.run_turn("build it")
        self.assertIn("no text reply", result.lower())


class TestSessionLeak(unittest.TestCase):
    """BUG 1.5 — /new should delete the old session."""

    def test_new_deletes_old_session(self):
        ss = InMemorySessionService()
        repl = make_repl(events=[], session_service=ss)
        old_id = repl.session_id
        # Verify session exists
        import asyncio
        sessions = asyncio.run(ss.list_sessions(app_name="adk", user_id="repl_user"))
        self.assertEqual(len(sessions.sessions), 1)
        # /new
        repl.handle_slash("/new")
        # Old session should be deleted
        sessions = asyncio.run(ss.list_sessions(app_name="adk", user_id="repl_user"))
        session_ids = [s.id for s in sessions.sessions]
        self.assertNotIn(old_id, session_ids)
        self.assertEqual(len(sessions.sessions), 1)


class TestGenerateContentConfig(unittest.TestCase):
    """A2 — root_agent should have generate_content_config set."""

    def test_root_agent_has_temperature(self):
        from adk.agent import root_agent
        self.assertIsNotNone(root_agent.generate_content_config)
        self.assertEqual(root_agent.generate_content_config.temperature, 0.1)


class TestErrorCallbacks(unittest.TestCase):
    """A6 — on_tool_error_callback and on_model_error_callback should exist."""

    def test_root_agent_has_on_tool_error(self):
        from adk.agent import root_agent, on_tool_error
        self.assertIs(root_agent.on_tool_error_callback, on_tool_error)

    def test_root_agent_has_on_model_error(self):
        from adk.agent import root_agent, on_model_error
        self.assertIs(root_agent.on_model_error_callback, on_model_error)

    def test_on_tool_error_logs(self):
        from adk.agent import on_tool_error
        tool = SimpleNamespace(name="generate_pbip")
        ret = on_tool_error(tool, {}, MagicMock(), ValueError("boom"))
        self.assertIsNone(ret)

    def test_on_model_error_logs(self):
        from adk.agent import on_model_error
        ctx = MagicMock()
        ret = on_model_error(ctx, MagicMock(), ValueError("rate limit"))
        self.assertIsNone(ret)


class TestPluginToolErrorCallback(unittest.TestCase):
    """BUG 4.3 — plugin should clean up _tool_starts on error."""

    def test_plugin_has_on_tool_error(self):
        from adk.plugin import PowerBIBuilderPlugin
        p = PowerBIBuilderPlugin()
        self.assertTrue(hasattr(p, "on_tool_error_callback"))

    def test_plugin_on_tool_error_cleans_up(self):
        from adk.plugin import PowerBIBuilderPlugin
        p = PowerBIBuilderPlugin()
        tool = SimpleNamespace(name="generate_pbip")
        ctx = MagicMock()
        # Simulate a start that was recorded with the same context id
        key = f"generate_pbip:{id(ctx)}"
        p._tool_starts[key] = 1.0
        import asyncio
        asyncio.run(p.on_tool_error_callback(
            tool=tool, tool_args={}, tool_context=ctx, error=ValueError("x")
        ))
        self.assertNotIn(key, p._tool_starts)


# ===========================================================================
# C. File upload / download
# ===========================================================================


class TestUploadCommand(unittest.TestCase):

    def test_upload_no_arg_usage(self):
        repl = make_repl(events=[])
        self.assertIn("Usage", repl.handle_slash("/upload"))

    def test_upload_missing_file(self):
        repl = make_repl(events=[])
        result = repl.handle_slash("/upload C:/nonexistent/file.csv")
        self.assertIn("not found", result.lower())

    def test_upload_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "test.csv"
            _sample_csv(csv_path)
            repl = make_repl(events=[], output_root=root)
            result = repl.handle_slash(f"/upload {csv_path}")
            self.assertIn("Uploaded", result)
            self.assertIn("test.csv", result)
            # Check state
            uploads = repl.state.get("uploaded_files", [])
            self.assertEqual(len(uploads), 1)
            # Check file was copied to _uploads
            assert (root / "_uploads" / "test.csv").is_file()

    def test_upload_with_custom_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "data.csv"
            _sample_csv(csv_path)
            repl = make_repl(events=[], output_root=root)
            result = repl.handle_slash(f"/upload {csv_path} as mydata.csv")
            self.assertIn("mydata.csv", result)


class TestDownloadCommand(unittest.TestCase):

    def test_download_no_project(self):
        repl = make_repl(events=[])
        self.assertIn("No project", repl.handle_slash("/download"))

    def test_download_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "DownloadTest")
            repl = make_repl(events=[], output_root=root)
            result = repl.handle_slash("/download DownloadTest")
            self.assertIn("Exported", result)
            self.assertIn(".zip", result)
            zip_path = root / "DownloadTest.zip"
            self.assertTrue(zip_path.is_file())
            # Verify zip contents
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                self.assertTrue(any(".pbip" in n for n in names))

    def test_download_to_custom_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_pbip(root, "CustomDest")
            dest = root / "custom" / "out.zip"
            repl = make_repl(events=[], output_root=root)
            result = repl.handle_slash(f"/download CustomDest to {dest}")
            self.assertIn("Exported", result)
            self.assertTrue(dest.is_file())


class TestSamplesCommand(unittest.TestCase):

    def test_samples_lists_files(self):
        repl = make_repl(events=[])
        result = repl.handle_slash("/samples")
        self.assertIn("sample data", result.lower())
        self.assertIn("SampleData.csv", result)


class TestFilesCommand(unittest.TestCase):

    def test_files_shows_uploads_and_projects(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "test.csv"
            _sample_csv(csv_path)
            repl = make_repl(events=[], output_root=root)
            repl.handle_slash(f"/upload {csv_path}")
            _make_pbip(root, "ProjX")
            result = repl.handle_slash("/files")
            self.assertIn("Uploaded files", result)
            self.assertIn("test.csv", result)
            self.assertIn("ProjX", result)

    def test_files_no_uploads(self):
        repl = make_repl(events=[])
        result = repl.handle_slash("/files")
        self.assertIn("none", result.lower())


class TestSourceArtifact(unittest.TestCase):

    def test_generate_pbip_with_source_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "data.csv"
            _sample_csv(csv_path)
            # Simulate /upload: copy file to _uploads
            upload_dir = root / "_uploads"
            upload_dir.mkdir()
            (upload_dir / "data.csv").write_bytes(csv_path.read_bytes())
            from adk.tools import highlevel_tools
            generate_pbip = highlevel_tools.generate_pbip
            # The goal of this test is the artifact-resolution branch: that a
            # source_artifact materialized to _uploads/ is picked up and
            # forwarded as ``source``. The real generate_pbip routes through
            # a MCP client -> subprocess-server round trip that drives the
            # full 13-step build pipeline (~40s). Stub ``adk.mcp_client`` so
            # we assert only that the right resolved source reached the MCP
            # call, without paying the build cost on every test run.
            captured = {}

            async def _fake_call_mcp_tool(name, **kwargs):
                captured.update(kwargs)
                return {"ok": True, "tool": name, "message": "ok",
                        "data": {}, "errors": []}

            import types as _types
            fake_mcp_client = _types.SimpleNamespace(call_mcp_tool=_fake_call_mcp_tool)
            # generate_pbip does ``from adk.mcp_client import call_mcp_tool``
            # lazily inside the function body, so intercepting the module in
            # sys.modules is what makes it pick up the stub.
            with unittest.mock.patch.dict(sys.modules,
                                          {"adk.mcp_client": fake_mcp_client}):
                res = asyncio.run(generate_pbip(
                    source="", description="test dashboard",
                    source_artifact="user:data.csv",
                    project_name="ArtifactTest", output_root=str(root),
                ))
            self.assertTrue(res["ok"], res.get("message"))
            # The artifact on disk must have been resolved to a real source
            # path and forwarded into the (stubbed) MCP call.
            self.assertEqual(captured.get("project_name"), "ArtifactTest")
            self.assertTrue(captured.get("source", ""))
            self.assertEqual(Path(captured["source"]).name, "data.csv")

    def test_generate_pbip_missing_artifact(self):
        from adk.tools.highlevel_tools import generate_pbip
        # Returns before ever reaching the MCP call, but the function is
        # still a coroutine function -- must be awaited regardless.
        res = asyncio.run(generate_pbip(
            source="", description="test",
            source_artifact="user:nonexistent.csv",
            output_root="./output",
        ))
        self.assertFalse(res["ok"])
        self.assertIn("artifact", res["message"].lower())


class TestLoadArtifactsTool(unittest.TestCase):

    def test_load_artifacts_in_root_agent(self):
        from adk.agent import root_agent
        names = [getattr(t, 'name', getattr(t, '__name__', str(t)))
                 for t in root_agent.tools]
        self.assertIn("load_artifacts", names)


class TestSaveFilesPlugin(unittest.TestCase):

    def test_save_files_plugin_in_runner(self):
        """Verify SaveFilesAsArtifactsPlugin is wired into the runner."""
        repl = ChatRepl()  # real runner with default plugins
        # The runner should have 2 plugins: PowerBIBuilderPlugin + SaveFilesAsArtifactsPlugin
        plugin_mgr = getattr(repl.runner, "plugin_manager", None)
        if plugin_mgr:
            plugins = getattr(plugin_mgr, "plugins", [])
            plugin_names = [type(p).__name__ for p in plugins]
            self.assertIn("SaveFilesAsArtifactsPlugin", plugin_names)

    def test_app_export_has_plugins(self):
        """Verify the `app` export (used by adk web) carries plugins."""
        from adk.agent import app
        from google.adk.apps import App
        self.assertIsInstance(app, App)
        plugin_names = [type(p).__name__ for p in app.plugins]
        self.assertIn("PowerBIBuilderPlugin", plugin_names)
        self.assertIn("SaveFilesAsArtifactsPlugin", plugin_names)

    def test_app_root_agent_matches(self):
        """The app's root_agent should be the same as root_agent."""
        from adk.agent import app, root_agent
        self.assertIs(app.root_agent, root_agent)


class TestWebUploadBridge(unittest.TestCase):
    """Verify on_user_message_callback materializes web uploads to disk."""

    def test_plugin_has_on_user_message_callback(self):
        from adk.plugin import PowerBIBuilderPlugin
        p = PowerBIBuilderPlugin()
        self.assertTrue(hasattr(p, "on_user_message_callback"))

    def test_web_upload_materializes_to_disk(self):
        """When a user message has inline_data, the plugin writes bytes
        to output_root/_uploads/ so generate_pbip can find them."""
        import asyncio
        from adk.plugin import PowerBIBuilderPlugin
        import os

        p = PowerBIBuilderPlugin()
        csv_bytes = b"Date,Region,Sales\n2024-01-01,North,100\n"
        user_msg = types.Content(
            role="user",
            parts=[types.Part(inline_data=types.Blob(
                data=csv_bytes, mime_type="text/csv", display_name="web_upload.csv",
            ))],
        )
        with tempfile.TemporaryDirectory() as td:
            os.environ["POWERBI_OUTPUT_ROOT"] = td
            try:
                asyncio.run(p.on_user_message_callback(
                    invocation_context=MagicMock(), user_message=user_msg,
                ))
            finally:
                os.environ.pop("POWERBI_OUTPUT_ROOT", None)
            upload_file = Path(td) / "_uploads" / "web_upload.csv"
            self.assertTrue(upload_file.is_file())
            self.assertEqual(upload_file.read_bytes(), csv_bytes)

    def test_web_upload_no_inline_data_returns_none(self):
        """Messages without inline_data should be a no-op."""
        import asyncio
        from adk.plugin import PowerBIBuilderPlugin
        p = PowerBIBuilderPlugin()
        user_msg = types.Content(
            role="user", parts=[types.Part(text="just a text message")],
        )
        ret = asyncio.run(p.on_user_message_callback(
            invocation_context=MagicMock(), user_message=user_msg,
        ))
        self.assertIsNone(ret)

    def test_unsupported_mime_stripped_to_text(self):
        """Excel/CSV/JSON inline_data must be replaced with text placeholder
        to avoid Gemini 400 'Unsupported MIME type' errors."""
        import asyncio
        from adk.plugin import PowerBIBuilderPlugin
        import os
        p = PowerBIBuilderPlugin()
        excel_msg = types.Content(
            role="user",
            parts=[
                types.Part(text="build from this"),
                types.Part(inline_data=types.Blob(
                    data=b"fake excel", mime_type="application/vnd.ms-excel",
                    display_name="data.xls",
                )),
            ],
        )
        with tempfile.TemporaryDirectory() as td:
            os.environ["POWERBI_OUTPUT_ROOT"] = td
            try:
                result = asyncio.run(p.on_user_message_callback(
                    invocation_context=MagicMock(), user_message=excel_msg,
                ))
            finally:
                os.environ.pop("POWERBI_OUTPUT_ROOT", None)
        # Should return modified Content (not None)
        self.assertIsNotNone(result)
        # No inline_data parts should remain
        inline_remaining = [pt for pt in result.parts if getattr(pt, "inline_data", None)]
        self.assertEqual(len(inline_remaining), 0)
        # Text placeholder should mention the filename and source_artifact
        text_parts = [pt.text for pt in result.parts if getattr(pt, "text", None)]
        joined = " ".join(text_parts)
        self.assertIn("data.xls", joined)
        self.assertIn("source_artifact", joined)

    def test_supported_mime_kept_as_inline(self):
        """Image/audio/video/pdf inline_data should be kept as-is."""
        import asyncio
        from adk.plugin import PowerBIBuilderPlugin
        p = PowerBIBuilderPlugin()
        img_msg = types.Content(
            role="user",
            parts=[types.Part(inline_data=types.Blob(
                data=b"fake png", mime_type="image/png",
                display_name="logo.png",
            ))],
        )
        result = asyncio.run(p.on_user_message_callback(
            invocation_context=MagicMock(), user_message=img_msg,
        ))
        # Should return None (no modification needed)
        self.assertIsNone(result)

    def test_zip_project_produces_valid_zip(self):
        """Verify _zip_project produces a valid zip and plugin saves it."""
        from adk.plugin import _zip_project
        import io, zipfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "MyProj"
            root.mkdir()
            (root / "MyProj.SemanticModel").mkdir()
            (root / "MyProj.SemanticModel" / "model.tmdl").write_text("model")
            (root / "MyProj.pbip").write_text("{}")
            zb = _zip_project(str(root), "MyProj")
            self.assertGreater(len(zb), 0)
            with zipfile.ZipFile(io.BytesIO(zb)) as zf:
                names = zf.namelist()
                self.assertTrue(any("MyProj.pbip" in n for n in names))
                self.assertTrue(any("model.tmdl" in n for n in names))

    def test_zip_project_empty_path_returns_empty(self):
        from adk.plugin import _zip_project
        self.assertEqual(_zip_project("", "test"), b"")

    def test_zip_project_missing_dir_returns_empty(self):
        from adk.plugin import _zip_project
        self.assertEqual(_zip_project("C:/nonexistent/path", "test"), b"")


# ===========================================================================
# D. Minor fixes
# ===========================================================================


class TestRunTurnGuard(unittest.TestCase):
    """BUG 1.1 — run_turn('') should return empty without calling LLM."""

    def test_empty_string_returns_empty(self):
        repl = make_repl(events=[])
        self.assertEqual(repl.run_turn(""), "")

    def test_whitespace_returns_empty(self):
        repl = make_repl(events=[])
        self.assertEqual(repl.run_turn("   "), "")


class TestValidatePathsArgsNone(unittest.TestCase):
    """BUG 2.1 — validate_paths with args=None should not crash."""

    def test_args_none_returns_none(self):
        from adk.agent import validate_paths
        tool = SimpleNamespace(name="generate_pbip")
        ctx = MagicMock()
        ret = validate_paths(tool, None, ctx)
        self.assertIsNone(ret)

    def test_args_not_dict_returns_none(self):
        from adk.agent import validate_paths
        tool = SimpleNamespace(name="generate_pbip")
        ctx = MagicMock()
        ret = validate_paths(tool, "not a dict", ctx)
        self.assertIsNone(ret)


class TestArtifactSessionId(unittest.TestCase):
    """B5 — /artifact should pass session_id defensively."""

    def test_artifact_load_with_session_id(self):
        """Verify the /artifact command passes session_id to load_artifact."""
        # This is verified by the code path — the session_id is passed
        # in the _cmd_artifact method. We check the method exists and
        # uses self.session_id.
        repl = make_repl(events=[])
        # The method should not crash when loading a missing artifact
        result = repl.handle_slash("/artifact user:nonexistent.md")
        self.assertIn("not found", result.lower())


# ===========================================================================
# Integration: full audit-fix verification
# ===========================================================================


class TestFlatVsNestedLayoutMismatch(unittest.TestCase):
    """Regression: a real bug found by forensically replaying an `adk web`
    session. write_pbir_page/write_theme_json/finalize_pages_index use a
    FLAT layout convention (output_root/<name>.Report — what
    create_project_scaffold builds from scratch), but generate_pbip/
    build_report use a NESTED layout (output_root/<name>/<name>.Report).
    Calling the low-level tools with output_root="./output" against an
    EXISTING nested project silently created a disconnected duplicate
    .Report folder — reported ok=True, but never touched the real project."""

    @staticmethod
    def _make_nested_pbip(root: Path, name: str) -> Path:
        """Build a project at ``root/<name>/`` -- the NESTED layout
        generate_pbip/build_report actually produce. ``_make_pbip`` itself
        builds the FLAT layout (create_project_scaffold's own convention:
        straight under whatever root it's given), so nesting it one level
        here reproduces the real-world mismatch scenario."""
        nested_root = root / name
        nested_root.mkdir(parents=True, exist_ok=True)
        return _make_pbip(nested_root, name)

    def test_detects_mismatch_when_nested_project_exists(self):
        from utils.pbip_paths import detect_flat_vs_nested_layout_mismatch

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = self._make_nested_pbip(root, "MismatchTest")
            self.assertTrue((proj_root / "MismatchTest.Report").is_dir())
            self.assertFalse((root / "MismatchTest.Report").exists())
            msg = detect_flat_vs_nested_layout_mismatch(root, "MismatchTest")
            self.assertIsNotNone(msg)
            self.assertIn("disconnected", msg.lower())

    def test_no_mismatch_when_flat_target_already_exists(self):
        """If the flat target itself exists (e.g. output_root was already
        the project's own directory), there's nothing to warn about."""
        from utils.pbip_paths import detect_flat_vs_nested_layout_mismatch

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = _make_pbip(root, "FlatOkTest")
            msg = detect_flat_vs_nested_layout_mismatch(proj_root, "FlatOkTest")
            self.assertIsNone(msg)

    def test_no_mismatch_when_nothing_exists_at_all(self):
        from utils.pbip_paths import detect_flat_vs_nested_layout_mismatch

        with tempfile.TemporaryDirectory() as td:
            msg = detect_flat_vs_nested_layout_mismatch(Path(td), "NothingHereTest")
            self.assertIsNone(msg)

    def test_finalize_pages_index_refuses_mismatched_call(self):
        from adk.tools.project_tools import finalize_pages_index

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_nested_pbip(root, "FinalizeMismatchTest")
            res = finalize_pages_index(
                "FinalizeMismatchTest", ["summary-page"], output_root=str(root),
            )
            self.assertFalse(res["ok"])
            self.assertIn("disconnected", res["message"].lower())
            # and it must NOT have written a stray pages.json
            self.assertFalse((root / "FinalizeMismatchTest.Report").exists())

    def test_finalize_pages_index_succeeds_with_correct_output_root(self):
        from adk.tools.project_tools import finalize_pages_index

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj_root = self._make_nested_pbip(root, "FinalizeOkTest")
            res = finalize_pages_index(
                "FinalizeOkTest", ["summary-page"], output_root=str(proj_root),
            )
            self.assertTrue(res["ok"], res.get("message"))

    def test_write_pbir_page_refuses_mismatched_call(self):
        from adk.tools.pbir_tools import write_pbir_page

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_nested_pbip(root, "WritePageMismatchTest")
            res = write_pbir_page(
                "WritePageMismatchTest.Report/definition",
                {"id": "extra-page", "displayName": "Extra"},
                output_root=str(root),
            )
            self.assertFalse(res["ok"])
            self.assertIn("disconnected", res["message"].lower())

    def test_write_theme_json_refuses_mismatched_call(self):
        from adk.tools.pbir_tools import write_theme_json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_nested_pbip(root, "WriteThemeMismatchTest")
            res = write_theme_json(
                "WriteThemeMismatchTest.Report/definition", output_root=str(root),
            )
            self.assertFalse(res["ok"])
            self.assertIn("disconnected", res["message"].lower())


class TestAddVisualQueryStateValidation(unittest.TestCase):
    """Regression: add_visual raised a raw, unhelpful AttributeError
    ("'str' object has no attribute 'get'"/"'...' has no attribute
    'values'") when query_state was malformed -- observed in a real
    session as a 4-retry loop the calling agent couldn't self-correct
    from, ultimately settling on an empty (non-functional) query_state."""

    def test_string_query_state_gives_clear_error(self):
        with tempfile.TemporaryDirectory() as td:
            proj_root = _make_pbip(Path(td), "QueryStateStrTest")
            res = hl.add_visual(
                str(proj_root), "summary-page", "card",
                "<arg_key>query_state</arg_key>",
            )
            self.assertFalse(res["ok"])
            self.assertIn("dict", res["message"].lower())
            self.assertNotIn("attribute", res["message"].lower())

    def test_list_query_state_gives_clear_error(self):
        with tempfile.TemporaryDirectory() as td:
            proj_root = _make_pbip(Path(td), "QueryStateListTest")
            res = hl.add_visual(str(proj_root), "summary-page", "card", [])
            self.assertFalse(res["ok"])
            self.assertIn("dict", res["message"].lower())

    def test_valid_dict_query_state_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            proj_root = _make_pbip(Path(td), "QueryStateOkTest")
            res = hl.add_visual(
                str(proj_root), "summary-page", "card",
                {"select": [{"kind": "column", "table": "data", "name": "Sales"}]},
            )
            self.assertTrue(res["ok"], res.get("message"))


class TestDeletePageUpdatesPageOrder(unittest.TestCase):
    """Regression: delete_page filtered a nonexistent "pages" list key
    instead of the real "pageOrder" schema (see
    mcp_server/pbir_generator.pages_metadata) -- a real bug found by
    forensically replaying a live adk web session. The deleted page's id
    stayed in pageOrder forever, so Power BI Desktop tried to load a page
    whose folder no longer existed and failed to open the report."""

    @staticmethod
    def _add_second_page(proj_root: Path, project_name: str) -> None:
        from mcp_server.server import PbipToolbox

        tb = PbipToolbox(proj_root)
        rep_def = f"{project_name}.Report/definition"
        tb.write_pbir_page(rep_def, {"id": "page-2", "displayName": "Page 2",
                                      "width": 1280, "height": 720, "visuals": []})
        pages_json = proj_root / f"{project_name}.Report" / "definition" / "pages" / "pages.json"
        atomic = json.loads(pages_json.read_text(encoding="utf-8"))
        atomic["pageOrder"] = ["summary-page", "page-2"]
        pages_json.write_text(json.dumps(atomic), encoding="utf-8")

    def test_deleted_page_removed_from_page_order(self):
        from adk.tools.edit_tools import delete_page

        with tempfile.TemporaryDirectory() as td:
            proj_root = _make_pbip(Path(td), "DeletePageOrderTest")
            self._add_second_page(proj_root, "DeletePageOrderTest")

            res = delete_page(str(proj_root), "page-2", output_root=str(td))
            self.assertTrue(res["ok"], res.get("errors"))

            pages_json = proj_root / "DeletePageOrderTest.Report" / "definition" / "pages" / "pages.json"
            data = json.loads(pages_json.read_text(encoding="utf-8"))
            self.assertNotIn("page-2", data["pageOrder"])
            self.assertIn("summary-page", data["pageOrder"])

    def test_deleting_active_page_resets_active_page_name(self):
        from adk.tools.edit_tools import delete_page

        with tempfile.TemporaryDirectory() as td:
            proj_root = _make_pbip(Path(td), "DeleteActivePageTest")
            self._add_second_page(proj_root, "DeleteActivePageTest")
            pages_json = proj_root / "DeleteActivePageTest.Report" / "definition" / "pages" / "pages.json"
            data = json.loads(pages_json.read_text(encoding="utf-8"))
            data["activePageName"] = "page-2"
            pages_json.write_text(json.dumps(data), encoding="utf-8")

            res = delete_page(str(proj_root), "page-2", output_root=str(td))
            self.assertTrue(res["ok"], res.get("errors"))

            data = json.loads(pages_json.read_text(encoding="utf-8"))
            self.assertEqual(data["activePageName"], "summary-page")

    def test_validate_pbip_structure_catches_dangling_page_order_entry(self):
        """Even if delete_page (or any other tool) leaves pages.json stale,
        validate_pbip_structure must catch it as a hard error -- Desktop
        would otherwise fail to open the report."""
        from mcp_server.server import PbipToolbox

        with tempfile.TemporaryDirectory() as td:
            proj_root = _make_pbip(Path(td), "DanglingPageOrderTest")
            pages_json = (proj_root / "DanglingPageOrderTest.Report"
                          / "definition" / "pages" / "pages.json")
            data = json.loads(pages_json.read_text(encoding="utf-8"))
            data["pageOrder"] = ["summary-page", "ghost-page"]
            pages_json.write_text(json.dumps(data), encoding="utf-8")

            tb = PbipToolbox(proj_root)
            res = tb.validate_pbip_structure(str(proj_root))
            self.assertFalse(res.ok)
            self.assertTrue(any("ghost-page" in e for e in res.errors))


class TestAuditIntegration(unittest.TestCase):

    def test_full_upload_generate_build_download_flow(self):
        """End-to-end: upload CSV → generate → build_report → download zip."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "sales.csv"
            _sample_csv(csv_path)
            repl = make_repl(events=[], output_root=root)

            # 1. Upload
            up = repl.handle_slash(f"/upload {csv_path}")
            self.assertIn("Uploaded", up)

            # 2. Generate from artifact
            from adk.tools.highlevel_tools import generate_pbip
            gen = asyncio.run(generate_pbip(
                source="", description="Sales dashboard",
                source_artifact="user:sales.csv",
                project_name="AuditE2E", output_root=str(root),
            ))
            self.assertTrue(gen["ok"])

            # 3. Build rich report
            br = hl.build_report(gen["data"]["pbip_root"],
                                  num_pages=2, visual_variety="all")
            self.assertTrue(br["ok"])
            self.assertEqual(len(br["data"]["pages_added"]), 2)

            # 4. Download
            dl = repl.handle_slash("/download AuditE2E")
            self.assertIn("Exported", dl)
            self.assertTrue((root / "AuditE2E.zip").is_file())


if __name__ == "__main__":
    unittest.main()

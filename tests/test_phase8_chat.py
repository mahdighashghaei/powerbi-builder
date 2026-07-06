"""Phase 8 tests — AI Chat Assistant (REPL, slash commands, session state,
memory/artifact services, plugin callbacks, multi-agent architecture).

These tests exercise ``adk/chat.py`` and ``adk/plugin.py`` without making
real LLM calls: a mock ``Runner`` yields fake ``Event`` objects that
mimic the streaming + state-delta behaviour of a real ADK run.
"""
from __future__ import annotations

import asyncio
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

from google.adk.artifacts import InMemoryArtifactService  # noqa: E402
from google.adk.events import Event, EventActions  # noqa: E402
from google.adk.memory import InMemoryMemoryService  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from adk.chat import ChatRepl, event_text, event_has_error  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — fake events + mock runner
# ---------------------------------------------------------------------------


def fake_event(
    text: str = "",
    author: str = "powerbi_builder",
    turn_complete: bool = True,
    partial: bool = False,
    state_delta: dict | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> Event:
    """Build a realistic ADK ``Event`` for tests."""
    kwargs: dict = dict(
        author=author,
        turn_complete=turn_complete,
        partial=partial,
        invocation_id="inv-test",
        id="evt-test",
        timestamp=0.0,
    )
    if text:
        kwargs["content"] = types.Content(
            role="model", parts=[types.Part(text=text)]
        )
    if state_delta:
        kwargs["actions"] = EventActions(state_delta=state_delta)
    if error_code:
        kwargs["error_code"] = error_code
        if error_message:
            kwargs["error_message"] = error_message
    return Event(**kwargs)


def mock_runner(events: list[Event]) -> MagicMock:
    """Return a MagicMock whose ``.run(...)`` yields the given events."""
    r = MagicMock()
    r.run.return_value = iter(events)
    return r


def _iter_input(lines: list[str]):
    """Build an input_fn for ``cmd_loop`` that yields the given lines then
    raises ``EOFError`` (instead of ``StopIteration``) so the REPL loop ends."""
    it = iter(lines)

    def _fn(_prompt: str) -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return _fn


def make_repl(
    *,
    events: list[Event] | None = None,
    runner: MagicMock | None = None,
    output_root: str | Path | None = None,
    session_service=None,
    artifact_service=None,
    memory_service=None,
) -> ChatRepl:
    """Convenience constructor: ChatRepl with a mock runner (no LLM)."""
    if runner is None:
        runner = mock_runner(events or [])
    return ChatRepl(
        runner=runner,
        output_root=str(output_root) if output_root else None,
        session_service=session_service,
        artifact_service=artifact_service,
        memory_service=memory_service,
    )


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


class TestEventHelpers(unittest.TestCase):

    def test_event_text_extracts_parts(self):
        e = fake_event(text="hello world")
        self.assertEqual(event_text(e), "hello world")

    def test_event_text_empty_when_no_content(self):
        e = fake_event(text="")
        self.assertEqual(event_text(e), "")

    def test_event_text_concatenates_multiple_parts(self):
        e = Event(
            author="m",
            content=types.Content(
                role="model",
                parts=[types.Part(text="a"), types.Part(text="b"), types.Part(text="c")],
            ),
            turn_complete=True,
            invocation_id="i", id="e", timestamp=0.0,
        )
        self.assertEqual(event_text(e), "abc")

    def test_event_has_error_returns_message(self):
        e = fake_event(error_code="E503", error_message="rate limited")
        err = event_has_error(e)
        self.assertIsNotNone(err)
        self.assertIn("E503", err)
        self.assertIn("rate limited", err)

    def test_event_has_error_none_when_ok(self):
        e = fake_event(text="ok")
        self.assertIsNone(event_has_error(e))


# ---------------------------------------------------------------------------
# Slash-command parsing & dispatch
# ---------------------------------------------------------------------------


class TestSlashParsing(unittest.TestCase):

    def setUp(self):
        self.repl = make_repl(events=[])

    def test_non_slash_returns_none(self):
        self.assertIsNone(self.repl.handle_slash("hello there"))
        self.assertIsNone(self.repl.handle_slash("  build me a dashboard"))

    def test_help_lists_commands(self):
        out = self.repl.handle_slash("/help")
        self.assertIn("/help", out)
        self.assertIn("/new", out)
        self.assertIn("/state", out)
        self.assertIn("/exit", out)

    def test_unknown_command_reports_error(self):
        out = self.repl.handle_slash("/frobnicate")
        self.assertIn("Unknown command", out)

    def test_exit_sets_quit_flag(self):
        out = self.repl.handle_slash("/exit")
        self.assertTrue(self.repl._should_quit)
        self.assertIn("Goodbye", out)

    def test_state_empty(self):
        out = self.repl.handle_slash("/state")
        self.assertIn("empty", out.lower())

    def test_state_shows_current_project(self):
        self.repl.session.state["current_project"] = "Sales"
        out = self.repl.handle_slash("/state")
        self.assertIn("Sales", out)

    def test_new_clears_session(self):
        old_id = self.repl.session_id
        out = self.repl.handle_slash("/new")
        self.assertIn("New session", out)
        self.assertNotEqual(self.repl.session_id, old_id)
        # state should be fresh/empty
        self.assertEqual(dict(self.repl.state).get("current_project"), None)

    def test_open_no_project_no_arg(self):
        out = self.repl.handle_slash("/open")
        self.assertIn("No project", out)

    def test_open_uses_current_project(self):
        self.repl.session.state["current_project"] = "Demo"
        out = self.repl.handle_slash("/open")
        self.assertIn("Demo", out)

    def test_memory_no_arg_usage(self):
        out = self.repl.handle_slash("/memory")
        self.assertIn("Usage", out)

    def test_artifact_empty(self):
        out = self.repl.handle_slash("/artifact")
        self.assertIn("No artifacts", out)

    def test_theme_no_arg_usage(self):
        out = self.repl.handle_slash("/theme")
        self.assertIn("Usage", out)

    def test_add_measure_no_arg_usage(self):
        out = self.repl.handle_slash("/add-measure")
        self.assertIn("Usage", out)

    def test_add_measure_hyphen_maps_to_method(self):
        # /add-measure (with hyphen) must dispatch to _cmd_add_measure
        out = self.repl.handle_slash("/add-measure Foo SUM(t[x])")
        self.assertNotIn("Unknown command", out)

    def test_deploy_no_arg_usage(self):
        out = self.repl.handle_slash("/deploy")
        self.assertIn("Usage", out)


# ---------------------------------------------------------------------------
# Runner mock — streaming + text extraction
# ---------------------------------------------------------------------------


class TestRunnerMock(unittest.TestCase):

    def test_streaming_chunks_concatenated(self):
        repl = make_repl(events=[
            fake_event(text="Hello ", partial=True, turn_complete=False),
            fake_event(text="world!", partial=False, turn_complete=True),
        ])
        reply = repl.run_turn("hi")
        self.assertEqual(reply, "Hello world!")

    def test_single_final_event(self):
        repl = make_repl(events=[fake_event(text="Done.", turn_complete=True)])
        self.assertEqual(repl.run_turn("go"), "Done.")

    def test_error_event_returned(self):
        repl = make_repl(events=[
            fake_event(error_code="E503", error_message="rate limited"),
        ])
        reply = repl.run_turn("hi")
        self.assertIn("E503", reply)
        self.assertIn("rate limited", reply)

    def test_empty_event_stream_returns_no_reply_message(self):
        repl = make_repl(events=[])
        # An empty event stream (no text, no errors) now returns a helpful
        # "no text reply" message instead of a silent empty string.
        result = repl.run_turn("hi")
        self.assertIn("no text reply", result.lower())


# ---------------------------------------------------------------------------
# Session state tracking via state_delta
# ---------------------------------------------------------------------------


class TestSessionState(unittest.TestCase):

    def test_state_delta_persisted_in_session(self):
        repl = make_repl(events=[
            fake_event(text="built", state_delta={"current_project": "Sales"}),
        ])
        repl.run_turn("build it")
        self.assertEqual(repl.state.get("current_project"), "Sales")

    def test_state_persists_across_turns(self):
        repl = make_repl(events=[
            fake_event(state_delta={"current_project": "Sales"}),
        ])
        repl.run_turn("turn 1")
        # second turn with no delta keeps prior state
        repl2_events = [fake_event(text="ok")]
        repl.runner.run.return_value = iter(repl2_events)
        repl.run_turn("turn 2")
        self.assertEqual(repl.state.get("current_project"), "Sales")

    def test_state_command_reflects_delta(self):
        repl = make_repl(events=[
            fake_event(state_delta={"current_project": "Demo",
                                    "current_project_root": "/tmp/Demo"}),
        ])
        repl.run_turn("build")
        out = repl.handle_slash("/state")
        self.assertIn("Demo", out)
        self.assertIn("/tmp/Demo", out)


# ---------------------------------------------------------------------------
# Current-project tracking callback (track_project)
# ---------------------------------------------------------------------------


class TestCurrentProjectTracking(unittest.TestCase):

    def _fake_tool_context(self, state=None):
        ctx = MagicMock()
        ctx.state = state if state is not None else {}
        return ctx

    def test_track_project_from_generate_pbip(self):
        from adk.agent import track_project
        tool = SimpleNamespace(name="generate_pbip")
        ctx = self._fake_tool_context()
        tool_response = {"ok": True, "data": {"project_name": "Sales", "pbip_root": "/o/Sales"}}
        # ADK calls with keyword args: tool=, args=, tool_context=, tool_response=
        ret = track_project(tool=tool, args={}, tool_context=ctx, tool_response=tool_response)
        self.assertIsNone(ret)
        self.assertEqual(ctx.state["current_project"], "Sales")
        self.assertEqual(ctx.state["current_project_root"], "/o/Sales")

    def test_track_project_from_create_scaffold_uses_args(self):
        from adk.agent import track_project
        tool = SimpleNamespace(name="create_project_scaffold")
        ctx = self._fake_tool_context()
        tool_response = {"ok": True, "data": {"project_root": "/o"}}
        track_project(tool=tool, args={"project_name": "Demo"}, tool_context=ctx, tool_response=tool_response)
        self.assertEqual(ctx.state["current_project"], "Demo")
        self.assertEqual(ctx.state["current_project_root"], "/o")

    def test_track_project_skips_failed_result(self):
        from adk.agent import track_project
        tool = SimpleNamespace(name="generate_pbip")
        ctx = self._fake_tool_context()
        tool_response = {"ok": False, "data": {}, "errors": ["boom"]}
        track_project(tool=tool, args={}, tool_context=ctx, tool_response=tool_response)
        self.assertNotIn("current_project", ctx.state)

    def test_track_project_ignores_unrelated_tool(self):
        from adk.agent import track_project
        tool = SimpleNamespace(name="write_tmdl_table")
        ctx = self._fake_tool_context()
        track_project(tool=tool, args={}, tool_context=ctx, tool_response={"ok": True, "data": {}})
        self.assertNotIn("current_project", ctx.state)


# ---------------------------------------------------------------------------
# Memory service
# ---------------------------------------------------------------------------


class TestMemoryService(unittest.TestCase):

    def test_add_and_search_memory(self):
        ms = InMemoryMemoryService()
        ss = InMemorySessionService()
        repl = make_repl(
            events=[fake_event(text="Built SalesDashboard from sales.csv")],
            memory_service=ms,
            session_service=ss,
        )
        repl.run_turn("build")
        # The mock Runner does not append events to the session (the real
        # Runner does). Simulate that by adding the event directly so the
        # memory service has content to index.
        repl.session.events.append(
            fake_event(text="Built SalesDashboard from sales.csv")
        )
        # re-add now that the session has an event
        asyncio.run(ms.add_session_to_memory(repl.session))
        # /memory command searches the memory service
        out = repl.handle_slash("/memory sales")
        self.assertNotIn("No memories", out)

    def test_memory_no_match(self):
        repl = make_repl(events=[fake_event(text="hello")])
        repl.run_turn("hi")
        out = repl.handle_slash("/memory nonexistent_topic_xyz")
        self.assertIn("No memories", out)


# ---------------------------------------------------------------------------
# Artifact service
# ---------------------------------------------------------------------------


class TestArtifactService(unittest.TestCase):

    def test_save_and_load_artifact_roundtrip(self):
        svc = InMemoryArtifactService()
        part = types.Part(text="# Build Report\nHello")
        # user-scoped (prefix "user:") so no session_id is required
        ver = asyncio.run(svc.save_artifact(
            app_name="a", user_id="u", filename="user:report.md", artifact=part
        ))
        self.assertGreaterEqual(ver, 0)
        loaded = asyncio.run(svc.load_artifact(
            app_name="a", user_id="u", filename="user:report.md"
        ))
        self.assertIsNotNone(loaded)
        self.assertIn("Build Report", loaded.text)

    def test_artifact_command_lists_recorded(self):
        repl = make_repl(events=[])
        repl.session.state["artifacts"] = [
            {"filename": "user:build_report_Sales.md", "version": 0, "project": "Sales"}
        ]
        out = repl.handle_slash("/artifact")
        self.assertIn("build_report_Sales.md", out)
        self.assertIn("Sales", out)

    def test_artifact_command_loads_text(self):
        svc = InMemoryArtifactService()
        asyncio.run(svc.save_artifact(
            app_name="adk", user_id="repl_user",
            filename="user:rep.md", artifact=types.Part(text="# Hello artifact"),
        ))
        repl = make_repl(events=[], artifact_service=svc)
        out = repl.handle_slash("/artifact user:rep.md")
        self.assertIn("Hello artifact", out)

    def test_artifact_not_found(self):
        repl = make_repl(events=[])
        out = repl.handle_slash("/artifact user:missing.md")
        self.assertIn("not found", out)


# ---------------------------------------------------------------------------
# Plugin callbacks
# ---------------------------------------------------------------------------


class TestPluginCallbacks(unittest.TestCase):

    def _make_tool_context_with_services(self):
        """A ToolContext-like mock with working async save_artifact + state."""
        svc = InMemoryArtifactService()
        ctx = MagicMock()
        ctx.state = {}

        async def save_artifact(*, filename, artifact, custom_metadata=None):
            return await svc.save_artifact(
                app_name="a", user_id="u", filename=filename, artifact=artifact
            )

        ctx.save_artifact = save_artifact
        return ctx, svc

    def test_after_tool_saves_artifact_on_generate_success(self):
        from adk.plugin import PowerBIBuilderPlugin
        plugin = PowerBIBuilderPlugin()
        tool = SimpleNamespace(name="generate_pbip")
        ctx, svc = self._make_tool_context_with_services()
        result = {
            "ok": True,
            "data": {
                "project_name": "Sales",
                "pbip_root": "/o/Sales",
                "summary": [{"agent": "Schema", "ok": True, "message": "ok"}],
            },
        }
        asyncio.run(plugin.after_tool_callback(
            tool=tool, tool_args={}, tool_context=ctx, result=result
        ))
        # artifact saved (user-scoped: prefix "user:")
        loaded = asyncio.run(svc.load_artifact(
            app_name="a", user_id="u", filename="user:build_report_Sales.md"
        ))
        self.assertIsNotNone(loaded)
        self.assertIn("Sales", loaded.text)
        # state recorded
        self.assertEqual(ctx.state["artifacts"][0]["filename"], "user:build_report_Sales.md")

    def test_after_tool_no_artifact_for_unrelated_tool(self):
        from adk.plugin import PowerBIBuilderPlugin
        plugin = PowerBIBuilderPlugin()
        tool = SimpleNamespace(name="write_tmdl_table")
        ctx, svc = self._make_tool_context_with_services()
        asyncio.run(plugin.after_tool_callback(
            tool=tool, tool_args={}, tool_context=ctx,
            result={"ok": True, "data": {}},
        ))
        self.assertNotIn("artifacts", ctx.state)

    def test_after_tool_no_artifact_on_failure(self):
        from adk.plugin import PowerBIBuilderPlugin
        plugin = PowerBIBuilderPlugin()
        tool = SimpleNamespace(name="generate_pbip")
        ctx, svc = self._make_tool_context_with_services()
        asyncio.run(plugin.after_tool_callback(
            tool=tool, tool_args={}, tool_context=ctx,
            result={"ok": False, "data": {}, "errors": ["x"]},
        ))
        self.assertNotIn("artifacts", ctx.state)

    def test_before_tool_callback_returns_none(self):
        from adk.plugin import PowerBIBuilderPlugin
        plugin = PowerBIBuilderPlugin()
        tool = SimpleNamespace(name="read_csv_schema")
        ctx = MagicMock()
        ret = asyncio.run(plugin.before_tool_callback(
            tool=tool, tool_args={"csv_path": "x"}, tool_context=ctx
        ))
        self.assertIsNone(ret)

    def test_before_agent_callback_returns_none(self):
        from adk.plugin import PowerBIBuilderPlugin
        plugin = PowerBIBuilderPlugin()
        agent = SimpleNamespace(name="powerbi_builder")
        cc = MagicMock()
        ret = asyncio.run(plugin.before_agent_callback(agent=agent, callback_context=cc))
        self.assertIsNone(ret)


# ---------------------------------------------------------------------------
# Multi-agent architecture
# ---------------------------------------------------------------------------


class TestMultiAgent(unittest.TestCase):

    def test_root_agent_has_four_sub_agents(self):
        from adk.agent import root_agent
        names = [s.name for s in root_agent.sub_agents]
        # planner + analyzer + cleaner + reviewer + status added (Phase 2-6).
        self.assertEqual(names, [
            "planner", "data_analyzer", "data_cleaner", "schema_specialist",
            "dax_specialist", "report_specialist", "report_reviewer",
            "status", "deploy_specialist",
        ])

    def test_root_agent_has_callbacks_wired(self):
        from adk.agent import root_agent, track_project, log_model_request
        self.assertIs(root_agent.after_tool_callback, track_project)
        self.assertIs(root_agent.before_model_callback, log_model_request)

    def test_specialists_have_focused_toolsets(self):
        from adk.agent import root_agent
        by_name = {s.name: s for s in root_agent.sub_agents}
        # schema specialist has create_project_scaffold
        schema_tools = [getattr(t, "name", t.__name__) for t in by_name["schema_specialist"].tools]
        self.assertIn("create_project_scaffold", schema_tools)
        # deploy specialist has deploy_pbip_to_fabric
        deploy_tools = [getattr(t, "name", t.__name__) for t in by_name["deploy_specialist"].tools]
        self.assertIn("deploy_pbip_to_fabric", deploy_tools)
        # report specialist has finalize_pages_index
        report_tools = [getattr(t, "name", t.__name__) for t in by_name["report_specialist"].tools]
        self.assertIn("finalize_pages_index", report_tools)

    def test_pipeline_agent_is_sequential(self):
        from adk.agent import pipeline_agent
        from google.adk.agents import SequentialAgent
        self.assertIsInstance(pipeline_agent, SequentialAgent)
        names = [s.name for s in pipeline_agent.sub_agents]
        # Phase 8: planner/analyzer/cleaner/reviewer added to the pipeline.
        self.assertEqual(names, [
            "planner", "data_analyzer", "data_cleaner",
            "schema_specialist", "dax_specialist", "report_specialist",
            "report_reviewer",
        ])

    def test_sub_agents_have_distinct_parents(self):
        from adk.agent import root_agent, pipeline_agent
        root_names = {id(s) for s in root_agent.sub_agents}
        pipe_names = {id(s) for s in pipeline_agent.sub_agents}
        # factory-built: no shared instances between the two trees
        self.assertEqual(root_names & pipe_names, set())


# ---------------------------------------------------------------------------
# REPL integration — list/open with a real temp output dir
# ---------------------------------------------------------------------------


class TestReplIntegration(unittest.TestCase):

    def test_list_finds_projects(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Alpha.SemanticModel").mkdir()
            (root / "Alpha.Report").mkdir()
            (root / "Beta.SemanticModel").mkdir()
            (root / "Beta.Report").mkdir()
            repl = make_repl(events=[], output_root=root)
            out = repl.handle_slash("/list")
            self.assertIn("Alpha", out)
            self.assertIn("Beta", out)

    def test_list_marks_current_project(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Alpha.SemanticModel").mkdir()
            (root / "Alpha.Report").mkdir()
            repl = make_repl(events=[], output_root=root)
            repl.session.state["current_project"] = "Alpha"
            out = repl.handle_slash("/list")
            self.assertIn("← current", out)

    def test_open_finds_existing_project(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Alpha.SemanticModel").mkdir()
            (root / "Alpha.Report").mkdir()
            (root / "Alpha.pbip").write_text("{}", encoding="utf-8")
            repl = make_repl(events=[], output_root=root)
            out = repl.handle_slash("/open Alpha")
            self.assertIn("Alpha.pbip", out)

    def test_open_missing_project_reports_error(self):
        with tempfile.TemporaryDirectory() as td:
            repl = make_repl(events=[], output_root=td)
            out = repl.handle_slash("/open Ghost")
            self.assertIn("not found", out)

    def test_cmd_loop_exits_on_exit_command(self):
        outputs = []
        repl = make_repl(events=[])
        repl.cmd_loop(
            input_fn=_iter_input(["/exit"]),
            output_fn=outputs.append,
        )
        self.assertTrue(any("Goodbye" in o for o in outputs))

    def test_cmd_loop_sends_non_slash_to_run_turn(self):
        outputs = []
        repl = make_repl(events=[fake_event(text="Built it!", turn_complete=True)])
        # one real line then EOF → loop ends
        repl.cmd_loop(
            input_fn=_iter_input(["build a dashboard"]),
            output_fn=outputs.append,
        )
        # one user line → run_turn → "agent> Built it!"
        self.assertTrue(any("Built it!" in o for o in outputs))

    def test_cmd_loop_blank_lines_skipped(self):
        outputs = []
        repl = make_repl(events=[])
        repl.cmd_loop(
            input_fn=_iter_input(["", "   ", "/exit"]),
            output_fn=outputs.append,
        )
        # blank lines produce no prompt; /exit ends
        self.assertTrue(any("Goodbye" in o for o in outputs))


class TestHealthEndpoint(unittest.TestCase):
    """Health-check endpoint (adk/server.py) reports liveness."""

    def test_health_returns_ok(self):
        from fastapi.testclient import TestClient
        from adk.server import create_app
        client = TestClient(create_app())
        r = client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn(body["status"], ("ok", "degraded"))
        self.assertTrue(body["agent_loaded"])
        self.assertTrue(body["output_root_writable"])

    def test_health_not_shadowed_by_adk_own_health_route(self):
        """Regression: get_fast_api_app() registers its OWN bare-bones
        GET /health (google/adk/cli/api_server.py -- just {"status": "ok"}),
        added before adk.server's richer probe runs. Without explicitly
        removing that route first, Starlette's first-match-wins routing
        silently served ADK's minimal version forever, and the "real" probe
        (agent_loaded / output_root_writable / model) was dead code -- this
        only surfaced once adk/server.py actually mounted the full adk web
        app instead of a bare separate FastAPI() instance."""
        from fastapi.testclient import TestClient
        from adk.server import create_app
        client = TestClient(create_app())
        body = client.get("/health").json()
        self.assertIn("agent_loaded", body)
        self.assertIn("output_root_writable", body)
        self.assertIn("model", body)

    def test_chat_ui_is_actually_mounted(self):
        """Regression: adk/server.py used to build its own bare FastAPI()
        with only /health + A2A routes -- Docker's CMD (python -m
        adk.server) never actually served the adk web chat UI at all.
        Confirm the real dev-ui is reachable, not just liveness/A2A."""
        from fastapi.testclient import TestClient
        from adk.server import create_app
        client = TestClient(create_app())
        r = client.get("/", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers.get("content-type", ""))


class TestSessionPersistence(unittest.TestCase):
    """F1 — DatabaseSessionService keeps sessions across a new service instance."""

    def test_build_session_service_defaults_to_inmemory(self):
        from adk.chat import _build_session_service
        from google.adk.sessions import InMemorySessionService
        # No env override → in-memory
        svc = _build_session_service()
        self.assertIsInstance(svc, InMemorySessionService)

    def test_database_session_service_persists_across_instances(self):
        from google.adk.sessions import DatabaseSessionService
        from google.adk.events import Event
        from google.genai import types
        import os

        tmp = tempfile.mkdtemp().replace("\\", "/")
        url = f"sqlite+aiosqlite:///{tmp}/sess.db"
        svc = DatabaseSessionService(url)
        s = asyncio.run(svc.create_session(app_name="adk", user_id="u"))
        sid = s.id
        asyncio.run(svc.append_event(session=s, event=Event(
            author="u", content=types.Content(role="user", parts=[types.Part(text="hi")]),
        )))
        # A brand-new service instance pointed at the same DB file must see
        # the session + its event — i.e. persistence works across "restarts".
        svc2 = DatabaseSessionService(url)
        listed = asyncio.run(svc2.list_sessions(app_name="adk", user_id="u"))
        self.assertEqual(len(listed.sessions), 1)
        sess = asyncio.run(svc2.get_session(app_name="adk", user_id="u", session_id=sid))
        self.assertIsNotNone(sess)
        self.assertEqual(len(sess.events), 1)


if __name__ == "__main__":
    unittest.main()

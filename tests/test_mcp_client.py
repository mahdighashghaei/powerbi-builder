"""Tests for adk/mcp_client.py — the persistent MCP client used to route
``adk/tools/highlevel_tools.py::generate_pbip`` through a real client ->
subprocess-server round trip over the Model Context Protocol, instead of a
bare in-process import.

Covers:
  * a genuine round trip (spawns the real ``mcp_server.server`` subprocess)
  * session reuse across multiple calls (no re-spawn per call)
  * fail-safe behavior: a connection/protocol failure becomes a normal
    ``{"ok": False, ...}`` dict, never an unhandled exception or a hang
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import adk.mcp_client as mc  # noqa: E402


def _reset_client_state():
    mc._session = None
    mc._stack = None
    mc._loop = None
    mc._connect_lock = None


class TestRealRoundTrip(unittest.TestCase):
    """These spawn the actual mcp_server.server subprocess -- slower than a
    typical unit test (subprocess startup), but this is the whole point:
    confirming the MCP protocol is genuinely exercised, not just imported.

    All tests in this class share ONE event loop and ONE mcp_client session
    (set up in setUpClass) so the subprocess is spawned exactly once for the
    whole class, not once per test. Previously each test used its own
    ``asyncio.run()`` (a fresh event loop), which defeated the
    ``_loop is current_loop`` singleton guard in ``mcp_client._connect`` and
    spawned a fresh ``python -m mcp_server.server`` subprocess (full
    ``agents`` + ``pandas`` import) for every single test method."""

    @classmethod
    def setUpClass(cls):
        _reset_client_state()
        cls._loop = asyncio.new_event_loop()

    @classmethod
    def tearDownClass(cls):
        # Close the persistent MCP session (and its subprocess) on the same
        # loop that opened it, then tear the loop down.
        async def _cleanup():
            if mc._stack is not None:
                try:
                    await mc._stack.aclose()
                except Exception:
                    pass
        try:
            cls._loop.run_until_complete(_cleanup())
        finally:
            cls._loop.close()
            _reset_client_state()

    def _run(self, coro):
        """Run ``coro`` to completion on the class-level shared event loop.

        Using ``self._loop.run_until_complete`` (instead of ``asyncio.run``)
        keeps the loop alive across tests so the mcp_client singleton reuses
        its subprocess session instead of respawning per test."""
        return self._loop.run_until_complete(coro)

    def test_read_csv_schema_round_trip(self):
        result = self._run(mc.call_mcp_tool("read_csv_schema", csv_path="SampleData.csv"))
        self.assertTrue(result.get("ok"), result.get("message"))
        self.assertIn("schema", result.get("data", {}))

    def test_session_reused_across_calls(self):
        async def two_calls():
            await mc.call_mcp_tool("read_csv_schema", csv_path="SampleData.csv")
            first_session = mc._session
            await mc.call_mcp_tool("read_csv_schema", csv_path="SampleData.csv")
            second_session = mc._session
            return first_session, second_session

        first, second = self._run(two_calls())
        self.assertIsNotNone(first)
        self.assertIs(first, second)

    def test_unknown_tool_returns_error_dict_not_raise(self):
        result = self._run(mc.call_mcp_tool("this_tool_does_not_exist"))
        self.assertFalse(result.get("ok"))

    def test_reconnects_cleanly_across_separate_event_loops(self):
        """Regression: a session (and the anyio streams underneath it) is
        bound to the event loop it was created in. The real adk web
        process runs one continuous loop for its whole life, so this
        never bit production -- but any caller that wraps each call in
        its own asyncio.run() (a fresh loop every time, e.g. two
        separate test methods each doing asyncio.run(generate_pbip(...)))
        used to reuse a now-stale session and get an
        anyio.ClosedResourceError on the second call.

        NOTE: this test deliberately steps OUTSIDE the shared class loop
        (via bare asyncio.run) to exercise the reconnect path, so it
        spawns its own subprocess -- that is the behaviour under test."""
        # First call on a throwaway loop (not the shared class loop): this
        # builds a session bound to a loop we then discard.
        _reset_client_state()
        result1 = asyncio.run(mc.call_mcp_tool("read_csv_schema", csv_path="SampleData.csv"))
        self.assertTrue(result1.get("ok"), result1.get("message"))

        # A brand new asyncio.run() -- a different event loop than above.
        result2 = asyncio.run(mc.call_mcp_tool("read_csv_schema", csv_path="SampleData.csv"))
        self.assertTrue(result2.get("ok"), result2.get("message"))
        _reset_client_state()


class TestFailSafe(unittest.TestCase):
    """Connection/protocol failures must degrade to a normal error dict --
    never raise into the calling tool function, never hang indefinitely."""

    def setUp(self):
        _reset_client_state()

    def tearDown(self):
        _reset_client_state()

    def test_connect_failure_returns_error_dict(self):
        async def failing_connect():
            raise RuntimeError("subprocess could not start")

        with patch("adk.mcp_client._connect", side_effect=failing_connect):
            result = asyncio.run(mc.call_mcp_tool("read_csv_schema", csv_path="x.csv"))
        self.assertFalse(result["ok"])
        self.assertIn("MCP round-trip failed", result["message"])
        self.assertIn("subprocess could not start", result["errors"][0])

    def test_non_json_response_returns_error_dict(self):
        class _FakeContent:
            type = "text"
            text = "not json at all {{{"

        class _FakeResult:
            content = [_FakeContent()]
            isError = False

        async def fake_connect():
            class _FakeSession:
                async def call_tool(self, name, kwargs):
                    return _FakeResult()
            return _FakeSession()

        with patch("adk.mcp_client._connect", side_effect=fake_connect):
            result = asyncio.run(mc.call_mcp_tool("read_csv_schema", csv_path="x.csv"))
        self.assertFalse(result["ok"])
        self.assertIn("not valid JSON", result["message"])

    def test_empty_content_returns_error_dict(self):
        class _FakeResult:
            content = []
            isError = False

        async def fake_connect():
            class _FakeSession:
                async def call_tool(self, name, kwargs):
                    return _FakeResult()
            return _FakeSession()

        with patch("adk.mcp_client._connect", side_effect=fake_connect):
            result = asyncio.run(mc.call_mcp_tool("read_csv_schema", csv_path="x.csv"))
        self.assertFalse(result["ok"])
        self.assertIn("no text content", result["message"])


class TestEnvironmentInheritance(unittest.TestCase):
    """The subprocess is a trusted, local re-launch of THIS codebase, so it
    must inherit the full parent environment (not the MCP SDK's curated
    default subset) -- otherwise a caller blanking LLM_* env vars to force
    deterministic, no-network test behavior would silently have no effect."""

    def test_connect_passes_full_parent_environment(self):
        import os
        captured = {}

        class _FakeParams:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch("mcp.client.stdio.StdioServerParameters", _FakeParams), \
             patch.object(mc, "_session", None), \
             patch.object(mc, "_stack", None):
            with patch("mcp.client.stdio.stdio_client", side_effect=RuntimeError("stop here")):
                try:
                    asyncio.run(mc._connect())
                except RuntimeError:
                    pass

        self.assertIn("env", captured)
        self.assertEqual(captured["env"], dict(os.environ))


if __name__ == "__main__":
    unittest.main()

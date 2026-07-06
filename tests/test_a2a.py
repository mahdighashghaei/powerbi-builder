"""Tests for the A2A (Agent-to-Agent) protocol surface (Wave B1).

Verifies:
  * the Agent Card is served with the expected A2A shape.
  * the card lists the root agent + its sub-agents + skills (tools).
  * ``tasks/send`` executes a real build and returns a completed task.
  * ``tasks/send`` with an empty prompt returns a ``failed`` task (fail-safe).
  * ``tasks/get`` retrieves a previously-submitted task.

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


class TestAgentCard(unittest.TestCase):
    """The A2A Agent Card shape."""

    def test_card_has_required_a2a_fields(self):
        from adk.a2a import build_agent_card  # noqa: E402

        card = build_agent_card()
        self.assertEqual(card["protocol"], "A2A")
        self.assertIn("name", card)
        self.assertIn("description", card)
        self.assertIn("version", card)
        self.assertIn("capabilities", card)
        self.assertIn("skills", card)
        self.assertIn("subAgents", card)
        self.assertIn("defaultInputModes", card)

    def test_card_lists_sub_agents(self):
        from adk.a2a import build_agent_card  # noqa: E402

        card = build_agent_card()
        # The root agent has several specialist sub-agents.
        self.assertGreater(len(card["subAgents"]), 0)
        names = {s["name"] for s in card["subAgents"]}
        # At least one of the known specialists appears.
        known = {"planner_agent", "schema_agent", "dax_agent", "report_agent", "schema_specialist"}
        self.assertTrue(names & known, f"no known sub-agent in {names}")

    def test_card_skills_are_tool_names(self):
        from adk.a2a import build_agent_card  # noqa: E402

        card = build_agent_card()
        skill_ids = {s["id"] for s in card["skills"]}
        # The progressive-disclosure skill tools should appear.
        self.assertIn("list_skills", skill_ids)


class TestTaskHandling(unittest.TestCase):
    """A2A task send/get."""

    def _write_csv(self, path: Path) -> None:
        path.write_text(
            "OrderDate,Region,Product,Quantity,Amount\n"
            "2024-01-05,North,Widget,10,250.50\n"
            "2024-01-07,South,Gadget,5,99.99\n",
            encoding="utf-8",
        )

    def test_send_with_csv_builds_pbip(self):
        from adk import a2a  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            csv = Path(td) / "sales.csv"
            self._write_csv(csv)
            task = a2a.handle_task(
                {"task": {"message": {"parts": [{"type": "text", "text": str(csv)}]}}}
            )
            self.assertEqual(task["state"], "completed")
            self.assertIn("result", task)
            result = task["result"]
            self.assertTrue(result.get("ok"), f"build failed: {result}")
            # The agent reply message summarizes the build.
            agent_msg = task["messages"][-1]
            self.assertEqual(agent_msg["role"], "agent")
            self.assertTrue(agent_msg["parts"][0]["text"])
            # The task is retrievable.
            fetched = a2a.get_task(task["id"])
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched["id"], task["id"])

    def test_send_empty_prompt_fails_gracefully(self):
        from adk import a2a  # noqa: E402

        task = a2a.handle_task({"task": {"message": {"parts": [{"type": "text", "text": "   "}]}}})
        self.assertEqual(task["state"], "failed")
        self.assertIn("error", task)
        self.assertEqual(task["error"]["code"], "empty_prompt")

    def test_send_without_csv_path_fails_gracefully(self):
        from adk import a2a  # noqa: E402

        task = a2a.handle_task(
            {"task": {"message": {"parts": [{"type": "text", "text": "build me a dashboard"}]}}}
        )
        # No CSV path in the prompt -> execution_error (fail-safe, no exception).
        self.assertEqual(task["state"], "failed")
        self.assertIn("error", task)

    def test_task_has_id_and_timestamps(self):
        from adk import a2a  # noqa: E402

        task = a2a.handle_task(
            {"task": {"message": {"parts": [{"type": "text", "text": "hello.csv desc"}]}}}
        )
        self.assertTrue(task["id"])
        self.assertTrue(task["createdAt"])
        self.assertTrue(task["updatedAt"])


class TestServerRoutes(unittest.TestCase):
    """The A2A routes are mounted on the FastAPI app."""

    def test_app_has_a2a_routes(self):
        from adk.server import create_app  # noqa: E402

        app = create_app()
        paths = {r.path for r in app.routes}
        self.assertIn("/.well-known/agent-card.json", paths)
        self.assertIn("/a2a/tasks/send", paths)
        self.assertIn("/a2a/tasks/{task_id}", paths)
        # The health route is still present.
        self.assertIn("/health", paths)


if __name__ == "__main__":
    unittest.main()

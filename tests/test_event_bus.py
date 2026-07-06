"""Tests for the in-process event bus + state gateway (Wave D1).

Verifies:
  * EventBus pub/sub delivers events to subscribers.
  * Wildcard subscribers receive every event.
  * A handler error is swallowed (fail-safe — never breaks the bus).
  * Event history is recorded and filterable by topic.
  * StateGateway.set publishes state.changed events.
  * StateGateway snapshot/update/get/clear work.
  * The default bus/gateway singletons are resettable.
  * Agents publish agent.completed events on run (integration with BaseAgent).

Stdlib unittest — runs under ``python -m pytest tests/ -v``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.event_bus import EventBus, StateGateway, reset_default  # noqa: E402


class TestEventBus(unittest.TestCase):
    """Pub/sub delivery + fail-safe."""

    def test_publish_delivers_to_subscriber(self):
        bus = EventBus()
        received = []
        bus.subscribe("topic.x", lambda e: received.append(e.payload))
        bus.publish("topic.x", {"v": 1})
        self.assertEqual(received, [{"v": 1}])

    def test_wildcard_subscriber_receives_all(self):
        bus = EventBus()
        all_events = []
        bus.subscribe("*", lambda e: all_events.append(e.topic))
        bus.publish("a", {})
        bus.publish("b", {})
        self.assertEqual(all_events, ["a", "b"])

    def test_handler_error_swallowed(self):
        bus = EventBus()
        good = []
        bus.subscribe("t", lambda e: (_ for _ in ()).throw(ValueError("boom")))
        bus.subscribe("t", lambda e: good.append(e.payload))
        # The bad handler raises but the good one still gets the event.
        bus.publish("t", {"v": 1})
        self.assertEqual(good, [{"v": 1}])

    def test_history_recorded_and_filtered(self):
        bus = EventBus()
        bus.publish("a", {"1": 1})
        bus.publish("b", {"2": 2})
        bus.publish("a", {"3": 3})
        self.assertEqual(len(bus.history()), 3)
        self.assertEqual(len(bus.history("a")), 2)
        self.assertEqual(len(bus.history("b")), 1)

    def test_unsubscribe(self):
        bus = EventBus()
        got = []
        h = lambda e: got.append(e.payload)  # noqa: E731
        bus.subscribe("t", h)
        bus.publish("t", {"v": 1})
        bus.unsubscribe("t", h)
        bus.publish("t", {"v": 2})
        self.assertEqual(got, [{"v": 1}])

    def test_event_has_timestamp(self):
        bus = EventBus()
        e = bus.publish("t", {})
        self.assertTrue(e.timestamp)


class TestStateGateway(unittest.TestCase):
    """State store with change broadcasting."""

    def test_set_publishes_change(self):
        gw = StateGateway()
        changes = []
        gw.bus.subscribe(gw.STATE_TOPIC, lambda e: changes.append(e.payload))
        gw.set("project", "SalesDashboard")
        self.assertEqual(changes, [{"key": "project", "value": "SalesDashboard"}])

    def test_get_set_snapshot(self):
        gw = StateGateway()
        gw.set("a", 1)
        gw.set("b", 2)
        self.assertEqual(gw.get("a"), 1)
        self.assertEqual(gw.snapshot(), {"a": 1, "b": 2})

    def test_update_publishes_each_key(self):
        gw = StateGateway()
        n = []
        gw.bus.subscribe(gw.STATE_TOPIC, lambda e: n.append(e.payload["key"]))
        gw.update({"x": 1, "y": 2})
        self.assertEqual(n, ["x", "y"])

    def test_clear(self):
        gw = StateGateway()
        gw.set("a", 1)
        gw.clear()
        self.assertEqual(gw.snapshot(), {})


class TestDefaultBus(unittest.TestCase):
    """The module-level singletons."""

    def tearDown(self):
        reset_default()

    def test_default_bus_is_singleton(self):
        from utils.event_bus import default_bus  # noqa: E402

        self.assertIs(default_bus(), default_bus())

    def test_reset_clears_singleton(self):
        from utils.event_bus import default_bus, default_gateway  # noqa: E402

        b1 = default_bus()
        reset_default()
        b2 = default_bus()
        self.assertIsNot(b1, b2)


class TestAgentIntegration(unittest.TestCase):
    """BaseAgent publishes agent.completed events on the event bus."""

    def test_agent_run_publishes_event(self):
        from utils.event_bus import default_bus, reset_default  # noqa: E402

        reset_default()
        bus = default_bus()
        events = []
        bus.subscribe("agent.completed", lambda e: events.append(e.payload))

        # Run a real agent via the orchestrator on a tiny CSV.
        import tempfile  # noqa: E402

        from agents.orchestrator import OrchestratorAgent  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            csv = Path(td) / "s.csv"
            csv.write_text(
                "OrderDate,Region,Amount\n2024-01-05,North,100\n2024-01-07,South,200\n",
                encoding="utf-8",
            )
            out = Path(td) / "out"
            OrchestratorAgent(str(out)).run(
                source_path=str(csv), business_description="sales by region"
            )
        # The orchestrator runs several agents; each publishes an event.
        self.assertTrue(events)
        agent_names = {e["agent"] for e in events}
        # At least the planner + schema ran.
        self.assertIn("PlannerAgent", agent_names)


if __name__ == "__main__":
    unittest.main()

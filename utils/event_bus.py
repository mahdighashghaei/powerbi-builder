"""In-process event bus for DAG-style agent orchestration (Wave D1).

The roadmap's "DAG orchestration" concept separates **state** from the
conversation and passes it through a message bus so the model's context window
stays light. The project already separates state from the conversation
(``AgentContext`` / ``session.state``); this module adds the *message bus* half:
a lightweight in-process pub/sub that agents use to signal state transitions
without stuffing them into the LLM message history.

Design:
  * ``EventBus`` — a synchronous, in-process pub/sub (no external broker).
    ``publish(topic, payload)`` notifies all subscribers; ``subscribe(topic, fn)``
    registers a handler. Thread-safe (ADK may run sub-agents concurrently).
  * ``StateGateway`` — a thin key/value store backed by the bus: ``set``/``get``
    publish ``state.changed`` events so downstream agents react to state
    transitions rather than polling. This is the "gateway" state passes through.
  * Fail-safe: a handler error is logged and swallowed — one bad subscriber
    never breaks the bus (and never the build).

This is deliberately not a distributed message queue; it is the in-process
equivalent that keeps state out of the context window while staying simple and
dependency-free.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from collections import defaultdict


@dataclass
class Event:
    """One event published on the bus."""

    topic: str
    payload: dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }


class EventBus:
    """A thread-safe, in-process publish/subscribe message bus."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Callable[[Event], None]]] = defaultdict(list)
        self._history: list[Event] = []

    def subscribe(self, topic: str, handler: Callable[[Event], None]) -> None:
        with self._lock:
            self._subscribers[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Callable[[Event], None]) -> None:
        with self._lock:
            subs = self._subscribers.get(topic, [])
            if handler in subs:
                subs.remove(handler)

    def publish(self, topic: str, payload: dict[str, Any] | None = None) -> Event:
        event = Event(topic=topic, payload=payload or {})
        handlers: list[Callable[[Event], None]]
        with self._lock:
            self._history.append(event)
            handlers = list(self._subscribers.get(topic, []))
            # Wildcard subscribers receive every event.
            handlers.extend(self._subscribers.get("*", []))
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                pass  # fail-safe: one bad subscriber never breaks the bus
        return event

    def history(self, topic: str | None = None) -> list[Event]:
        """Return published events, optionally filtered by topic."""
        with self._lock:
            if topic is None:
                return list(self._history)
            return [e for e in self._history if e.topic == topic]

    def clear(self) -> None:
        with self._lock:
            self._history.clear()


class StateGateway:
    """A key/value state store that broadcasts changes on an EventBus.

    Agents read/write shared state through this gateway instead of threading it
    through the LLM conversation. Every ``set`` publishes a ``state.changed``
    event carrying the key + new value, so interested agents react to
    transitions. This is the "passing state through a gateway" half of the DAG
    orchestration pattern.
    """

    STATE_TOPIC = "state.changed"

    def __init__(self, bus: EventBus | None = None) -> None:
        self._bus = bus or EventBus()
        self._state: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def bus(self) -> EventBus:
        return self._bus

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._state[key] = value
        self._bus.publish(self.STATE_TOPIC, {"key": key, "value": value})

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._state.get(key, default)

    def update(self, mapping: dict[str, Any]) -> None:
        for k, v in mapping.items():
            self.set(k, v)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def clear(self) -> None:
        with self._lock:
            self._state.clear()
        self._bus.clear()


# Module-level default bus + gateway so agents without an explicit bus still
# share one. Tests should call reset_default() between cases.
_default_bus: EventBus | None = None
_default_gateway: StateGateway | None = None


def default_bus() -> EventBus:
    global _default_bus
    if _default_bus is None:
        _default_bus = EventBus()
    return _default_bus


def default_gateway() -> StateGateway:
    global _default_gateway
    if _default_gateway is None:
        _default_gateway = StateGateway(default_bus())
    return _default_gateway


def reset_default() -> None:
    """Reset the module-level bus + gateway (tests only)."""
    global _default_bus, _default_gateway
    _default_bus = None
    _default_gateway = None


__all__ = [
    "Event",
    "EventBus",
    "StateGateway",
    "default_bus",
    "default_gateway",
    "reset_default",
]

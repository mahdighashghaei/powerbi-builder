"""OpenTelemetry trajectory tracing for the powerbi-builder agent.

This module provides *trajectory evaluation* (Wave A4): instead of inspecting
only the final output, each agent + tool execution is recorded as an OpenTelemetry
span so the full *trajectory* of steps the agent took can be replayed, audited,
and evaluated.

Design — fail-safe by default:
  * If ``opentelemetry`` is not installed, every call here is a no-op (the
    ``NoopTracer`` returns no-op spans). This keeps the project installable and
    runnable without the telemetry dependency.
  * Telemetry is **off** unless ``POWERBI_OTEL_ENABLED=1`` is set. Even when on,
    a span failure never propagates — a broken tracer must never break a build.
  * Two exporters: ``file`` (default — writes a JSONL trajectory to
    ``POWERBI_OTEL_FILE``) and ``otlp`` (sends to an OTLP collector when
    ``POWERBI_OTEL_ENDPOINT`` is set). ``none`` disables export but still
    records spans in-memory for the ``get_trajectory`` tool.

Spans are recorded with a per-run ``trace_id`` (stored in ``session.state`` as
``otel_run_id``) so a whole build's trajectory can be retrieved as one trace.

Config (env vars):
  * ``POWERBI_OTEL_ENABLED``  — "1"/"0" (default "0").
  * ``POWERBI_OTEL_EXPORTER`` — "file" | "otlp" | "none" (default "file").
  * ``POWERBI_OTEL_FILE``     — trajectory JSONL path (default
    ``<output_root>/trajectory.jsonl``).
  * ``POWERBI_OTEL_ENDPOINT`` — OTLP collector URL (otlp exporter only).
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# sys.path bootstrap so adk.config is importable when ADK runs from adk/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Config (read lazily so .env is loaded by adk.config first)
# ---------------------------------------------------------------------------

def _otel_enabled() -> bool:
    return os.getenv("POWERBI_OTEL_ENABLED", "0") == "1"


def _exporter_kind() -> str:
    return os.getenv("POWERBI_OTEL_EXPORTER", "file").lower()


def _otel_file() -> Path:
    p = os.getenv("POWERBI_OTEL_FILE")
    if p:
        return Path(p)
    out = os.getenv("POWERBI_OUTPUT_ROOT") or os.getenv("OUTPUT_DIR", str(_ROOT / "output"))
    return Path(out) / "trajectory.jsonl"


# ---------------------------------------------------------------------------
# In-memory span store (always active when enabled, even with exporter=none)
# ---------------------------------------------------------------------------

class _SpanRecord:
    """A plain record of one span, independent of the OTel SDK types."""

    __slots__ = (
        "name", "kind", "trace_id", "span_id", "parent_id",
        "start_ns", "end_ns", "attributes", "status", "events",
    )

    def __init__(self, name: str, kind: str, trace_id: str, span_id: str,
                 parent_id: str | None) -> None:
        self.name = name
        self.kind = kind
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_id = parent_id
        self.start_ns = _now_ns()
        self.end_ns: int | None = None
        self.attributes: dict[str, Any] = {}
        self.status: str = "ok"
        self.events: list[dict[str, Any]] = []

    def set_attr(self, key: str, value: Any) -> None:
        try:
            json.dumps(value, default=str)
            self.attributes[key] = value
        except (TypeError, ValueError):
            self.attributes[key] = str(value)

    def add_event(self, name: str, attrs: dict[str, Any] | None = None) -> None:
        self.events.append({"name": name, "attrs": attrs or {}, "ts_ns": _now_ns()})

    def end(self, status: str = "ok") -> None:
        self.end_ns = _now_ns()
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "duration_ms": ((self.end_ns - self.start_ns) / 1e6) if self.end_ns else None,
            "attributes": self.attributes,
            "status": self.status,
            "events": self.events,
        }


def _now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1e9)


class _TrajectoryStore:
    """Thread-safe in-memory store of span records keyed by trace_id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: dict[str, list[_SpanRecord]] = {}

    def add(self, span: _SpanRecord) -> None:
        with self._lock:
            self._spans.setdefault(span.trace_id, []).append(span)

    def get(self, trace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [s.to_dict() for s in self._spans.get(trace_id, [])]

    def all_runs(self) -> list[str]:
        with self._lock:
            return list(self._spans.keys())

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


# Module-level singleton — one store per process.
_TRAJECTORY = _TrajectoryStore()


# ---------------------------------------------------------------------------
# Span context manager
# ---------------------------------------------------------------------------

class _Span:
    """A span handle: records start/end + attributes, registers in the store,
    and exports to the configured exporter on close."""

    def __init__(self, record: _SpanRecord) -> None:
        self._record = record
        self._ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        try:
            self._record.set_attr(key, value)
        except Exception:
            pass  # never break a build over a span attribute

    def add_event(self, name: str, attrs: dict[str, Any] | None = None) -> None:
        try:
            self._record.add_event(name, attrs)
        except Exception:
            pass

    def record_exception(self, exc: BaseException) -> None:
        try:
            self._record.status = "error"
            self._record.add_event("exception", {"type": type(exc).__name__, "msg": str(exc)})
        except Exception:
            pass

    def end(self, status: str = "ok") -> None:
        if self._ended:
            return
        self._ended = True
        try:
            self._record.end(status)
            _TRAJECTORY.add(self._record)
            _export_span(self._record)
        except Exception:
            pass  # fail-safe


@contextlib.contextmanager
def span(name: str, kind: str = "tool", *, trace_id: str | None = None,
         parent_id: str | None = None, attributes: dict[str, Any] | None = None) -> Iterator[_Span]:
    """Open a telemetry span as a context manager.

    Usage::

        with telemetry.span("tool.write_tmdl", attributes={"project": "P"}) as s:
            ... do work ...
            s.set_attribute("rows", 42)
        # span auto-closes on block exit

    If telemetry is disabled, this yields a no-op span (still usable as a
    context manager + ``set_attribute``/``add_event`` are safe no-ops).
    """
    s = start_span(name, kind=kind, trace_id=trace_id, parent_id=parent_id, attributes=attributes)
    try:
        yield s
        if not isinstance(s, _NoopSpan):
            s.end("ok")
    except BaseException as exc:  # record + re-raise — never swallow
        if not isinstance(s, _NoopSpan):
            s.record_exception(exc)
            s.end("error")
        raise


def start_span(name: str, kind: str = "tool", *, trace_id: str | None = None,
               parent_id: str | None = None,
               attributes: dict[str, Any] | None = None) -> _Span | _NoopSpan:
    """Open a telemetry span and return its handle (no context manager).

    Use this when you need to start a span in one callback and end it in another
    (e.g. the ADK plugin's before/after tool callbacks). The caller MUST call
    ``span.end()`` (and may call ``set_attribute`` / ``add_event`` /
    ``record_exception`` in between). Returns a no-op span when telemetry is
    disabled, so all handle methods are safe no-ops.
    """
    if not _otel_enabled():
        return _NoopSpan()
    tid = trace_id or _current_trace_id() or _new_trace_id()
    sid = uuid.uuid4().hex[:16]
    record = _SpanRecord(name=name, kind=kind, trace_id=tid, span_id=sid, parent_id=parent_id)
    if attributes:
        for k, v in attributes.items():
            record.set_attr(k, v)
    s = _Span(record)
    _set_current_trace_id(tid)
    return s


class _NoopSpan:
    """A no-op span returned when telemetry is disabled."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def add_event(self, name: str, attrs: dict[str, Any] | None = None) -> None:
        pass

    def record_exception(self, exc: BaseException) -> None:
        pass

    def end(self, status: str = "ok") -> None:
        pass


# ---------------------------------------------------------------------------
# Per-run trace id (thread-local)
# ---------------------------------------------------------------------------

_trace_local = threading.local()


def _new_trace_id() -> str:
    tid = uuid.uuid4().hex
    _trace_local.trace_id = tid
    return tid


def _current_trace_id() -> str | None:
    return getattr(_trace_local, "trace_id", None)


def _set_current_trace_id(tid: str) -> None:
    _trace_local.trace_id = tid


def start_run() -> str:
    """Begin a new telemetry run and return its trace id.

    Store this in ``session.state["otel_run_id"]`` so the run's trajectory can
    be retrieved later via :func:`get_trajectory`. Safe to call when telemetry
    is disabled (returns a generated id but records nothing)."""
    tid = _new_trace_id()
    return tid


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------

_export_lock = threading.Lock()


def _export_span(record: _SpanRecord) -> None:
    kind = _exporter_kind()
    if kind == "none":
        return
    if kind == "file":
        try:
            with _export_lock:
                path = _otel_file()
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record.to_dict(), default=str) + "\n")
        except Exception:
            pass  # fail-safe
    elif kind == "otlp":
        _export_otlp(record)


def _export_otlp(record: _SpanRecord) -> None:
    """Export a span to an OTLP collector via the OpenTelemetry SDK.

    Best-effort: if the SDK or collector is unavailable, the export is skipped
    (the in-memory store still keeps the record for ``get_trajectory``).
    """
    try:
        from opentelemetry import trace as otel_trace  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore
            OTLPSpanExporter,
        )
        endpoint = os.getenv("POWERBI_OTEL_ENDPOINT")
        if not endpoint:
            return
        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        otel_trace.set_tracer_provider(provider)
        tracer = otel_trace.get_tracer("powerbi-builder")
        with tracer.start_as_current_span(record.name) as sp:
            sp.set_attribute("powerbi.kind", record.kind)
            for k, v in record.attributes.items():
                try:
                    sp.set_attribute(f"powerbi.{k}", v)
                except Exception:
                    pass
    except Exception:
        pass  # fail-safe — in-memory store still has the record


# ---------------------------------------------------------------------------
# Trajectory retrieval (used by the get_trajectory ADK tool)
# ---------------------------------------------------------------------------

def get_trajectory(trace_id: str | None = None) -> dict[str, Any]:
    """Return the recorded trajectory (list of spans) for a run.

    If ``trace_id`` is omitted, the most recent run's trajectory is returned.
    Each span dict carries name, kind, start/end, duration, attributes, status,
    and events — enough to replay/evaluate the agent's step-by-step actions.
    """
    if trace_id is None:
        runs = _TRAJECTORY.all_runs()
        if not runs:
            return {"ok": True, "trace_id": None, "spans": [], "count": 0}
        trace_id = runs[-1]
    spans = _TRAJECTORY.get(trace_id)
    return {
        "ok": True,
        "trace_id": trace_id,
        "spans": spans,
        "count": len(spans),
    }


def list_runs() -> list[str]:
    """Return all recorded run trace ids (most-recent last)."""
    return _TRAJECTORY.all_runs()


def reset_for_tests() -> None:
    """Clear the in-memory trajectory store (tests only)."""
    _TRAJECTORY.clear()
    _trace_local.__dict__.pop("trace_id", None)


__all__ = [
    "span",
    "start_span",
    "start_run",
    "get_trajectory",
    "list_runs",
    "reset_for_tests",
]

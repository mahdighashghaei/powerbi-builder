"""Defensive wrappers around google-genai types (API drift guard).

The ``google.genai.types`` module evolves: attribute names like
``Blob.display_name``, the structure of ``inline_data``, and ``Event.error_code``
have changed across releases. Accessing them directly means a genai upgrade can
break the code silently.

This module centralises every fragile attribute access behind a small helper
with a sensible fallback, so a missing/renamed attribute degrades gracefully
instead of raising ``AttributeError`` mid-turn.
"""
from __future__ import annotations

from typing import Any


def blob_display_name(blob: Any) -> str:
    """Return the display name of a ``types.Blob``, or ``""`` if absent.

    Handles both ``display_name`` (current) and a possible ``file_name`` rename.
    """
    for attr in ("display_name", "file_name", "name"):
        val = getattr(blob, attr, None)
        if val:
            return str(val)
    return ""


def blob_mime_type(blob: Any) -> str:
    """Return the MIME type of a ``types.Blob``, or ``""`` if absent.

    Handles ``mime_type`` (current) and the older ``mime_type``/``mime`` naming.
    """
    for attr in ("mime_type", "mime"):
        val = getattr(blob, attr, None)
        if val:
            return str(val)
    return ""


def blob_data(blob: Any) -> bytes | None:
    """Return the raw bytes of a ``types.Blob``, or ``None``."""
    return getattr(blob, "data", None)


def part_inline_data(part: Any) -> Any:
    """Return the ``inline_data`` blob of a ``types.Part``, or ``None``.

    Handles ``inline_data`` (current) and a possible ``inline_blob`` rename.
    """
    return getattr(part, "inline_data", None) or getattr(part, "inline_blob", None)


def part_text(part: Any) -> str:
    """Return the text of a ``types.Part``, or ``""`` if it has none."""
    return getattr(part, "text", None) or ""


def event_error_code(event: Any) -> str:
    """Return an event's error code, or ``""`` if the event is not an error.

    Handles ``error_code`` (current) and a possible ``error.code`` nesting.
    """
    code = getattr(event, "error_code", None)
    if code:
        return str(code)
    err = getattr(event, "error", None)
    if err is not None:
        return str(getattr(err, "code", "") or "")
    return ""


def event_error_message(event: Any) -> str:
    """Return an event's error message, or ``""`` if the event is not an error."""
    msg = getattr(event, "error_message", None)
    if msg:
        return str(msg)
    err = getattr(event, "error", None)
    if err is not None:
        return str(getattr(err, "message", "") or "")
    return ""


def event_is_error(event: Any) -> bool:
    """True if the event represents an error state."""
    return bool(event_error_code(event) or event_error_message(event))

"""Retry / backoff helpers for Gemini (and other LLM) API calls.

Gemini API calls can fail transiently with:
  * 429 RESOURCE_EXHAUSTED  — rate limit hit
  * 503 SERVICE_UNAVAILABLE — temporary overload
  * 500 INTERNAL            — server-side blip
  * google.api_core.exceptions.RetryError — wrapped transient failure

These helpers centralise the "is this worth retrying?" decision and an
exponential-backoff loop, so callers don't hand-roll sleeps. The ADK Runner
drives the model itself, so :func:`is_retryable_error` is also used by the
``on_model_error_callback`` in :mod:`adk.agent` to log a clear, actionable
message and apply a short backoff before the run resumes.
"""
from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

# Substrings that mark an error as transient (worth retrying). Matched
# case-insensitively against the stringified error / its repr.
_RETRYABLE_MARKERS = (
    "429",
    "resource_exhausted",
    "rate limit",
    "rate_limit",
    "quota",
    "503",
    "service_unavailable",
    "unavailable",
    "500",
    "internal",  # google internal — cautious; retried only a few times
    "deadline_exceeded",
    "timeout",
    "connection reset",
    "temporarily",
    "retryable",
)

# Errors that look transient but should NOT be retried indefinitely.
_NON_RETRYABLE_MARKERS = (
    "invalid_argument",
    "invalid_api_key",
    "permission_denied",
    "not_found",
    "unauthorized",
)


def is_retryable_error(error: BaseException) -> bool:
    """Heuristically decide whether ``error`` is worth retrying.

    Inspects the stringified error (and any ``__cause__``) for rate-limit /
    transient markers, while excluding clearly-permanent errors (bad key,
    invalid argument). Returns ``False`` when in doubt — failing fast is
    safer than looping on a permanent error.
    """
    text = f"{error!r} {error}".lower()
    # Permanent errors are never retryable, even if they mention a marker.
    if any(m in text for m in _NON_RETRYABLE_MARKERS):
        # 429 with an invalid key is still permanent.
        return False
    return any(m in text for m in _RETRYABLE_MARKERS)


async def retry_async(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    **kwargs: Any,
) -> T:
    """Call ``await func(*args, **kwargs)`` with exponential backoff.

    Retries only on :func:`is_retryable_error` failures; a non-retryable
    error is raised immediately. Backoff is ``base_delay * 2**attempt``
    capped at ``max_delay``, with a small jitter.
    """
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not is_retryable_error(exc) or attempt == retries:
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            # jitter: up to 25% of the delay, to avoid thundering herds
            jitter = delay * 0.25 * (time.monotonic() % 1)
            wait = delay + jitter
            time.sleep(wait)  # async-safe enough for short waits in REPL
    # unreachable, but keeps mypy happy
    assert last_exc is not None
    raise last_exc


def retry_sync(
    func: Callable[..., T],
    *args: Any,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    **kwargs: Any,
) -> T:
    """Sync variant of :func:`retry_async` for non-async call sites."""
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not is_retryable_error(exc) or attempt == retries:
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            jitter = delay * 0.25 * (time.monotonic() % 1)
            time.sleep(delay + jitter)
    assert last_exc is not None
    raise last_exc


def retryable(
    *,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator: wrap a sync callable in :func:`retry_sync`.

    Usage::

        @retryable(retries=4)
        def call_gemini(prompt: str) -> str: ...
    """

    def deco(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return retry_sync(
                func, *args, retries=retries,
                base_delay=base_delay, max_delay=max_delay, **kwargs,
            )

        return wrapper

    return deco

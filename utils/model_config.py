"""utils/model_config.py — Centralized LLM provider/model configuration.

Single source of truth for "which LLM provider and model should this run
use" across both entry points:

  * the deterministic ``agents/*.py`` pipeline (``main.py``), where 5 call
    sites (``BIReasoningAgent``, ``MeasureSelectorAgent``, ``PlannerAgent``,
    ``VisualPlannerAgent``, ``utils/llm_client.py``) *optionally* enhance an
    otherwise fully-offline deterministic result with an LLM call;
  * the ADK interactive layer (``adk/agent.py``'s 10-agent architecture),
    where every agent is constructed as ``Agent(model=...)``.

Before this module, both layers hardcoded Google Gemini via ad-hoc
``os.getenv("GOOGLE_API_KEY")`` checks and direct ``google.genai.Client()``
calls, with no path to Anthropic or OpenAI. This module replaces that with
one resolution function (:func:`get_llm_config`) and two consumption
helpers — :func:`get_adk_model` for the ADK layer, :func:`get_text_completion`
for the pipeline layer — backed by ``litellm`` so adding a new provider is a
one-line addition to :data:`_PROVIDER_DEFAULTS`, not a rewrite.

Fail-loud vs. fail-safe (deliberately different from the rest of this
codebase's "always degrade silently" convention, and intentionally so):

* :func:`get_llm_config` raises :class:`MissingAPIKeyError` ONLY when a
  provider was *explicitly* requested (``LLM_PROVIDER`` set) and its API key
  isn't available — an explicit ask that silently degrades to "as if nothing
  was configured" would hide a real misconfiguration from the user.
* When no provider is requested at all, resolution auto-detects from
  whichever legacy key is present (preserving today's behavior exactly) and
  returns ``None`` when nothing is configured — the fully-deterministic/
  offline mode every agent already handles.
* Callers decide how loud to be with that exception: the ADK layer (agents
  built once at process startup) lets it propagate; the pipeline's optional-
  LLM agents catch it, log it as an error (visible, not swallowed), and fall
  back to their existing deterministic path — preserving every agent's
  documented "LLM is optional, never crashes the build" contract.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

try:
    # This module is the single source of truth for provider/model
    # resolution and must work standalone (e.g. a fresh script that imports
    # only utils.model_config, without ever importing config.py or
    # adk/config.py first) — so it loads .env itself rather than relying on
    # another module's import side-effect. python-dotenv does not override
    # already-set environment variables, so calling this again from
    # config.py/adk/config.py is a harmless no-op.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

# ---------------------------------------------------------------------------
# Provider catalog — add a new provider by adding one entry here.
# ---------------------------------------------------------------------------

_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "google": {
        "env": "GOOGLE_API_KEY",
        # gemini-2.5-flash, not 2.0-flash: 2.0-flash has been observed to
        # return quota limit 0 ("429 RESOURCE_EXHAUSTED ... limit: 0") on
        # some free-tier keys (see adk/config.py's original note).
        "default_model": "gemini-2.5-flash",
        "litellm_prefix": "gemini",
    },
    "anthropic": {
        "env": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-5",
        "litellm_prefix": "anthropic",
    },
    "openai": {
        "env": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "litellm_prefix": "openai",
    },
}

# Auto-detect order when LLM_PROVIDER is not explicitly set — deliberately
# preserves the EXACT legacy config.py priority (Google, then OpenAI) and
# excludes Anthropic on purpose: ANTHROPIC_API_KEY is commonly present
# ambiently in any environment where Claude Code itself is running (it's the
# CLI's own credential, inherited by every subprocess this project spawns),
# so silently auto-detecting it here would activate this project's LLM
# features for anyone running it via Claude Code, whether or not they
# intended to. Anthropic must be opted into explicitly via LLM_PROVIDER=anthropic.
_AUTO_DETECT_ORDER = ("google", "openai")


class MissingAPIKeyError(RuntimeError):
    """Raised when a provider was explicitly requested but its key is absent.

    Deliberately loud — see module docstring. Never raised for the "nothing
    configured at all" case, only for an explicit-but-broken configuration.
    """


@dataclass(frozen=True)
class LLMConfig:
    """Resolved provider + model for the current run."""

    provider: str            # "google" | "anthropic" | "openai" | ...
    model: str                # provider-native model id, e.g. "claude-sonnet-5"
    litellm_model: str        # "anthropic/claude-sonnet-5" — for litellm.completion()
    api_key: str
    api_base: str | None = None  # e.g. a corporate/proxy gateway endpoint


def _resolve_model_name(provider: str) -> str:
    """LLM_MODEL (provider-agnostic) > legacy override > provider default.

    ``POWERBI_MODEL``/``MODEL_NAME`` are legacy env vars that historically
    only ever meant "which Gemini model" (the only provider that existed).
    They must NOT apply to a non-google provider — e.g. a ``.env`` with
    ``POWERBI_MODEL=gemini-2.5-flash`` (a perfectly normal legacy setting)
    combined with ``LLM_PROVIDER=anthropic`` must resolve to Anthropic's own
    default model, not the nonsensical ``anthropic/gemini-2.5-flash``.
    """
    explicit = os.getenv("LLM_MODEL", "").strip()
    if explicit:
        return explicit
    if provider == "google":
        legacy = os.getenv("POWERBI_MODEL", "").strip() or os.getenv("MODEL_NAME", "").strip()
        if legacy:
            return legacy
    return _PROVIDER_DEFAULTS[provider]["default_model"]


def _build_config(provider: str, api_key: str) -> LLMConfig:
    model = _resolve_model_name(provider)
    prefix = _PROVIDER_DEFAULTS[provider]["litellm_prefix"]
    env_name = _PROVIDER_DEFAULTS[provider]["env"]
    # Honor a custom endpoint (e.g. <PROVIDER>_BASE_URL) when the API key is
    # scoped to a gateway/proxy rather than the provider's public API —
    # without this, a valid-looking key can still fail auth against the
    # wrong endpoint.
    api_base = os.getenv(f"{env_name.removesuffix('_API_KEY')}_BASE_URL", "").strip() or None
    return LLMConfig(
        provider=provider,
        model=model,
        litellm_model=f"{prefix}/{model}",
        api_key=api_key,
        api_base=api_base,
    )


def get_llm_config() -> LLMConfig | None:
    """Resolve the active LLM provider + model from the environment.

    Returns:
        An :class:`LLMConfig`, or ``None`` when no provider is configured at
        all (fully-deterministic/offline mode — the historical default).

    Raises:
        MissingAPIKeyError: an explicit ``LLM_PROVIDER`` was set but the
            corresponding API key environment variable is empty/unset, or
            ``LLM_PROVIDER`` names a provider this module doesn't know about.
    """
    explicit_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit_provider:
        if explicit_provider not in _PROVIDER_DEFAULTS:
            raise MissingAPIKeyError(
                f"LLM_PROVIDER={explicit_provider!r} is not a known provider "
                f"(known: {sorted(_PROVIDER_DEFAULTS)}). Add it to "
                "utils/model_config.py._PROVIDER_DEFAULTS, or unset LLM_PROVIDER "
                "to auto-detect from whichever API key is present."
            )
        env_name = _PROVIDER_DEFAULTS[explicit_provider]["env"]
        key = os.getenv(env_name, "").strip()
        if not key:
            raise MissingAPIKeyError(
                f"LLM_PROVIDER={explicit_provider!r} was explicitly requested "
                f"but {env_name} is not set. Set {env_name}, or unset "
                "LLM_PROVIDER to fall back to auto-detection / offline mode."
            )
        return _build_config(explicit_provider, key)

    # No explicit provider — legacy auto-detect (backward compatible).
    for provider in _AUTO_DETECT_ORDER:
        env_name = _PROVIDER_DEFAULTS[provider]["env"]
        key = os.getenv(env_name, "").strip()
        if key:
            return _build_config(provider, key)

    return None  # nothing configured — deterministic/offline mode


# ---------------------------------------------------------------------------
# ADK layer consumption — value for Agent(model=...)
# ---------------------------------------------------------------------------


def get_adk_model(config: LLMConfig | None = None) -> Any:
    """Return the value to pass as ADK ``Agent(model=...)``.

    ``provider="google"`` returns the bare model string — ADK's native path,
    zero behavior change from before this module existed. Any other
    provider returns a lazily-imported ``LiteLlm`` instance. When no config
    is resolved (offline mode) falls back to the google default model string
    so ``adk/agent.py`` always receives a usable value (ADK requires a model
    at agent-construction time; there is no "no LLM" mode for that layer).
    """
    if config is None:
        config = get_llm_config()
    if config is None:
        return _PROVIDER_DEFAULTS["google"]["default_model"]
    if config.provider == "google":
        return config.model
    from google.adk.models.lite_llm import LiteLlm  # heavy import — lazy
    kwargs: dict[str, Any] = {}
    if config.api_base:
        kwargs["api_base"] = config.api_base
    return LiteLlm(model=config.litellm_model, **kwargs)


# ---------------------------------------------------------------------------
# Pipeline layer consumption — provider-agnostic single-turn completion
# ---------------------------------------------------------------------------


def get_text_completion(
    prompt: str, config: LLMConfig, timeout: float | None = None,
) -> str:
    """Single-turn text completion via ``litellm.completion()``.

    Raises on failure (network error, bad key, provider outage) — callers
    keep their existing retry/fallback wrapping (``utils.retry.retry_sync``
    + a catch-all that returns the deterministic result) exactly as before;
    this function does not swallow errors itself.

    ``timeout`` (seconds) is optional and omitted by default — the primary
    reasoning call sites (BI reasoning, planning, measure/visual selection)
    don't pass one, unchanged. Short-lived escalation call sites (e.g. the
    semantic-assist layer in DataAnalyzerAgent/DataCleanerAgent) pass a low
    value so an unresponsive provider can't stall the fast deterministic
    pipeline they sit alongside.
    """
    import litellm  # heavy import — lazy, matches the rest of this codebase

    kwargs: dict[str, Any] = {}
    if config.api_base:
        kwargs["base_url"] = config.api_base
    if timeout is not None:
        kwargs["timeout"] = timeout
    response = litellm.completion(
        model=config.litellm_model,
        messages=[{"role": "user", "content": prompt}],
        api_key=config.api_key,
        **kwargs,
    )
    return response.choices[0].message.content or ""


__all__ = [
    "LLMConfig",
    "MissingAPIKeyError",
    "get_llm_config",
    "get_adk_model",
    "get_text_completion",
]

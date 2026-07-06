"""tests/test_model_config.py — Unit tests for the centralized LLM provider
config (``utils/model_config.py``).

Covers:
  - get_llm_config resolution order (explicit LLM_PROVIDER > auto-detect > None)
  - the deliberate exclusion of Anthropic from auto-detection (ANTHROPIC_API_KEY
    is commonly present ambiently in any Claude-Code-driven environment and
    must never silently activate this project's LLM features)
  - MissingAPIKeyError firing only for an explicit-but-broken configuration
  - model name precedence, including the regression this session found
    (legacy POWERBI_MODEL/MODEL_NAME must not leak into a non-google provider)
  - api_base resolution from <PROVIDER>_BASE_URL
  - get_adk_model (bare string for google, LiteLlm instance otherwise)
  - get_text_completion (mocked litellm.completion — no real network calls)

Every test fully isolates the relevant environment variables so it is immune
to whatever is actually configured in this machine's .env / ambient shell.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

# Import once at module scope (before any test's setUp() clears env vars).
# utils.model_config calls load_dotenv() as one-time module-level code on
# first import — importing lazily inside a test body would let that first
# import silently reload GOOGLE_API_KEY etc. from .env AFTER setUp() already
# cleared them, defeating the isolation below.
import utils.model_config  # noqa: F401

_ENV_KEYS = (
    "LLM_PROVIDER", "LLM_MODEL", "POWERBI_MODEL", "MODEL_NAME",
    "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "GOOGLE_BASE_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
)


class _EnvIsolatedTestCase(unittest.TestCase):
    """Snapshots + fully clears the relevant env vars for every test, then
    restores the original values — immune to whatever this machine's real
    .env/ambient shell happens to have configured."""

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestGetLlmConfigResolution(_EnvIsolatedTestCase):
    def test_nothing_configured_returns_none(self):
        from utils.model_config import get_llm_config

        self.assertIsNone(get_llm_config())

    def test_auto_detect_google(self):
        from utils.model_config import get_llm_config

        os.environ["GOOGLE_API_KEY"] = "g-key"
        cfg = get_llm_config()
        self.assertEqual(cfg.provider, "google")
        self.assertEqual(cfg.api_key, "g-key")
        self.assertEqual(cfg.litellm_model, "gemini/gemini-2.5-flash")

    def test_auto_detect_prefers_google_over_openai(self):
        from utils.model_config import get_llm_config

        os.environ["GOOGLE_API_KEY"] = "g-key"
        os.environ["OPENAI_API_KEY"] = "o-key"
        cfg = get_llm_config()
        self.assertEqual(cfg.provider, "google")

    def test_auto_detect_falls_back_to_openai(self):
        from utils.model_config import get_llm_config

        os.environ["OPENAI_API_KEY"] = "o-key"
        cfg = get_llm_config()
        self.assertEqual(cfg.provider, "openai")

    def test_auto_detect_never_picks_up_anthropic(self):
        """The key regression this session found: ANTHROPIC_API_KEY is
        commonly present ambiently (it's the Claude Code CLI's own
        credential, inherited by every subprocess) and must never silently
        activate this project's LLM features without an explicit ask."""
        from utils.model_config import get_llm_config

        os.environ["ANTHROPIC_API_KEY"] = "sneaky-ambient-key"
        self.assertIsNone(get_llm_config())

    def test_explicit_provider_anthropic_works_when_key_present(self):
        from utils.model_config import get_llm_config

        os.environ["LLM_PROVIDER"] = "anthropic"
        os.environ["ANTHROPIC_API_KEY"] = "a-key"
        cfg = get_llm_config()
        self.assertEqual(cfg.provider, "anthropic")
        self.assertEqual(cfg.litellm_model, "anthropic/claude-sonnet-5")

    def test_explicit_provider_missing_key_raises_loudly(self):
        from utils.model_config import MissingAPIKeyError, get_llm_config

        os.environ["LLM_PROVIDER"] = "openai"
        with self.assertRaises(MissingAPIKeyError):
            get_llm_config()

    def test_explicit_unknown_provider_raises(self):
        from utils.model_config import MissingAPIKeyError, get_llm_config

        os.environ["LLM_PROVIDER"] = "mistral"
        with self.assertRaises(MissingAPIKeyError):
            get_llm_config()

    def test_explicit_provider_case_insensitive(self):
        from utils.model_config import get_llm_config

        os.environ["LLM_PROVIDER"] = "  Anthropic  "
        os.environ["ANTHROPIC_API_KEY"] = "a-key"
        cfg = get_llm_config()
        self.assertEqual(cfg.provider, "anthropic")


class TestModelNamePrecedence(_EnvIsolatedTestCase):
    def test_llm_model_overrides_everything(self):
        from utils.model_config import get_llm_config

        os.environ["GOOGLE_API_KEY"] = "g-key"
        os.environ["LLM_MODEL"] = "custom-model"
        cfg = get_llm_config()
        self.assertEqual(cfg.model, "custom-model")
        self.assertEqual(cfg.litellm_model, "gemini/custom-model")

    def test_legacy_powerbi_model_applies_to_google(self):
        from utils.model_config import get_llm_config

        os.environ["GOOGLE_API_KEY"] = "g-key"
        os.environ["POWERBI_MODEL"] = "gemini-legacy-override"
        cfg = get_llm_config()
        self.assertEqual(cfg.model, "gemini-legacy-override")

    def test_legacy_powerbi_model_does_not_leak_into_other_providers(self):
        """Regression: a .env with POWERBI_MODEL=gemini-2.5-flash (a normal
        legacy Google-only setting) combined with LLM_PROVIDER=anthropic
        must resolve to Anthropic's own default, never
        'anthropic/gemini-2.5-flash'."""
        from utils.model_config import get_llm_config

        os.environ["LLM_PROVIDER"] = "anthropic"
        os.environ["ANTHROPIC_API_KEY"] = "a-key"
        os.environ["POWERBI_MODEL"] = "gemini-2.5-flash"
        cfg = get_llm_config()
        self.assertEqual(cfg.provider, "anthropic")
        self.assertNotIn("gemini", cfg.model)
        self.assertEqual(cfg.litellm_model, "anthropic/claude-sonnet-5")

    def test_legacy_model_name_also_google_only(self):
        from utils.model_config import get_llm_config

        os.environ["LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "o-key"
        os.environ["MODEL_NAME"] = "gemini-2.0-flash"
        cfg = get_llm_config()
        self.assertNotIn("gemini", cfg.model)


class TestApiBaseResolution(_EnvIsolatedTestCase):
    def test_api_base_resolved_from_provider_env(self):
        from utils.model_config import get_llm_config

        os.environ["LLM_PROVIDER"] = "anthropic"
        os.environ["ANTHROPIC_API_KEY"] = "a-key"
        os.environ["ANTHROPIC_BASE_URL"] = "https://my-gateway.example.com"
        cfg = get_llm_config()
        self.assertEqual(cfg.api_base, "https://my-gateway.example.com")

    def test_no_base_url_leaves_api_base_none(self):
        from utils.model_config import get_llm_config

        os.environ["GOOGLE_API_KEY"] = "g-key"
        cfg = get_llm_config()
        self.assertIsNone(cfg.api_base)


class TestGetAdkModel(_EnvIsolatedTestCase):
    def test_google_returns_bare_string(self):
        from utils.model_config import get_adk_model, get_llm_config

        os.environ["GOOGLE_API_KEY"] = "g-key"
        value = get_adk_model(get_llm_config())
        self.assertIsInstance(value, str)
        self.assertEqual(value, "gemini-2.5-flash")

    def test_anthropic_returns_lite_llm_instance(self):
        from utils.model_config import get_adk_model, get_llm_config

        os.environ["LLM_PROVIDER"] = "anthropic"
        os.environ["ANTHROPIC_API_KEY"] = "a-key"
        value = get_adk_model(get_llm_config())
        from google.adk.models.lite_llm import LiteLlm
        self.assertIsInstance(value, LiteLlm)
        self.assertEqual(value.model, "anthropic/claude-sonnet-5")

    def test_offline_falls_back_to_google_default_string(self):
        from utils.model_config import get_adk_model

        value = get_adk_model(None)
        self.assertEqual(value, "gemini-2.5-flash")

    def test_resolves_config_itself_when_not_provided(self):
        from utils.model_config import get_adk_model

        os.environ["GOOGLE_API_KEY"] = "g-key"
        value = get_adk_model()
        self.assertEqual(value, "gemini-2.5-flash")


class TestGetTextCompletion(_EnvIsolatedTestCase):
    def test_calls_litellm_completion_with_resolved_model(self):
        from utils.model_config import LLMConfig, get_text_completion

        config = LLMConfig(provider="anthropic", model="claude-sonnet-5",
                            litellm_model="anthropic/claude-sonnet-5", api_key="a-key")
        fake_response = MagicMock()
        fake_response.choices = [MagicMock(message=MagicMock(content="hello"))]
        with patch("litellm.completion", return_value=fake_response) as mock_completion:
            result = get_text_completion("test prompt", config)
        self.assertEqual(result, "hello")
        mock_completion.assert_called_once()
        _, kwargs = mock_completion.call_args
        self.assertEqual(kwargs["model"], "anthropic/claude-sonnet-5")
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "test prompt"}])
        self.assertEqual(kwargs["api_key"], "a-key")
        self.assertNotIn("base_url", kwargs)

    def test_passes_api_base_when_set(self):
        from utils.model_config import LLMConfig, get_text_completion

        config = LLMConfig(provider="anthropic", model="claude-sonnet-5",
                            litellm_model="anthropic/claude-sonnet-5", api_key="a-key",
                            api_base="https://gateway.example.com")
        fake_response = MagicMock()
        fake_response.choices = [MagicMock(message=MagicMock(content="ok"))]
        with patch("litellm.completion", return_value=fake_response) as mock_completion:
            get_text_completion("prompt", config)
        _, kwargs = mock_completion.call_args
        self.assertEqual(kwargs["base_url"], "https://gateway.example.com")

    def test_propagates_exceptions(self):
        """Callers keep their existing retry/fallback wrapping — this
        function must not swallow errors itself."""
        from utils.model_config import LLMConfig, get_text_completion

        config = LLMConfig(provider="anthropic", model="claude-sonnet-5",
                            litellm_model="anthropic/claude-sonnet-5", api_key="a-key")
        with patch("litellm.completion", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                get_text_completion("prompt", config)


if __name__ == "__main__":
    unittest.main()

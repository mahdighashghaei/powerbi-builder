"""tests/test_semantic_llm_assist.py — Optional LLM-assisted semantic
decision layer for DataAnalyzerAgent / DataCleanerAgent.

"Advisor, not executor": the LLM never touches raw data or executes
cleaning; it only proposes a decision consumed by the existing
deterministic executors. Covers:
  - agents.schemas.CleaningStrategyDecision / SemanticInterpretation
  - agents.data_cleaner_agent._heuristic_role_confidence /
    _get_cleaning_strategy_llm
  - agents.data_analyzer_agent._get_semantic_interpretation_llm
  - Feature-flag-off zero-footprint guarantee on both agents
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.model_config import LLMConfig

# Pre-import (module scope, before any test patches get_llm_config): the
# first import of adk.tools.data_cleaning_tools transitively imports
# adk/config.py, whose module-level `MODEL_NAME = get_adk_model()` calls
# get_llm_config() as a one-time side effect. Importing it now means that
# side effect fires with the REAL get_llm_config, not a test's mock.
import adk.tools.data_cleaning_tools  # noqa: E402,F401

_FAKE_CONFIG = LLMConfig(
    provider="anthropic", model="claude-sonnet-5",
    litellm_model="anthropic/claude-sonnet-5", api_key="fake-key",
)


def _settings(**overrides):
    from config import Settings
    return Settings(**overrides)


class TestNewSchemas(unittest.TestCase):
    def test_cleaning_strategy_decision_defaults(self):
        from agents.schemas import CleaningStrategyDecision

        d = CleaningStrategyDecision(column="x")
        self.assertEqual(d.semantic_role, "ambiguous")
        self.assertEqual(d.null_handling_strategy, "leave_as_is")
        self.assertEqual(d.source, "deterministic")

    def test_semantic_interpretation_defaults(self):
        from agents.schemas import SemanticInterpretation

        s = SemanticInterpretation()
        self.assertEqual(s.column_roles, [])
        self.assertEqual(s.source, "deterministic")

    def test_column_role_guess_defaults_to_unknown(self):
        from agents.schemas import ColumnRoleGuess

        self.assertEqual(ColumnRoleGuess(column_name="c").likely_role, "unknown")


class TestHeuristicRoleConfidence(unittest.TestCase):
    def test_non_numeric_is_confident(self):
        from agents.data_cleaner_agent import _heuristic_role_confidence

        self.assertEqual(_heuristic_role_confidence("Segment", "string"), 1.0)

    def test_identifier_token_is_confident(self):
        from agents.data_cleaner_agent import _heuristic_role_confidence

        self.assertGreaterEqual(_heuristic_role_confidence("zip_code", "int64"), 0.9)
        self.assertGreaterEqual(_heuristic_role_confidence("account_id", "int64"), 0.9)

    def test_measure_keyword_is_fairly_confident(self):
        from agents.data_cleaner_agent import _heuristic_role_confidence

        self.assertGreaterEqual(_heuristic_role_confidence("total_amount", "double"), 0.8)

    def test_generic_numeric_name_is_ambiguous(self):
        from agents.data_cleaner_agent import _heuristic_role_confidence

        self.assertLess(_heuristic_role_confidence("col_7", "int64"), 0.6)

    def test_never_raises_on_bad_input(self):
        from agents.data_cleaner_agent import _heuristic_role_confidence

        self.assertEqual(_heuristic_role_confidence(None, "int64"), 1.0)  # type: ignore[arg-type]


class TestGetCleaningStrategyLLM(unittest.TestCase):
    _PROFILE = {"null_pct": 20, "distinct_count": 50, "unique_pct": 40,
                "distinct_values": []}

    def test_flag_off_returns_none_without_calling_llm(self):
        from agents.data_cleaner_agent import _get_cleaning_strategy_llm

        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=False)), \
             patch("utils.model_config.get_llm_config", side_effect=AssertionError("must not be called")):
            result = _get_cleaning_strategy_llm("value_7", self._PROFILE, "int64", "")
        self.assertIsNone(result)

    def test_no_llm_configured_returns_none(self):
        from agents.data_cleaner_agent import _get_cleaning_strategy_llm

        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True)), \
             patch("utils.model_config.get_llm_config", return_value=None):
            result = _get_cleaning_strategy_llm("value_7", self._PROFILE, "int64", "")
        self.assertIsNone(result)

    def test_missing_api_key_error_returns_none(self):
        from agents.data_cleaner_agent import _get_cleaning_strategy_llm
        from utils.model_config import MissingAPIKeyError

        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True)), \
             patch("utils.model_config.get_llm_config", side_effect=MissingAPIKeyError("nope")):
            result = _get_cleaning_strategy_llm("value_7", self._PROFILE, "int64", "")
        self.assertIsNone(result)

    def test_high_confidence_success_returns_decision(self):
        from agents.data_cleaner_agent import _get_cleaning_strategy_llm

        fake_text = (
            '{"semantic_role": "numeric_code", "null_handling_strategy": '
            '"fill_mode", "confidence": 0.9, "reasoning": "looks like an id"}'
        )
        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True,
                                                          semantic_llm_confidence_threshold=0.6)), \
             patch("utils.model_config.get_llm_config", return_value=_FAKE_CONFIG), \
             patch("utils.model_config.get_text_completion", return_value=fake_text):
            result = _get_cleaning_strategy_llm("account_num", self._PROFILE, "int64", "")
        self.assertIsNotNone(result)
        self.assertEqual(result.semantic_role, "numeric_code")
        self.assertEqual(result.null_handling_strategy, "fill_mode")
        self.assertEqual(result.source, "llm")

    def test_low_confidence_is_discarded(self):
        from agents.data_cleaner_agent import _get_cleaning_strategy_llm

        fake_text = (
            '{"semantic_role": "numeric_measure", "null_handling_strategy": '
            '"fill_mean", "confidence": 0.2, "reasoning": "not sure"}'
        )
        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True,
                                                          semantic_llm_confidence_threshold=0.6)), \
             patch("utils.model_config.get_llm_config", return_value=_FAKE_CONFIG), \
             patch("utils.model_config.get_text_completion", return_value=fake_text):
            result = _get_cleaning_strategy_llm("value_7", self._PROFILE, "int64", "")
        self.assertIsNone(result)

    def test_call_failure_returns_none(self):
        """Synthetic failure at OUR wrapper boundary (get_text_completion
        itself raises) -- covers the generic exception-handling path.
        See test_real_litellm_timeout_propagates_and_is_caught below for a
        genuine timeout simulated one layer deeper, at litellm.completion."""
        from agents.data_cleaner_agent import _get_cleaning_strategy_llm

        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True)), \
             patch("utils.model_config.get_llm_config", return_value=_FAKE_CONFIG), \
             patch("utils.model_config.get_text_completion", side_effect=TimeoutError("slow")):
            result = _get_cleaning_strategy_llm("value_7", self._PROFILE, "int64", "")
        self.assertIsNone(result)

    def test_real_litellm_timeout_propagates_and_is_caught(self):
        """Simulate a genuine timeout at the litellm.completion() layer
        itself (litellm's own ``Timeout`` exception class), leaving
        ``utils.model_config.get_text_completion`` UNMOCKED so this also
        proves the ``timeout=12`` kwarg our caller passes actually reaches
        ``litellm.completion`` — not just that some exception, anywhere,
        gets swallowed."""
        import litellm
        from agents.data_cleaner_agent import _get_cleaning_strategy_llm

        seen_kwargs: dict = {}

        def _raise_timeout(*args, **kwargs):
            seen_kwargs.update(kwargs)
            raise litellm.exceptions.Timeout(
                message="Request timed out after 12s.",
                model=_FAKE_CONFIG.litellm_model,
                llm_provider=_FAKE_CONFIG.provider,
            )

        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True)), \
             patch("utils.model_config.get_llm_config", return_value=_FAKE_CONFIG), \
             patch("litellm.completion", side_effect=_raise_timeout):
            result = _get_cleaning_strategy_llm("col_7", self._PROFILE, "int64", "")

        self.assertIsNone(result)
        self.assertEqual(seen_kwargs.get("timeout"), 12)

    def test_malformed_json_returns_none(self):
        from agents.data_cleaner_agent import _get_cleaning_strategy_llm

        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True)), \
             patch("utils.model_config.get_llm_config", return_value=_FAKE_CONFIG), \
             patch("utils.model_config.get_text_completion", return_value="not json at all"):
            result = _get_cleaning_strategy_llm("value_7", self._PROFILE, "int64", "")
        self.assertIsNone(result)

    def test_invalid_enum_values_clamp_to_safe_defaults(self):
        from agents.data_cleaner_agent import _get_cleaning_strategy_llm

        fake_text = (
            '{"semantic_role": "not_a_real_role", "null_handling_strategy": '
            '"delete_everything", "confidence": 0.9, "reasoning": "x"}'
        )
        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True,
                                                          semantic_llm_confidence_threshold=0.6)), \
             patch("utils.model_config.get_llm_config", return_value=_FAKE_CONFIG), \
             patch("utils.model_config.get_text_completion", return_value=fake_text):
            result = _get_cleaning_strategy_llm("value_7", self._PROFILE, "int64", "")
        self.assertIsNotNone(result)
        self.assertEqual(result.semantic_role, "ambiguous")
        self.assertEqual(result.null_handling_strategy, "leave_as_is")


class TestGetSemanticInterpretationLLM(unittest.TestCase):
    _PROFILE = {
        "schema": {"columns": [{"name": "Sales", "dataType": "double"},
                                {"name": "Region", "dataType": "string"}]},
        "quality": {"columns": {
            "Sales": {"null_pct": 0, "distinct_count": 100, "unique_pct": 90},
            "Region": {"null_pct": 0, "distinct_count": 4, "unique_pct": 4,
                       "distinct_values": ["East", "West", "North", "South"]},
        }},
    }

    def test_flag_off_returns_none_without_calling_llm(self):
        from agents.data_analyzer_agent import _get_semantic_interpretation_llm

        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=False)), \
             patch("utils.model_config.get_llm_config", side_effect=AssertionError("must not be called")):
            result = _get_semantic_interpretation_llm(self._PROFILE, "")
        self.assertIsNone(result)

    def test_success_returns_interpretation_with_roles(self):
        from agents.data_analyzer_agent import _get_semantic_interpretation_llm

        fake_text = (
            '{"business_domain_guess": "retail sales", "column_roles": ['
            '{"column_name": "Sales", "likely_role": "fact_measure"}, '
            '{"column_name": "Region", "likely_role": "dimension_key"}], '
            '"confidence": 0.9, "reasoning": "clear retail pattern"}'
        )
        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True,
                                                          semantic_llm_confidence_threshold=0.6)), \
             patch("utils.model_config.get_llm_config", return_value=_FAKE_CONFIG), \
             patch("utils.model_config.get_text_completion", return_value=fake_text):
            result = _get_semantic_interpretation_llm(self._PROFILE, "retail dashboard")
        self.assertIsNotNone(result)
        self.assertEqual(result.business_domain_guess, "retail sales")
        self.assertEqual(len(result.column_roles), 2)
        self.assertEqual(result.column_roles[0].likely_role, "fact_measure")
        self.assertEqual(result.source, "llm")

    def test_low_confidence_is_discarded(self):
        from agents.data_analyzer_agent import _get_semantic_interpretation_llm

        fake_text = '{"business_domain_guess": "?", "column_roles": [], "confidence": 0.1, "reasoning": ""}'
        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True,
                                                          semantic_llm_confidence_threshold=0.6)), \
             patch("utils.model_config.get_llm_config", return_value=_FAKE_CONFIG), \
             patch("utils.model_config.get_text_completion", return_value=fake_text):
            result = _get_semantic_interpretation_llm(self._PROFILE, "")
        self.assertIsNone(result)

    def test_invalid_role_clamps_to_unknown(self):
        from agents.data_analyzer_agent import _get_semantic_interpretation_llm

        fake_text = (
            '{"business_domain_guess": "x", "column_roles": ['
            '{"column_name": "Sales", "likely_role": "made_up_role"}], '
            '"confidence": 0.9, "reasoning": ""}'
        )
        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True,
                                                          semantic_llm_confidence_threshold=0.6)), \
             patch("utils.model_config.get_llm_config", return_value=_FAKE_CONFIG), \
             patch("utils.model_config.get_text_completion", return_value=fake_text):
            result = _get_semantic_interpretation_llm(self._PROFILE, "")
        self.assertEqual(result.column_roles[0].likely_role, "unknown")

    def test_malformed_json_returns_none(self):
        from agents.data_analyzer_agent import _get_semantic_interpretation_llm

        with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True)), \
             patch("utils.model_config.get_llm_config", return_value=_FAKE_CONFIG), \
             patch("utils.model_config.get_text_completion", return_value="nonsense"):
            result = _get_semantic_interpretation_llm(self._PROFILE, "")
        self.assertIsNone(result)


class TestDataCleanerAgentIntegration(unittest.TestCase):
    """End-to-end through DataCleanerAgent._run() with a real CSV file."""

    def _make_ctx(self, tmp: str, csv_path: Path):
        from agents.base import AgentContext
        from mcp_server.server import PbipToolbox

        return AgentContext(
            business_description="ambiguous numeric column test",
            source_path=csv_path,
            toolbox=PbipToolbox(tmp),
            project_name="TestProject",
            pbip_root=Path(tmp) / "TestProject",
        )

    def _write_ambiguous_csv(self, tmp: str) -> Path:
        import pandas as pd

        # "col_7" is numeric, generic-named (ambiguous, matches no keyword
        # hint at all), ~20% null -- exactly the escalation trigger
        # (0 < null_pct <= 60, low heuristic confidence). "Region" is
        # non-numeric, never escalates.
        n = 50
        col = [float(i) for i in range(n)]
        for i in range(0, n, 5):
            col[i] = None
        df = pd.DataFrame({
            "col_7": col,
            "Region": (["East", "West"] * (n // 2)),
        })
        path = Path(tmp) / "data.csv"
        df.to_csv(path, index=False)
        return path

    def test_flag_off_zero_footprint(self):
        from agents.data_analyzer_agent import DataAnalyzerAgent
        from agents.data_cleaner_agent import DataCleanerAgent

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = self._write_ambiguous_csv(tmp)
            ctx = self._make_ctx(tmp, csv_path)

            with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=False)), \
                 patch("utils.model_config.get_llm_config",
                       side_effect=AssertionError("must not be called")):
                analyzer_result = DataAnalyzerAgent(ctx).run()
                self.assertTrue(analyzer_result.ok, analyzer_result.message)
                self.assertNotIn("semantic_interpretation", ctx.extra)

                cleaner_result = DataCleanerAgent(ctx).run()
                self.assertTrue(cleaner_result.ok, cleaner_result.message)
                self.assertNotIn("cleaning_strategy_decisions", ctx.extra)

    def test_flag_on_llm_decision_overrides_answer(self):
        from agents.data_analyzer_agent import DataAnalyzerAgent
        from agents.data_cleaner_agent import DataCleanerAgent

        fake_text = (
            '{"semantic_role": "numeric_measure", "null_handling_strategy": '
            '"fill_mean", "confidence": 0.95, "reasoning": "looks like a score"}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = self._write_ambiguous_csv(tmp)
            ctx = self._make_ctx(tmp, csv_path)

            with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=False)):
                analyzer_result = DataAnalyzerAgent(ctx).run()
            self.assertTrue(analyzer_result.ok, analyzer_result.message)

            with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True,
                                                              semantic_llm_confidence_threshold=0.6)), \
                 patch("utils.model_config.get_llm_config", return_value=_FAKE_CONFIG), \
                 patch("utils.model_config.get_text_completion", return_value=fake_text):
                cleaner_result = DataCleanerAgent(ctx).run()

            self.assertTrue(cleaner_result.ok, cleaner_result.message)
            decisions = ctx.extra.get("cleaning_strategy_decisions")
            self.assertIsNotNone(decisions)
            col_7_decision = next(d for d in decisions if d["column"] == "col_7")
            self.assertEqual(col_7_decision["source"], "llm")
            self.assertTrue(col_7_decision["escalated"])
            self.assertEqual(col_7_decision["semantic_role"], "numeric_measure")
            # the cleaning report's applied actions should reflect a mean impute
            report = ctx.extra["cleaning_report"]
            self.assertTrue(any("col_7" in a and "mean" in a for a in report["actions_applied"]))

    def test_high_confidence_column_logged_without_llm_call(self):
        """A column the heuristic is confident about must still appear in
        cleaning_strategy_decisions with escalated=False -- otherwise
        there's no way to tell, after the fact, WHY a given column never
        reached the LLM (as opposed to it silently never being considered)."""
        import pandas as pd
        from agents.data_analyzer_agent import DataAnalyzerAgent
        from agents.data_cleaner_agent import DataCleanerAgent

        with tempfile.TemporaryDirectory() as tmp:
            n = 50
            amount_col = [float(i) for i in range(n)]
            for i in range(0, n, 5):
                amount_col[i] = None
            # "total_amount" confidently matches the measure-keyword hints
            # ("amount"/"total") -- heuristic confidence >= threshold, so
            # this must resolve WITHOUT ever calling the LLM.
            df = pd.DataFrame({
                "total_amount": amount_col,
                "Region": (["East", "West"] * (n // 2)),
            })
            csv_path = Path(tmp) / "data.csv"
            df.to_csv(csv_path, index=False)
            ctx = self._make_ctx(tmp, csv_path)

            with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=False)):
                DataAnalyzerAgent(ctx).run()

            with patch("config.DEFAULT_SETTINGS", _settings(semantic_llm_assist_enabled=True,
                                                              semantic_llm_confidence_threshold=0.6)), \
                 patch("utils.model_config.get_llm_config",
                       side_effect=AssertionError("must not be called for a confident column")):
                cleaner_result = DataCleanerAgent(ctx).run()

            self.assertTrue(cleaner_result.ok, cleaner_result.message)
            decisions = ctx.extra.get("cleaning_strategy_decisions")
            self.assertIsNotNone(decisions)
            amount_decision = next(d for d in decisions if d["column"] == "total_amount")
            self.assertEqual(amount_decision["source"], "deterministic_confident")
            self.assertFalse(amount_decision["escalated"])
            self.assertIsNone(amount_decision["semantic_role"])
            self.assertGreaterEqual(amount_decision["confidence"], 0.6)


if __name__ == "__main__":
    unittest.main()

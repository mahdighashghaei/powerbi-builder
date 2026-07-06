"""Phase 1 tests — Pydantic output schemas + session.state sync + llm_client.

These tests verify the *infrastructure* added in Phase 1 without changing the
pipeline's external behaviour (which is why the Phase 0 snapshots must still
match — that is checked by ``test_phase0_e2e_semantic`` running unchanged).

Covers:
  * every Pydantic schema in ``agents/schemas.py`` validates its real-world
    shape (a measure/relationship/validation produced by the deterministic
    agents round-trips through the model);
  * ``AgentContext.sync_to_state`` / ``load_from_state`` mirror the typed
    fields into ``session_state`` and back (the source-of-truth contract);
  * ``BaseAgent.run`` syncs state after every agent (no manual call needed);
  * ``refine_relationships`` is a no-op returning the heuristic *unchanged*
    when no API key is set (so the offline baseline stays byte-identical) and
    *enriches* with confidence_score/source_reasoning only when the LLM ran.

Run with::

    python -m pytest tests/test_phase1_schemas_state.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class TestPydanticSchemas(unittest.TestCase):
    """Each schema must validate a representative real-world payload."""

    def test_measure_set_with_rationale(self):
        from agents.schemas import Measure, MeasureSet

        ms = MeasureSet(measures=[
            Measure(name="Total Sales", expression="SUM(Sales[Amount])",
                    table="Sales", displayFolder="Revenue",
                    formatString="$ #,##0.00", rationale="core revenue KPI"),
            Measure(name="Order Count", expression="COUNTROWS('Sales')",
                    table="Sales", displayFolder="Orders",
                    formatString="#,##0"),
        ])
        self.assertEqual(ms.count, 2)
        self.assertEqual(ms.measures[0].rationale, "core revenue KPI")

    def test_relationship_set_with_confidence(self):
        from agents.schemas import Relationship, RelationshipSet

        rs = RelationshipSet(relationships=[
            Relationship(from_table="Orders", from_column="CustomerId",
                         to_table="Customers", to_column="Id",
                         confidence_score=0.9, source_reasoning="FK by name"),
        ], table_count=2)
        self.assertEqual(len(rs.relationships), 1)
        self.assertEqual(rs.relationships[0].confidence_score, 0.9)

    def test_validation_result_with_routing(self):
        from agents.schemas import ValidationIssue, ValidationResult

        vr = ValidationResult(
            ok=False, tables=1, measures=5, pages=1, visuals=4,
            issues=[
                ValidationIssue(severity="error", message="ghost ref",
                                agent_responsible="MeasureSelectorAgent",
                                suggested_fix="remove measure X"),
            ],
        )
        self.assertFalse(vr.ok)
        self.assertEqual(vr.issues[0].agent_responsible, "MeasureSelectorAgent")

    def test_build_plan(self):
        from agents.schemas import BuildPlan, PlanStep

        plan = BuildPlan(steps=[
            PlanStep(phase="analyze", agent="DataAnalyzerAgent", action="profile"),
            PlanStep(phase="dax", agent="DAXAgent", action="build measures"),
        ], needs_cleaning=True, report_style="rich")
        self.assertEqual(plan.step_count, 2)
        self.assertIn("DAXAgent", plan.agents)

    def test_report_plan_counts(self):
        from agents.schemas import PagePlan, ReportPlan, VisualPlan

        rp = ReportPlan(pages=[
            PagePlan(id="summary-page", displayName="Summary", visuals=[
                VisualPlan(name="card-primary", kind="card", measure="Total Sales"),
                VisualPlan(name="bar-region", kind="barChart", category="Region",
                           measure="Total Sales"),
            ]),
        ])
        self.assertEqual(rp.page_count, 1)
        self.assertEqual(rp.visual_count, 2)

    def test_schema_result_roundtrips_existing_dict(self):
        from agents.schemas import ColumnSpec, SchemaResult, TableSpec

        # mirrors what SchemaAgent produces
        sr = SchemaResult(
            table_name="Sales",
            columns=[ColumnSpec(name="Amount", dataType="double", summarizeBy="sum")],
            all_tables=[TableSpec(table_name="Sales",
                                  columns=[ColumnSpec(name="Amount", dataType="double")])],
        )
        self.assertEqual(sr.all_tables[0].table_name, "Sales")

    def test_data_profile_defaults(self):
        from agents.schemas import DataProfile

        dp = DataProfile(quality_score=95.0)
        # defaults must be safe-empty so a partial offline profile validates
        self.assertEqual(dp.issues, [])
        self.assertTrue(dp.verified)


class TestAgentContextStateSync(unittest.TestCase):
    """sync_to_state / load_from_state round-trip the typed context fields."""

    def _make_context(self):
        from agents.base import AgentContext
        from mcp_server.server import PbipToolbox

        tmp = tempfile.mkdtemp()
        return AgentContext(
            business_description="test sales dashboard",
            source_path=Path(tmp) / "data.csv",
            toolbox=PbipToolbox(tmp),
            project_name="TestProject",
            pbip_root=Path(tmp),
        )

    def test_sync_to_state_writes_all_keys(self):
        ctx = self._make_context()
        ctx.schema = {"table_name": "Sales", "columns": [{"name": "Amount"}]}
        ctx.measures = [{"name": "Total Sales"}]
        ctx.pages = [{"id": "summary-page"}]
        ctx.validation = {"ok": True}
        ctx.extra["relationships"] = [{"from_table": "A", "to_table": "B"}]

        state = ctx.sync_to_state()
        for key in ("schema", "measures", "pages", "validation",
                    "relationships", "business_description", "project_name"):
            self.assertIn(key, state, f"sync_to_state missing {key}")
        self.assertEqual(state["measures"], [{"name": "Total Sales"}])
        self.assertEqual(state["relationships"], [{"from_table": "A", "to_table": "B"}])

    def test_load_from_state_restores_fields(self):
        ctx = self._make_context()
        ctx.session_state = {
            "schema": {"table_name": "Orders", "columns": []},
            "measures": [{"name": "Order Count"}],
            "pages": [{"id": "p1"}],
            "validation": {"ok": False},
            "relationships": [{"from_table": "X", "to_table": "Y"}],
            "plan": [{"phase": "dax"}],
        }
        ctx.load_from_state()
        self.assertEqual(ctx.schema["table_name"], "Orders")
        self.assertEqual(ctx.measures, [{"name": "Order Count"}])
        self.assertEqual(ctx.pages, [{"id": "p1"}])
        self.assertEqual(ctx.extra["relationships"], [{"from_table": "X", "to_table": "Y"}])
        self.assertEqual(ctx.extra["plan"], [{"phase": "dax"}])

    def test_load_from_state_ignores_missing_keys(self):
        ctx = self._make_context()
        ctx.schema = {"table_name": "Keep"}
        ctx.session_state = {"measures": [{"name": "M"}]}  # no schema key
        ctx.load_from_state()
        # schema untouched (no key in state), measures updated
        self.assertEqual(ctx.schema["table_name"], "Keep")
        self.assertEqual(ctx.measures, [{"name": "M"}])


class TestBaseAgentSyncsState(unittest.TestCase):
    """BaseAgent.run must sync state after _run, even on success and failure."""

    def test_syncs_on_success(self):
        from agents.base import AgentContext, BaseAgent
        from mcp_server.server import PbipToolbox

        tmp = tempfile.mkdtemp()
        ctx = AgentContext(
            business_description="x", source_path=Path(tmp) / "d.csv",
            toolbox=PbipToolbox(tmp), project_name="P", pbip_root=Path(tmp),
        )

        class Dummy(BaseAgent):
            name = "Dummy"
            description = "d"
            def _run(self):
                self.context.schema = {"table_name": "Written"}
                from agents.base import AgentResult
                return AgentResult(agent=self.name, ok=True, message="ok")

        Dummy(ctx).run()
        # sync_to_state ran after _run, so session_state reflects the schema
        self.assertEqual(ctx.session_state["schema"], {"table_name": "Written"})

    def test_syncs_on_crash(self):
        from agents.base import AgentContext, BaseAgent
        from mcp_server.server import PbipToolbox

        tmp = tempfile.mkdtemp()
        ctx = AgentContext(
            business_description="x", source_path=Path(tmp) / "d.csv",
            toolbox=PbipToolbox(tmp), project_name="P", pbip_root=Path(tmp),
        )

        class Crasher(BaseAgent):
            name = "Crasher"
            description = "c"
            def _run(self):
                self.context.measures = [{"name": "BeforeCrash"}]
                raise RuntimeError("boom")

        result = Crasher(ctx).run()
        self.assertFalse(result.ok)  # crash captured, not raised
        # state still synced with the mutation made before the crash
        self.assertEqual(ctx.session_state["measures"], [{"name": "BeforeCrash"}])


class TestLlmClientOffline(unittest.TestCase):
    """refine_relationships must be a clean no-op without an API key."""

    def setUp(self) -> None:
        os.environ["GOOGLE_API_KEY"] = ""

    def test_returns_heuristic_unchanged_offline(self):
        from utils.llm_client import refine_relationships

        tables = [{"table_name": "Orders", "columns": [{"name": "CustomerId"}]},
                  {"table_name": "Customers", "columns": [{"name": "Id"}]}]
        heuristic = [{"from_table": "Orders", "from_column": "CustomerId",
                      "to_table": "Customers", "to_column": "Id",
                      "to_cardinality": "one"}]
        out = refine_relationships(tables, heuristic)
        # offline → exact same list object (byte-identical baseline)
        self.assertIs(out, heuristic)
        # no extra keys added offline
        self.assertEqual(set(out[0].keys()), set(heuristic[0].keys()))

    def test_typed_variant_offline(self):
        from utils.llm_client import refine_relationships_typed

        tables = [{"table_name": "A", "columns": [{"name": "BId"}]},
                  {"table_name": "B", "columns": [{"name": "Id"}]}]
        heuristic = [{"from_table": "A", "from_column": "BId",
                      "to_table": "B", "to_column": "Id"}]
        rs = refine_relationships_typed(tables, heuristic)
        self.assertEqual(len(rs.relationships), 1)
        # offline fallback keeps default confidence (1.0) + heuristic reasoning
        self.assertEqual(rs.relationships[0].confidence_score, 1.0)

    def test_enriches_when_llm_succeeds(self):
        """When the LLM returns valid JSON, confidence_score/source_reasoning
        are populated and the heuristic can be pruned/extended."""
        from utils.llm_client import refine_relationships
        from utils.model_config import LLMConfig

        tables = [{"table_name": "Orders", "columns": [{"name": "CustomerId"}]},
                  {"table_name": "Customers", "columns": [{"name": "Id"}]}]
        heuristic = [{"from_table": "Orders", "from_column": "CustomerId",
                      "to_table": "Customers", "to_column": "Id",
                      "to_cardinality": "one"}]
        fake_config = LLMConfig(provider="anthropic", model="claude-sonnet-5",
                                 litellm_model="anthropic/claude-sonnet-5", api_key="fake-key")
        fake_text = '''[
            {"from_table": "Orders", "from_column": "CustomerId",
             "to_table": "Customers", "to_column": "Id", "to_cardinality": "one",
             "confidence_score": 0.95, "source_reasoning": "name match"}
        ]'''
        with patch("utils.model_config.get_llm_config", return_value=fake_config), \
             patch("utils.model_config.get_text_completion", return_value=fake_text):
            out = refine_relationships(tables, heuristic)
        self.assertEqual(out[0]["confidence_score"], 0.95)
        self.assertEqual(out[0]["source_reasoning"], "name match")

    def test_falls_back_on_invalid_llm_json(self):
        """Malformed LLM output → heuristic returned unchanged (fail-safe)."""
        from utils.llm_client import refine_relationships
        from utils.model_config import LLMConfig

        tables = [{"table_name": "A", "columns": [{"name": "X"}]}]
        heuristic = [{"from_table": "A", "from_column": "X",
                      "to_table": "A", "to_column": "X"}]
        fake_config = LLMConfig(provider="anthropic", model="claude-sonnet-5",
                                 litellm_model="anthropic/claude-sonnet-5", api_key="fake-key")
        with patch("utils.model_config.get_llm_config", return_value=fake_config), \
             patch("utils.model_config.get_text_completion", return_value="not json at all"):
            out = refine_relationships(tables, heuristic)
        self.assertIs(out, heuristic)


if __name__ == "__main__":
    unittest.main(verbosity=2)

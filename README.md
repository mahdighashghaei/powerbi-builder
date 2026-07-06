# Power BI Builder

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](requirements.txt)
[![Google ADK](https://img.shields.io/badge/Google%20ADK-2.3%2B-4285F4)](https://google.github.io/adk-docs/)
[![MCP](https://img.shields.io/badge/MCP-FastMCP-black)](https://modelcontextprotocol.io/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](#license)

**Power BI Builder** is a multi-agent system that turns a raw data file (CSV, Excel, JSON) and a plain-English business description into a complete, openable **Power BI Project** (`.pbip`: TMDL semantic model + PBIR report) — or edits an existing one.

The project is built in two layers. A deterministic, dependency-free **multi-agent pipeline** (`agents/`) does the actual work: it infers a schema, generates and scores multiple candidate DAX measure sets and report layouts, validates the result, and retries only the specific agent responsible when something is wrong. On top of that sits a genuine **Google ADK** conversational layer (`adk/`) — a root agent with specialist sub-agents, sessions, memory, artifacts, and callbacks — that lets a user drive the whole thing through natural language in the ADK web UI. The two layers are connected through a single **MCP (Model Context Protocol)** server (`mcp_server/`) that exposes the pipeline's tools and enforces path/JSON safety on every file write.

Every LLM-assisted decision in the deterministic pipeline (planning, business reasoning, measure/visual selection) has a rule-based fallback, so a missing or misconfigured LLM key degrades to that fallback instead of failing the build. An LLM provider must still be configured for the ADK conversational layer (`adk web`) — see **Installation** for how to set up Google (native) or a LiteLlm-backed provider (Anthropic/OpenAI).

---

## Installation

The recommended way to run Power BI Builder is through the **Google ADK conversational layer** — either in Docker (simplest) or locally. A single-shot CLI and a standalone MCP server also exist and are documented as optional/advanced alternatives in **Usage**.

### 1. Clone

```bash
git clone https://github.com/mahdighashghaei/powerbi-builder
cd powerbi-builder
```

### 2. Configure an LLM provider

```bash
cp .env.example .env
```

`utils/model_config.py` is the single source of truth for provider/model resolution, shared by the ADK layer and the deterministic pipeline's optional-LLM agents. Two ways to configure a provider:

**Option A — Google (native ADK path, no LiteLlm involved)**

```bash
# .env
GOOGLE_API_KEY=your-google-api-key
```

This is auto-detected — no `LLM_PROVIDER` needed. The ADK layer passes the bare model string (default `gemini-2.5-flash`) directly to `Agent(model=...)`. Override the model with `POWERBI_MODEL` or `MODEL_NAME`.

**Option B — Anthropic / OpenAI (or any other LiteLlm-supported provider)**

```bash
# .env
LLM_PROVIDER=openai
OPENAI_BASE_URL=your-base-url
OPENAI_API_KEY=your-openai-api-key
LLM_MODEL=your-llm-model
```

`LLM_PROVIDER` must be set explicitly for a non-Google provider — it is not auto-detected. The ADK layer then passes a `google.adk.models.lite_llm.LiteLlm` instance to `Agent(model=...)`, and the deterministic pipeline's optional-LLM agents call `litellm.completion()` directly. Override the model with the provider-agnostic `LLM_MODEL` (e.g. `LLM_MODEL=claude-sonnet-5`), and point at a custom gateway/proxy endpoint with `<PROVIDER>_BASE_URL` (e.g. `ANTHROPIC_BASE_URL`).

Other settings in `.env.example`: `OUTPUT_DIR`/`LOG_LEVEL` (runtime paths/verbosity), and `POWERBI_SESSION_DB_URL` (a SQLAlchemy async URL, e.g. `sqlite+aiosqlite:///./output/adk_sessions.db`, to persist ADK chat sessions across restarts).

### 3. Run

**Docker (recommended — simplest setup):**

```bash
docker compose up --build
```

Open **http://localhost:8000** for the ADK web chat UI, or check **http://localhost:8000/health** for a liveness probe. Generated projects and the session database persist in named volumes across container restarts.

**Local Python environment:**

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt

adk web adk/
```

Open **http://localhost:8000** in your browser. (A terminal chat REPL also exists — see **Usage** — but the browser UI is the recommended way to interact with the agent.)

See **Usage** below for the Google ADK chat commands, and for the optional CLI / standalone MCP server alternatives.

---

## Usage

### Google ADK web UI (recommended)

```bash
adk web adk/
```

Open **http://localhost:8000** in your browser and describe what to build:

```
you> Build a sales dashboard from examples/sample.csv
agent> ✅ Project: SalesDashboard — 1 table, 6 measures, 1 page. Ready to download.
you> Add a YoY % measure
you> Deploy this to MyWorkspace
```

This drives `root_agent`, which internally calls `generate_pbip`/`edit_pbip`/`add_measure`/etc. — the natural-language front end for the pipeline described in **Multi-Agent Pipeline** below.

A terminal chat REPL (`python chat.py`) is also available for the same `root_agent` if you prefer a command-line session over the browser; see `chat.py --help` for its options and slash commands.

### Docker

```bash
docker compose up --build
curl http://localhost:8000/health
```

Serves the ADK web UI on port 8000 with a persistent output volume and a SQLite session store — see **Installation**.

### Optional / advanced: single-shot CLI

For scripting or CI, `main.py` calls the deterministic pipeline directly — no ADK, no LLM key, and no MCP round trip required:

```bash
python main.py --csv examples/sample.csv --desc "Monthly sales by region"
python main.py --schema schema.json --desc "Customer churn dashboard" --project-name ChurnDashboard
python main.py --pbip ./output/ExistingProject --desc "add a YoY % measure and a trend page"
python main.py --pbix ./MyReport.pbix --desc "add a regional breakdown"
python main.py --excel data.xlsx --sheet "Sheet1" --desc "executive summary"
```

Common flags: `--theme`, `--output-dir`, `--interactive`, `--report-json`, `--deploy-to <workspace>` (`--deploy-dry-run` by default via `--deploy-mode`).

### Optional / advanced: standalone MCP server

```bash
python -m mcp_server.server ./output
```

Exposes `read_csv_schema`, `write_tmdl_table`, `write_tmdl_measures`, `write_pbir_page`, `write_deneb_visual`, `write_theme_json`, `validate_pbip_structure`, `generate_pbip`, `edit_pbip`, `add_measure`, `add_visual`, `add_page`, `deploy_to_fabric`, and `suggest_measures` over the Model Context Protocol, for any external MCP client (the ADK layer already talks to this same server internally — see **Architecture**).

---

## Features

- **Multiple input modes**: build from CSV / Excel / JSON schema (`create`), or edit an existing `.pbip` (`edit_pbip`), `.pbix` (`edit_pbix`), or raw Excel file (`edit_excel`).
- **Deterministic schema inference** with multi-strategy `summarizeBy` selection, tournament-scored (`agents/schema_agent.py`).
- **Automatic relationship detection** across multiple tables/sheets, heuristic plus optional LLM refinement (`agents/relationship_agent.py`).
- **Multi-candidate DAX generation** — up to 7 competing measure-set strategies (revenue-first, operational, time-intelligence, profitability, statistical, KPI-targeted, executive-summary), scored and tournament-selected (`agents/dax_agent.py`).
- **Guaranteed insight measures** — YoY %, category ranking, IQR-based anomaly count, and concept-coverage/outcome-rate measures are added automatically when the schema supports them.
- **Report/visual planning** — a large candidate visual pool is generated and arranged into pages using multiple competing layouts (executive, analytical, narrative, operational, …), scored and tournament-selected (`agents/visual_planner_agent.py`).
- **Structural + semantic validation** with auto-fix of trivial issues, and a separate semantic reviewer that flags ghost references, layout overlaps, and missing measures (`agents/validator_agent.py`, `agents/report_reviewer_agent.py`).
- **Targeted feedback loop** — validation/review/judge issues are routed to the *specific* agent responsible and re-run, up to 3 retries per build (`agents/orchestrator.py`).
- **Cross-agent Judge and consistency checker** — a deterministic, rule-based layer that scores global coherence across schema/DAX/report output and can force a re-run (`utils/judge.py`, `utils/consistency.py`).
- **Cross-run adaptive learning** — a persisted, per-project-cluster memory that biases future scoring weights and candidate counts based on prior outcomes (`utils/learning_memory.py`, `utils/adaptive_learning.py`).
- **Explainability artifacts** — every non-trivial decision is logged and written to `decisions.log.json`, `build.spec.json`, and a per-project `README.md`.
- **Google ADK conversational layer** — a root agent with 9 specialist sub-agents, a `SequentialAgent` workflow demo, sessions (in-memory or SQLite-backed), in-memory cross-session memory, artifact storage, and a `BasePlugin` (`adk/agent.py`, `adk/chat.py`, `adk/plugin.py`).
- **MCP server** (`mcp_server/server.py`, built with FastMCP) exposing both low-level file-write tools and high-level one-call orchestration tools (`generate_pbip`, `edit_pbip`, `add_measure`, `add_visual`, `add_page`, `deploy_to_fabric`, `suggest_measures`).
- **Microsoft Fabric deployment** via the official `fab` CLI, defaulting to a safe dry run (`fabric/deploy.py`).
- **Security by construction** — path-traversal-safe writes, confined reads, TMDL/DAX identifier escaping, JSON validation before every write, and a restricted subprocess environment for the external `fab` CLI.
- **Pattern libraries** for DAX (time intelligence, ranking, ratios, calculation groups), Deneb (Vega-Lite) visuals, SVG measures, and report themes.
- **Docker deployment** with a `/health` endpoint and a persistent SQLite session volume.
- **Extensive test suite** — 51 test modules covering security, schema inference, MCP tools, DAX patterns, validation, the feedback loop, adaptive learning, and the ADK chat layer.

---

## Architecture

The system is composed of three cooperating parts:

1. **Deterministic generation engine** (`agents/`) — a hand-written, dependency-free multi-agent pipeline coordinated by `OrchestratorAgent`. It does not depend on `google.adk` at all; its own docstrings describe it as mirroring the ADK `Agent` + sequential runner pattern without requiring the SDK. This is where schema inference, DAX generation, report planning, validation, the Judge, and adaptive learning live.
2. **MCP server** (`mcp_server/`) — a `FastMCP`-based server (`mcp_server/server.py`) exposing the pipeline's file-write/validate tools and Phase-7 high-level tools (`mcp_server/highlevel.py`) over the Model Context Protocol. Its `PbipToolbox` class is the actual, transport-agnostic implementation; the `@mcp.tool()` wrappers just serialize its results. Every write is funneled through `utils.security.safe_join` (path-traversal defense); every read is confined to an allowed-root set.
3. **Google ADK layer** (`adk/`) — the conversational front end: a `root_agent` with specialist sub-agents, tools, sessions, memory, artifacts, and callbacks. It reaches the deterministic engine through exactly one bridge: its `generate_pbip` tool performs a real MCP client → subprocess round trip (`adk/mcp_client.py`) to the MCP server, which in turn instantiates and runs `OrchestratorAgent`.

On top of the pipeline, several optimization/quality layers operate across every build:

- **Orchestration** — `OrchestratorAgent.run()` sequences the agents, threads a shared `AgentContext`, and owns the retry loop.
- **Validation** — structural (`validate_pbip_structure`) and semantic (TMDL/PBIR field) checks with auto-fix, plus a separate semantic reviewer for ghost references and layout issues.
- **Adaptive learning** — a cross-run `LearningMemory` biases scoring weights and candidate counts before generation even starts, based on how similar inputs performed in the past.
- **Explainability** — every scored decision is logged to a structured, per-run tracker and written to disk alongside the generated project.
- **Output generation** — TMDL (semantic model) and PBIR (report) files are rendered from Jinja2 templates / `mcp_server/pbir_generator.py`, verified against real Power BI Desktop sample exports.

```
                              ┌───────────────────────────────────────────┐
                              │        Google ADK conversational layer     │
                              │              (adk/agent.py)                │
                              │                                             │
                              │   root_agent ── sub_agents: schema, dax,   │
                              │   report, planner, analyzer, cleaner,      │
                              │   reviewer, status, deploy specialists     │
                              │   + sessions / memory / artifacts / plugin │
                              └───────────────────┬─────────────────────────┘
                                                  │  generate_pbip / edit_pbip / ...
                                                  ▼
                              ┌───────────────────────────────────────────┐
                              │     MCP server (mcp_server/server.py)      │
                              │     FastMCP · PbipToolbox · safe_join      │
                              │  low-level tools + high-level orchestration│
                              └───────────────────┬─────────────────────────┘
                                                  │  instantiates
                                                  ▼
                              ┌───────────────────────────────────────────┐
                              │      OrchestratorAgent (agents/orchestrator.py)   │
                              │      deterministic multi-agent pipeline    │
                              └───────────────────┬─────────────────────────┘
                                                  │
        ┌───────────┬───────────┬───────────┬────┴──────┬────────────┬────────────┐
        ▼           ▼           ▼           ▼            ▼            ▼            ▼
   Planner    BIReasoning  DataAnalyzer  DataCleaner   Schema/    Relationship   DAX
   Agent        Agent        Agent         Agent      ReadPBIP      Agent      Agent
                                                       /ReadPBIX
        │                                                                       │
        └───────────────────────────────┬───────────────────────────────────────┘
                                        ▼
                                 ReportAgent ── (delegates to) VisualPlannerAgent
                                        │
                                        ▼
                     ValidatorAgent → ReportReviewerAgent → JudgeLayer →
                     CrossAgentConsistencyChecker → feedback loop (≤3 retries)
                                        │
                                        ▼
                              InsightsAgent → StatusAgent
                                        │
                                        ▼
                 .pbip output + README.md + build.spec.json + decisions.log.json
                                        │
                                        ▼
                    (optional) fabric/deploy.py → fab CLI → Microsoft Fabric
```

CLI users (`main.py`) call `OrchestratorAgent` directly, bypassing both the ADK layer and the MCP round trip entirely — the deterministic engine works standalone.

---

## Multi-Agent Pipeline

All agents below live in `agents/` and are coordinated by `OrchestratorAgent`. Every agent inherits from `BaseAgent` (`agents/base.py`), receives the same mutable `AgentContext`, and returns a standard `AgentResult`.

| Agent | Responsibility | Inputs | Outputs | Interaction |
|---|---|---|---|---|
| **OrchestratorAgent** | Coordinates the whole run: sequencing, predictive weight adjustment, the feedback loop, and writing final artifacts. | Source path, business description, mode flags. | `RunReport` (steps, validation, error). | Instantiates and calls every other agent. |
| **PlannerAgent** | Produces an intent-aware `BuildPlan` (which phases run, `needs_cleaning`, `report_style`); LLM-assisted with a deterministic fallback. | Business description, data profile (if available). | `ctx.extra["plan"/"build_plan"/"needs_cleaning"/"report_style"]`. | Runs first; its `report_style`/`needs_cleaning` gate later agents. |
| **BIReasoningAgent** | Business-intent reasoning: domain, audience, dashboard type, recommended pages/KPIs. Advisory only. | Business description, partial schema. | `ctx.extra["bi_reasoning"]` (`BIReasoningResult`). | Consumed by `VisualPlannerAgent` for page/visual alignment. |
| **DataAnalyzerAgent** | Profiles data quality (nulls, outliers, duplicates, single-value columns), self-verifies via a second read, and raises ambiguous-decision questions. | Raw source file, or existing schema in edit modes. | `ctx.extra["data_profile"/"business_analysis"]`. | Gates whether `DataCleanerAgent` runs; can abort the run on blocking issues. |
| **DataCleanerAgent** | Builds and applies a non-destructive cleaning plan; writes a cleaned copy and redirects `ctx.source_path`. | `data_profile`, user/default answers. | Cleaned CSV file, `ctx.extra["cleaning_report"]`. | Runs only when the plan or quality score requires it; `SchemaAgent` reads its output. |
| **SchemaAgent** | Infers the table schema, generates several `summarizeBy` strategies, tournament-selects the best, and writes the TMDL table. | Source file, `business_analysis`, scoring weights. | `ctx.schema`, TMDL table file. | Replaced by `ReadPBIPAgent`/`ReadPBIXAgent` in edit modes; feeds every downstream agent. |
| **ReadPBIPAgent** | Parses an existing PBIP's TMDL into `ctx.schema` for edit-in-place workflows. | Existing PBIP folder. | `ctx.schema`, `existing_measures`, `existing_page_ids`. | Runs instead of `SchemaAgent` in `edit_pbip` mode. |
| **ReadPBIXAgent** | Extracts a schema from a `.pbix` ZIP archive (`model.bim` JSON, or a Report/Layout fallback). | `.pbix` file. | `ctx.schema`. | Runs instead of `SchemaAgent` in `edit_pbix` mode. |
| **RelationshipAgent** | Detects foreign-key relationships across tables (heuristic name matching, refined by an optional LLM call) and writes `relationships.tmdl`. | `ctx.schema["all_tables"]`. | `ctx.extra["relationships"]`. | Runs after the schema-producing agent, before `DAXAgent`. |
| **DAXAgent** | Generates up to 7 candidate DAX measure sets, scores and tournament-selects a winner, guarantees insight/concept-coverage/outcome-rate measures, sanitizes bad aggregations, and writes the measures. | `ctx.schema`, business analysis, prioritized KPIs, scoring weights. | `ctx.measures`, TMDL measures file. | Delegates final pruning/ranking to `MeasureSelectorAgent`; feeds `ReportAgent`. |
| **MeasureSelectorAgent** | Pure selector (not a pipeline `BaseAgent`) — optionally prunes/ranks/extends the DAX candidate pool by intent. | Candidate measures, description, schema. | `MeasureSet`. | Called only by `DAXAgent`. |
| **ReportAgent** | Resolves the effective report style/page count, builds a large visual-candidate pool, and writes the PBIR pages/visuals/theme/report.json. | `ctx.schema`, `ctx.measures`, `report_style`. | `ctx.pages`, PBIR files. | Delegates page/visual arrangement to `VisualPlannerAgent`. |
| **VisualPlannerAgent** | Plans page/visual arrangement across several candidate layouts, scores and tournament-selects one, and prunes ghost references. | Visual candidates, BI reasoning, report style, max pages. | `ctx.extra["report_plan"]`. | Instantiated and run inline by `ReportAgent`. |
| **ValidatorAgent** | Structural + TMDL/PBIR semantic validation; auto-fixes trivial issues; tags each issue with the responsible agent. | Files on disk under `ctx.pbip_root`. | `ctx.validation`. | Feeds the feedback loop's routing table. |
| **ReportReviewerAgent** | Semantic review: ghost references, visual/data-type mismatches, layout overlaps, measure coverage. | `ctx.pbip_root`. | `ctx.extra["review"]`. | Runs once after `ValidatorAgent`; its issues also feed the feedback loop. |
| **InsightsAgent** | Advisory narrative pass over the final state: anomalies, segments, underperformers, trends, per-visual explanations, missing-KPI suggestions. Pure pandas/statistics — no LLM. | Final schema/measures/report plan, raw or cleaned CSV. | `ctx.extra["insights"]`. | Runs after the feedback loop; rendered into the per-project `README.md`. |
| **StatusAgent** | Aggregates run progress/errors into a human-readable summary. | `ctx.extra["run_steps"]`. | `ctx.extra["status"]`. | Called at early aborts and at the end of a run. |

**Cross-agent, non-`BaseAgent` layers** that operate across the whole pipeline: `utils/judge.py::JudgeLayer` (global consistency scoring + override actions) and `utils/consistency.py::CrossAgentConsistencyChecker` (alignment scoring), both run by `OrchestratorAgent` after `ReportReviewerAgent`.

A parallel set of **Google ADK specialist agents** exists in `adk/agent.py` (`schema_specialist`, `dax_specialist`, `report_specialist`, `data_analyzer`, `data_cleaner`, `planner`, `report_reviewer`, `status`, `deploy_specialist`), wired as `sub_agents` of `root_agent` plus a demo `SequentialAgent` (`pipeline_agent`). These wrap the same conceptual responsibilities as tool-calling ADK agents for the conversational layer; the actual build logic they invoke still runs through `generate_pbip` → the deterministic pipeline above.

---

## Generation Workflow

1. **Input** — a user provides a CSV/Excel/JSON file (or an existing PBIP/PBIX to edit) plus a plain-English description, via the ADK web UI, the (optional) single-shot CLI (`main.py`), or the terminal chat REPL.
2. **Dispatch** — the CLI calls `OrchestratorAgent` directly; the ADK layer's `generate_pbip` tool round-trips through the MCP server to reach the same class.
3. **Setup** — the orchestrator resolves the project name, creates an isolated output directory, and binds a `PbipToolbox` to it.
4. **Predictive weighting** — scoring weights are pre-biased from `feedback_history.json`, business-domain keywords, and (if available) the data-quality score.
5. **Adaptive-learning init** — `LearningMemory` is loaded and used to compute an input cluster, `candidate_count`, and `adaptive_bias` for this run.
6. **Planning** — `PlannerAgent` produces the build plan; `BIReasoningAgent` reasons about business intent.
7. **Data analysis** — `DataAnalyzerAgent` profiles quality; blocking issues can abort the run in non-interactive mode.
8. **Cleaning** (conditional) — `DataCleanerAgent` runs only if the plan or quality score requires it.
9. **Semantic/KPI discovery** — the orchestrator discovers real arithmetic relationships between numeric columns (`utils/semantic_model.py`), prioritizes KPIs, extracts named business concepts, and detects binary-outcome columns — all computed once and shared via `ctx.extra`.
10. **Schema** — `SchemaAgent` (or `ReadPBIPAgent`/`ReadPBIXAgent` in edit modes) infers/reads the schema and writes TMDL.
11. **Relationships** — `RelationshipAgent` detects and writes cross-table relationships.
12. **DAX** — `DAXAgent` generates, scores, and writes the measure set.
13. **Report** — `ReportAgent` resolves style/page count, builds the visual pool, delegates arrangement to `VisualPlannerAgent`, and writes PBIR files + theme.
14. **Project metadata** — `.pbip` entry file, `.platform`, `definition.pbism`/`definition.pbir`, `pages.json`, and the model/database TMDL skeleton (plus an auto-generated `Date` table when applicable) are written.
15. **Validation** — `ValidatorAgent` checks structure and semantics and auto-fixes trivial issues.
16. **Review** — `ReportReviewerAgent` performs a semantic review.
17. **Judge + consistency check** — `JudgeLayer` and `CrossAgentConsistencyChecker` score global coherence and may emit override actions.
18. **Feedback loop** — up to 3 retries: routable issues are sent back to the specific responsible agent, which re-runs, followed by re-validation.
19. **Insights** — `InsightsAgent` computes the final narrative pass.
20. **Status + artifacts** — `StatusAgent` summarizes the run; `README.md`, `build.spec.json`, and `decisions.log.json` are written; the outcome is recorded into `LearningMemory` for future runs.
21. **(Optional) Deployment** — `--deploy-to <workspace>` (CLI) or the `deploy_pbip_to_fabric` tool publishes the project via the Fabric `fab` CLI, defaulting to a dry run.

---

## Candidate Selection

Non-trivial choices are made by generating several competing candidates **in-process** (not concurrent agents) and scoring them with a shared utility model, implemented in `utils/scoring.py`:

```
semantic_score = ( embedding_similarity_to_biz_intent
                  + kpi_semantic_alignment
                  + graph_connectivity_score
                  + schema_intent_match
                  + visual_semantic_coherence ) / 5

final_score = 0.6 * semantic_score + 0.4 * heuristic_score
```

- **Heuristic weights** default to `business_value=0.30, kpi_alignment=0.25, data_coverage=0.20, visual_quality=0.15, interpretability=0.10` (`DEFAULT_WEIGHTS`), and can be adjusted at runtime by predictive weighting, the Judge's policy adjustments, and the feedback loop.
- **Selection** uses `tournament_select()` — candidates are grouped, the top scorers from each group advance, and a global winner is chosen — rather than a plain arg-max.
- Applied to three independent decision points:
  - **Schema**: `SchemaAgent` scores up to 7 `summarizeBy` strategies (`conservative`, `analytical`, `categorical`, `kpi_focused`, `relationship_aware`, `aggressive_numeric`, `minimal_aggregation`).
  - **DAX measures**: `DAXAgent` scores up to 7 measure-set strategies (`revenue_first`, `operational`, `time_intelligence`, `profitability`, `statistical`, `kpi_targeted`, `executive_summary`).
  - **Visual layout**: `VisualPlannerAgent` scores up to 7 page/visual arrangements (`executive`, `analytical`, `comprehensive`, `narrative`, `operational`, `kpi_grid`, `mixed_density`).
- The number of candidates generated (`candidate_count`) and the priors applied before scoring (`adaptive_bias`) scale with input complexity, computed by `utils.scoring.compute_complexity_score`.
- A **Strategy Synthesis Layer** (`utils/strategy_synthesizer.py`) can generate additional, synthesized strategies when the Judge's `strategy_gaps` or a cluster's persisted failure patterns indicate a recurring gap.

---

## Adaptive Learning

`utils/learning_memory.py::LearningMemory` and `utils/adaptive_learning.py::AdaptiveLearningLayer` implement a **cross-run** learning loop (distinct from the in-run feedback loop above):

- **Clustering** — each run is assigned a deterministic, human-readable cluster key: `{domain}_kpi{bucket}_cols{bucket}` (e.g. `finance_kpi5to9_cols15to29`), derived from the business description's detected domain, the number of KPIs, and the number of schema columns (`LearningMemory.cluster_input`).
- **Scoring bias** — before any agent runs, the orchestrator reads the cluster's stored success/failure patterns and computes an `adaptive_bias` and `candidate_count` that are injected into `SchemaAgent`, `DAXAgent`, and `VisualPlannerAgent` via `ctx.extra`.
- **Persistence** — a single JSON file per project run root (`learning_memory.json`), written atomically (`save()` writes to a temp file then `os.replace`s it). It stores per-cluster success/failure patterns (capped at 20 each), per-strategy usage/success statistics (for Strategy Synthesis), and cached "semantic model" relationships keyed by schema fingerprint.
- **Feedback** — after a run finishes, `record_outcome()` records the winning candidate, its semantic score, and whether the Judge overrode it; `record_strategy_outcome()`/`decay_weak_strategies()`/`prune_strategies()` age out synthesized strategies that keep losing.
- All public methods are exception-safe by design — a corrupted or missing memory file never blocks a build; the system simply starts from neutral priors.

---

## Explainability

Every build writes a set of inspectable artifacts to the project's output root:

| Artifact | Produced by | Contents |
|---|---|---|
| `decisions.log.json` | `utils/explainability.py::ExplainabilityTracker`, flushed by `OrchestratorAgent._write_decisions_log()` | Every logged decision: agent, decision type, subject, rationale, alternatives considered, confidence, and extra context (e.g. rejected candidate scores). |
| `build.spec.json` | `OrchestratorAgent._write_spec()` | A versioned, reproducible snapshot of the build: source, inferred schema, measures, pages, relationships, validation result, plan, insights, and the full per-agent trajectory. |
| `README.md` (per project) | `OrchestratorAgent._write_readme()` | Human-readable summary of the data model, DAX measures, report pages, validation status, and (when present) the Business Insights section from `InsightsAgent`. |
| `feedback_history.json` | `OrchestratorAgent._run_feedback_loop()` | One entry per feedback-loop retry attempt: issue count, quality score, agents re-run, and the scoring weights used. |
| `learning_memory.json` | `utils/learning_memory.py::LearningMemory` | Cross-run cluster success/failure patterns and synthesized-strategy statistics (see **Adaptive Learning**). |
| `logs/powerbi_builder.log` | `utils/security.py::AuditLogger` | Centralized audit log of every agent and tool action. |

Individual decisions (schema-strategy selection, DAX candidate selection, visual arrangement selection, relationship inference, KPI/page recommendations) are logged via `utils.explainability.log_decision` from the relevant agent and end up in `decisions.log.json`.

---

## Security

Security controls are implemented in code, not just documented:

| Mechanism | Implementation |
|---|---|
| **Path-traversal prevention** | `utils.security.safe_join` rejects `..` segments lexically and verifies resolved-path containment after symlink expansion. Every MCP write is funneled through it. |
| **Confined reads** | `PbipToolbox._readable()` always rejects `..`; PBIP/project reads require strict containment inside an allowed-root set; user data-file reads outside the roots are logged as a warning rather than blocked. Extra roots via `POWERBI_ALLOWED_READ_ROOTS`. |
| **Identifier escaping** | `utils/identifiers.py` single-quote-escapes TMDL/DAX table and column names so a name containing `'` cannot break out of its quoted context. |
| **JSON validation** | `validate_json_string`/`serialize_json` validate content before every write. |
| **Atomic writes** | `atomic_write_text`/`atomic_write_json` write to a temp file and `os.replace()` it, so a crash never leaves a half-written file. |
| **Credential handling** | LLM API keys are read only from environment variables / `.env` (via `python-dotenv`); they are never written into `decisions.log.json`, `build.spec.json`, or `README.md`. `utils.model_config.MissingAPIKeyError` fails loudly only when a provider is explicitly requested but misconfigured. |
| **MCP stdio safety** | Under the stdio transport, `AuditLogger` routes console output to `stderr` (never `stdout`, which is the JSON-RPC channel). |
| **ADK-layer path guard** | `before_tool_callback=validate_paths` (`adk/agent.py`) verifies a file/directory exists before a path-taking tool executes, returning a clear error with suggested sample files instead of a stack trace. |
| **Restricted subprocess environment** | `security/sandbox.py::restricted_env()` passes only an allowlist of environment variables to the external `fab` CLI subprocess — LLM API keys are explicitly excluded. |
| **Safe-by-default deployment** | `deploy_pbip_to_fabric`/`deploy_to_fabric` default to `dry_run=True` everywhere (CLI, ADK tool, MCP tool) — nothing is uploaded to Microsoft Fabric unless the caller explicitly opts out. |
| **Adversarial test harness** | `security/red_team.py` generates a deterministic catalogue of adversarial inputs (path traversal, identifier injection, malformed schema); `security/blue_team.py` runs them against the real controls and records whether the defense held. |

---

## Project Structure

```
powerbi-builder/
├── agents/                    # deterministic multi-agent pipeline
│   ├── base.py                 # BaseAgent, AgentContext, AgentResult
│   ├── orchestrator.py         # OrchestratorAgent + RunReport + feedback loop
│   ├── planner_agent.py
│   ├── bi_reasoning_agent.py
│   ├── data_analyzer_agent.py
│   ├── data_cleaner_agent.py
│   ├── schema_agent.py
│   ├── relationship_agent.py
│   ├── dax_agent.py
│   ├── measure_selector_agent.py
│   ├── report_agent.py
│   ├── visual_planner_agent.py
│   ├── validator_agent.py
│   ├── report_reviewer_agent.py
│   ├── insights_agent.py
│   ├── status_agent.py
│   ├── read_pbip_agent.py
│   ├── read_pbix_agent.py
│   ├── schemas.py               # Pydantic contracts shared across agents
│   └── PowerBIBuilder.agent.md  # ADK agent instructions
├── adk/                        # Google ADK conversational layer
│   ├── agent.py                 # root_agent + specialist sub_agents + callbacks
│   ├── chat.py                  # ChatRepl — terminal REPL (Runner + services)
│   ├── plugin.py                # PowerBIBuilderPlugin (BasePlugin)
│   ├── server.py                # FastAPI app: adk web + /health + A2A routes
│   ├── mcp_client.py             # persistent MCP client (stdio subprocess)
│   ├── config.py                # ADK model / output-root / session config
│   ├── a2a.py / a2ui.py          # Agent-to-Agent / Agent-to-UI protocol surfaces
│   └── tools/                   # ~30 ADK FunctionTool wrappers
├── mcp_server/
│   ├── server.py                 # FastMCP stdio server + in-process PbipToolbox
│   ├── highlevel.py               # high-level orchestration tools (generate_pbip, ...)
│   ├── schema_inference.py         # CSV/JSON/Excel schema + data-quality profiling
│   └── pbir_generator.py          # PBIR JSON payload builders
├── utils/                       # shared, dependency-free utilities
│   ├── security.py                # safe_join, JSON validation, AuditLogger
│   ├── identifiers.py              # DAX/TMDL identifier escaping
│   ├── scoring.py                  # candidate scoring + tournament_select
│   ├── judge.py                    # JudgeLayer (cross-agent consistency authority)
│   ├── consistency.py              # CrossAgentConsistencyChecker
│   ├── explainability.py            # decision logging
│   ├── learning_memory.py           # cross-run adaptive learning persistence
│   ├── adaptive_learning.py
│   ├── strategy_synthesizer.py
│   ├── kpi_prioritizer.py / concept_coverage.py / semantic_model.py
│   ├── insights_engine.py / layout_engine.py
│   ├── model_config.py              # provider/model resolution (litellm-backed)
│   ├── llm_client.py / retry.py
│   ├── event_bus.py                 # in-process pub/sub (agent.completed events)
│   ├── tmdl_parser.py / date_table.py / excel_reader.py / pbip_paths.py
│   └── zip_utils.py / visual_types.py / consistency.py
├── patterns/                    # DAX / Deneb / SVG / theme pattern libraries
├── validators/                   # BPA rules, lineage, naming, knowledge graph
├── security/                      # red/blue/green security-drill harness + sandbox
├── fabric/                         # deploy.py + fab_cli.py (Microsoft Fabric)
├── templates/                       # Jinja2 templates (TMDL table, PBIR page, theme)
├── skills/                            # Microsoft Fabric skill reference docs
├── tests/                           # 51 test modules
├── examples/                       # sample.csv + MCP client config templates
├── main.py                       # single-shot CLI entry point
├── chat.py                      # interactive chat REPL entry point
├── config.py                   # legacy env-based settings loader
├── Dockerfile / docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

---

## Example

**Input** — `examples/sample.csv`:

```csv
OrderDate,Region,Product,Quantity,Amount
2024-01-05,North,Widget,10,250.50
2024-01-07,South,Gadget,5,99.99
2024-02-10,East,Widget,8,200.00
2024-02-15,West,Gadget,12,239.88
```

```bash
python main.py --csv examples/sample.csv --desc "Monthly sales by region"
```

**Output** (`output/MonthlySalesByRegion/`):

- A `sample` table with typed columns (`OrderDate: dateTime`, `Quantity: int64`, `Amount: double`, …).
- A tournament-selected set of DAX measures grouped into folders such as `Revenue`, `Orders`, and `Dates` (e.g. `Total Amount`, `Avg Amount`, `Order Count`, `Amount YTD`, `Amount YoY %`).
- A report page with a mix of cards, bar/column/line charts, and a details table, arranged by the visual-planning tournament.
- A structural + semantic validation pass with any auto-fixes applied.
- A per-project `README.md`, `build.spec.json`, and `decisions.log.json` describing exactly what was built and why.

---

## Output

Every successful `create`-mode build writes, under `output/<ProjectName>/`:

```
<ProjectName>.pbip                                   # root entry file — open this in Power BI Desktop
<ProjectName>.SemanticModel/
├── .platform
└── definition/
    ├── definition.pbism
    ├── model.tmdl
    ├── database.tmdl
    ├── relationships.tmdl        (if relationships were detected)
    └── tables/
        ├── <TableName>.tmdl      # columns + DAX measures
        └── Date.tmdl             (auto-generated when a date column needs one)
<ProjectName>.Report/
├── .platform
├── definition.pbir
└── definition/
    ├── version.json
    ├── report.json
    ├── StaticResources/RegisteredResources/theme.json
    └── pages/
        ├── pages.json
        └── <page-id>/
            ├── page.json
            └── visuals/<visual-id>/visual.json
README.md              # human-readable build summary + insights
build.spec.json         # versioned, reproducible build specification
decisions.log.json      # explainability decision trail
feedback_history.json   # feedback-loop retry history (if any retries occurred)
learning_memory.json    # cross-run adaptive learning memory for this project
```

`edit_pbip`/`edit_pbix`/`edit_excel` modes write into a fresh, isolated copy of the target project rather than mutating the original.

---

## Testing

```bash
# stdlib unittest
python -m unittest discover tests -v

# pytest (auto-collects the unittest suites; also runs the optional
# Gherkin/BDD .feature scenarios in tests/features/ if pytest-bdd is installed)
python -m pytest tests/ -v
```

The `tests/` directory contains **51 test modules** covering: path-traversal and identifier-injection defenses, CSV/JSON/Excel schema inference, every MCP tool (low-level and high-level), the candidate-scoring/tournament-selection model, the feedback loop, adaptive learning and the semantic-model discovery layer, the Judge and consistency checker, PBIR/TMDL structural validation, DAX/Deneb/SVG pattern libraries, Fabric deployment, and the ADK chat REPL (slash commands, sessions, memory, artifacts, plugin callbacks).

---

## Technologies

| Technology | Role |
|---|---|
| **Python 3.10+** | Primary language for the entire project. |
| **Google ADK (`google-adk[db]>=2.3.0`)** | Conversational agent framework — root agent, sub-agents, sessions, memory, artifacts, callbacks, plugins. |
| **MCP (`mcp>=1.2.0`, FastMCP)** | Tool-serving protocol between the ADK layer and the deterministic engine. |
| **pandas** | CSV/Excel reading, schema inference, data-quality profiling, insight computation. |
| **Jinja2** | TMDL/theme template rendering. |
| **Pydantic** | Typed contracts for plans, measures, report plans, and validation results (`agents/schemas.py`). |
| **python-dotenv** | Loads `.env` configuration without hardcoding secrets. |
| **litellm** | Provider-agnostic LLM completion (Google, Anthropic, OpenAI) for optional enrichment. |
| **FastAPI + uvicorn** | ASGI server hosting the ADK web app, `/health`, and A2A routes (`adk/server.py`). |
| **aiosqlite** | Async SQLite driver backing `DatabaseSessionService` session persistence. |
| **OpenTelemetry (`opentelemetry-api`/`-sdk`)** | Optional trajectory/telemetry spans for agent and tool execution. |
| **pytest / pytest-bdd** | Test runner; optional Gherkin/BDD scenario support. |
| **Power BI PBIP / TMDL / PBIR** | Target output format — the semantic model (TMDL) and report (PBIR) definitions Power BI Desktop reads directly. |
| **Microsoft Fabric CLI (`fab`, `ms-fabric-cli`)** | Optional deployment target, invoked as a sandboxed subprocess. |
| **Docker / Docker Compose** | Containerized deployment of the ADK web server with persistent volumes. |

---

## Roadmap

The following are realistic, not-yet-implemented directions consistent with the current architecture:

- Genuine parallel agent execution (e.g. an ADK `ParallelAgent` or concurrent candidate generation), currently sequential.
- A persistent, non-in-memory ADK memory backend (e.g. `VertexAiRagMemoryService`) for the conversational layer, which today only uses `InMemoryMemoryService`.
- Deeper `.pbix` binary model parsing beyond the current `model.bim`/Report-Layout fallback path.
- Wiring the standalone `security/trust_score.py` continuous trust score into the orchestrator's run report / `build.spec.json` (currently a separate, unwired module).
- A CI pipeline (lint + test) for pull requests.

---

## License

This project is released under the **GNU General Public License v3.0 (GPL-3.0)**. See [`LICENSE`](LICENSE) for the full text.

GPL-3.0 is a strong copyleft license: you are free to use, study, modify, and distribute this software (including commercially), but any distributed derivative work must also be licensed under GPL-3.0 and its complete corresponding source code must be made available.

---

## Contributing

Contributions are welcome:

1. Fork the repository and create a feature branch.
2. Keep the deterministic pipeline's fail-safe contract: any optional LLM call must have a working offline fallback, and any advisory layer (Judge, Insights, adaptive learning) must never raise out of `evaluate()`/`run()`.
3. Add or update tests under `tests/` for any behavioral change, and run `python -m pytest tests/ -v` before opening a pull request.
4. Follow the existing `ruff` configuration (`pyproject.toml`) for linting (`E`, `W`, `F`, `I`, `UP` rule sets).
5. Describe the change, the files touched, and how it was tested in the pull request description.

---

## Acknowledgements

- [Google Agent Development Kit (ADK)](https://google.github.io/adk-docs/) — the conversational multi-agent framework powering `adk/`.
- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) / [FastMCP](https://github.com/modelcontextprotocol/python-sdk) — the tool-serving protocol between the ADK layer and the deterministic engine.
- [Power BI Project (PBIP) / TMDL / PBIR](https://learn.microsoft.com/power-bi/developer/projects/projects-overview) — the target output format this project generates.
- [pandas](https://pandas.pydata.org/) — schema inference and data-quality profiling.
- [Jinja2](https://jinja.palletsprojects.com/) — TMDL/theme template rendering.
- [litellm](https://github.com/BerriAI/litellm) — provider-agnostic LLM completion.
- [Microsoft Fabric CLI](https://github.com/microsoft/fabric-cli) — optional deployment target.

Built as a capstone submission for the Kaggle **"AI Agents: Intensive Vibe Coding"** competition, *Agents for Business* track.

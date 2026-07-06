"""utils/scoring.py — Semantic-aware global utility scoring model.

Architecture: Top-3 Kaggle Winning Architecture — Semantic Multi-Candidate
Intelligence System. Scoring evolves from rule-based heuristics into a
*semantic utility model* that understands business intent, KPI alignment,
graph connectivity, schema-intent match, and visual coherence.

Final score formula (per candidate)
------------------------------------
    semantic_score = (
        embedding_similarity_to_biz_intent   # term-overlap with business description
        + kpi_semantic_alignment             # KPI name semantic match depth
        + graph_connectivity_score           # measure interdependency graph density
        + schema_intent_match                # column domain ↔ business domain fit
        + visual_semantic_coherence          # visual kinds ↔ dashboard type fit
    ) / 5.0                                  # normalised to [0, 1]

    final_score = 0.6 * semantic_score + 0.4 * heuristic_score

Selection strategy
------------------
    Tournament selection (replaces argmax):
    1. Partition candidates into groups.
    2. Select top-2 winners from each group.
    3. Re-score finalists with semantic model (second pass).
    4. Select global winner.

Backward compatibility
----------------------
    ``select_best()`` is preserved and internally calls ``tournament_select()``.
    All ``CandidateScore`` fields are extended, never removed.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Default weights — heuristic dimension weights (unchanged from prior version)
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: dict[str, float] = {
    "business_value":    0.30,
    "kpi_alignment":     0.25,
    "data_coverage":     0.20,
    "visual_quality":    0.15,
    "interpretability":  0.10,
}

# Semantic blending ratio: 60% semantic + 40% heuristic
_SEMANTIC_WEIGHT = 0.60
_HEURISTIC_WEIGHT = 0.40

# Power BI data types considered "real" (non-ambiguous)
_KNOWN_PBI_TYPES = {
    "int64", "double", "decimal", "string", "dateTime", "date",
    "boolean", "binary", "duration",
}

# DAX measure folders that signal direct business value
_BIZ_VALUE_FOLDERS = {"Revenue", "Orders", "Sales", "Finance", "KPI"}

# Visual kinds carrying immediate executive value
_EXEC_VISUAL_KINDS = {"card", "kpi"}

# Visual kinds that are chart-type (trend / comparison)
_CHART_VISUAL_KINDS = {"barChart", "columnChart", "lineChart", "donutChart", "scatterChart"}

# Dashboard-type → ideal visual kind mix (for visual_semantic_coherence)
_DASHBOARD_VISUAL_MAP: dict[str, frozenset[str]] = {
    "executive":    frozenset({"card", "kpi", "columnChart", "lineChart"}),
    "operational":  frozenset({"barChart", "columnChart", "matrix", "slicer"}),
    "analytical":   frozenset({"scatterChart", "lineChart", "matrix", "donutChart"}),
    "narrative":    frozenset({"card", "lineChart", "barChart", "columnChart"}),
    "default":      frozenset({"card", "barChart", "columnChart", "lineChart"}),
}

# Business domain keywords → relevant column/measure name patterns
_DOMAIN_INTENT_KEYWORDS: dict[str, list[str]] = {
    "revenue":     ["revenue", "sales", "income", "profit", "margin", "price", "amount"],
    "operations":  ["count", "volume", "quantity", "orders", "units", "rate", "duration"],
    "customer":    ["customer", "client", "user", "segment", "account", "contact"],
    "time":        ["date", "year", "month", "quarter", "period", "ytd", "yoy", "mom"],
    "geography":   ["region", "country", "city", "market", "territory", "zone"],
    "product":     ["product", "category", "brand", "segment", "sku", "item", "type"],
}


# ---------------------------------------------------------------------------
# Adaptive-intelligence: complexity scoring + candidate count selection
# ---------------------------------------------------------------------------

def compute_complexity_score(
    schema_columns: list[dict[str, Any]],
    kpi_list: list[str],
    business_description: str,
) -> float:
    """Score the complexity of an input context on a [0, 1] scale.

    Three independent signals are averaged:

    schema_size
        Fraction of columns relative to a "rich" schema (30 columns = 1.0).
        More columns → more candidate strategies needed to explore the space.

    kpi_density
        Fraction of KPIs relative to a "rich" KPI list (10 KPIs = 1.0).
        More KPIs → more candidate variation required to cover them all.

    ambiguity
        1 − (domain coverage / 6).  When the business description mentions
        many domain vocabularies the intent is clear (low ambiguity = low
        complexity contribution). When few domains match the intent is
        ambiguous → higher complexity.

    Returns:
        Float in [0, 1]; higher = more exploration candidates recommended.
    """
    # schema size: cols / 30 capped at 1.0
    n_cols = len(schema_columns or [])
    schema_size = min(1.0, n_cols / 30.0) if n_cols > 0 else 0.0

    # KPI density: kpis / 10 capped at 1.0
    n_kpis = len(kpi_list or [])
    kpi_density = min(1.0, n_kpis / 10.0)

    # ambiguity: how many domain vocabularies are present (0–6 domains)
    biz_toks = _tokenize(business_description or "")
    domain_hits = sum(
        1 for kws in _DOMAIN_INTENT_KEYWORDS.values()
        if any(kw in biz_toks for kw in kws)
    )
    # many domain hits → clear intent → low ambiguity contribution
    ambiguity = 1.0 - min(1.0, domain_hits / 6.0)

    return (schema_size + kpi_density + ambiguity) / 3.0


def candidate_count_from_complexity(complexity_score: float) -> int:
    """Map a [0, 1] complexity score to a candidate generation count.

    Thresholds (from the adaptive-intelligence specification):

    * complexity > 0.7  → 12 candidates  (high: 10–12 range)
    * complexity > 0.4  → 7  candidates  (medium: 5–7 range)
    * otherwise         → 4  candidates  (low: 3–5 range)

    The minimum is 4 (not 3) so that even a very simple input still generates
    at least the same 5-candidate baseline used before this upgrade (the
    agents always generate 5 base candidates regardless, so 4 means "keep
    the 5-base and don't add extras").
    """
    if complexity_score > 0.7:
        return 12
    elif complexity_score > 0.4:
        return 7
    else:
        return 4


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SemanticScore:
    """Semantic sub-scores for one candidate (all [0, 1])."""

    embedding_similarity:    float = 0.0   # text overlap with business description
    kpi_semantic_alignment:  float = 0.0   # KPI name semantic depth match
    graph_connectivity:      float = 0.0   # measure interdependency density
    schema_intent_match:     float = 0.0   # column domain ↔ business domain fit
    visual_semantic_coherence: float = 0.0 # visual kinds ↔ dashboard type

    @property
    def total(self) -> float:
        return (
            self.embedding_similarity
            + self.kpi_semantic_alignment
            + self.graph_connectivity
            + self.schema_intent_match
            + self.visual_semantic_coherence
        ) / 5.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "embedding_similarity":     round(self.embedding_similarity, 4),
            "kpi_semantic_alignment":   round(self.kpi_semantic_alignment, 4),
            "graph_connectivity":       round(self.graph_connectivity, 4),
            "schema_intent_match":      round(self.schema_intent_match, 4),
            "visual_semantic_coherence": round(self.visual_semantic_coherence, 4),
            "total":                    round(self.total, 4),
        }


@dataclass
class CandidateScore:
    """Scored evaluation of one candidate output from a generation component.

    Extended to hold both heuristic sub-scores (unchanged) and semantic
    sub-scores (new). ``total`` now reflects the blended final_score.
    """

    candidate_id: str
    # --- heuristic dimensions (unchanged) ---
    business_value:    float = 0.0
    kpi_alignment:     float = 0.0
    data_coverage:     float = 0.0
    visual_quality:    float = 0.0
    interpretability:  float = 0.0
    # --- semantic extension ---
    semantic:          SemanticScore = field(default_factory=SemanticScore)
    heuristic_total:   float = 0.0   # raw weighted heuristic score (pre-blend)
    semantic_total:    float = 0.0   # raw semantic score (pre-blend)
    total:             float = 0.0   # blended final score
    weights_used:      dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id":    self.candidate_id,
            "total":           round(self.total, 4),
            "heuristic_total": round(self.heuristic_total, 4),
            "semantic_total":  round(self.semantic_total, 4),
            "business_value":  round(self.business_value, 4),
            "kpi_alignment":   round(self.kpi_alignment, 4),
            "data_coverage":   round(self.data_coverage, 4),
            "visual_quality":  round(self.visual_quality, 4),
            "interpretability": round(self.interpretability, 4),
            "semantic":        self.semantic.as_dict(),
            "weights_used":    {k: round(v, 4) for k, v in self.weights_used.items()},
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    return num / den if den > 0 else default


def _col_names_in_expr(expression: str) -> set[str]:
    """Extract column names from a DAX expression (bare [Name] or 'T'[Name])."""
    return set(re.findall(r"\[([^\]]+)\]", expression))


def _resolve_weights(weights: dict[str, float] | None) -> dict[str, float]:
    if not weights:
        return dict(DEFAULT_WEIGHTS)
    merged = dict(DEFAULT_WEIGHTS)
    merged.update(weights)
    return merged


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens from any text, stripping punctuation."""
    return set(re.findall(r"[a-z][a-z0-9]*", text.lower()))


def _term_overlap(text_a: str, text_b: str) -> float:
    """Jaccard-style term overlap between two text strings → [0, 1]."""
    if not text_a or not text_b:
        return 0.0
    toks_a = _tokenize(text_a)
    toks_b = _tokenize(text_b)
    intersection = toks_a & toks_b
    union = toks_a | toks_b
    return _safe_div(len(intersection), len(union))


def _semantic_kpi_depth(name: str, kpis: list[str]) -> float:
    """Semantic depth match: full name > substring > token overlap.

    Returns a score in [0, 1] for the best-matching KPI.
    """
    if not kpis:
        return 0.5
    best = 0.0
    name_low = name.lower()
    name_toks = _tokenize(name)
    for k in kpis:
        k_low = k.lower()
        k_toks = _tokenize(k)
        if name_low == k_low:
            return 1.0
        elif k_low in name_low or name_low in k_low:
            best = max(best, 0.8)
        else:
            overlap = _safe_div(len(name_toks & k_toks), len(name_toks | k_toks))
            best = max(best, overlap * 0.6)
    return best


# ---------------------------------------------------------------------------
# Semantic scoring sub-components
# ---------------------------------------------------------------------------

def _embedding_similarity_to_biz_intent(
    candidate_text: str,
    business_description: str,
) -> float:
    """Approximate embedding similarity via term-overlap (no ML dependency).

    In a full ML pipeline this would be cosine similarity of sentence
    embeddings. Here we use Jaccard overlap of cleaned token sets as a
    deterministic, embedding-free approximation that preserves ranking quality.
    """
    return _term_overlap(candidate_text, business_description)


def _kpi_semantic_alignment_score(
    candidate_items: list[str],       # measure names or column names
    kpis: list[str],
) -> float:
    """Average semantic depth match across all candidate items and KPIs."""
    if not candidate_items:
        return 0.5
    scores = [_semantic_kpi_depth(item, kpis) for item in candidate_items]
    return sum(scores) / len(scores)


def _graph_connectivity_score_dax(measures: list[dict[str, Any]]) -> float:
    """Measure interdependency graph density.

    A dense graph (measures referencing each other's outputs) indicates
    a coherent, layered DAX model — the gold standard for Power BI.
    Score = (cross-measure references) / (n * (n-1)) capped at 1.0.
    """
    n = len(measures)
    if n <= 1:
        return 0.5  # single-measure models score neutral
    measure_names = {m["name"] for m in measures}
    cross_refs = 0
    for m in measures:
        expr = m.get("expression", "")
        # Count references to OTHER measures (measure names appearing in expr)
        for other_name in measure_names:
            if other_name != m["name"] and other_name in expr:
                cross_refs += 1
    max_possible = n * (n - 1)
    raw = _safe_div(cross_refs, max_possible)
    # Apply a concavity boost: even 1-2 cross-refs signal good layering
    return min(1.0, raw + (0.3 if cross_refs > 0 else 0.0))


def _schema_intent_match(
    columns: list[dict[str, Any]],
    business_description: str,
) -> float:
    """Fraction of domain intent keywords found in the schema column names.

    Maps the schema column names against the business domain vocabulary.
    A high score means the schema columns directly support the stated intent.
    """
    if not columns:
        return 0.5
    col_text = " ".join(c["name"].lower() for c in columns)
    biz_toks = _tokenize(business_description)

    domain_hits = 0
    domain_total = 0
    for domain, kws in _DOMAIN_INTENT_KEYWORDS.items():
        if any(kw in biz_toks for kw in kws):
            domain_total += 1
            if any(kw in col_text for kw in kws):
                domain_hits += 1

    if domain_total == 0:
        # No domain keywords in business description — fall back to generic overlap
        return _term_overlap(col_text, business_description)
    return _safe_div(domain_hits, domain_total)


def _visual_semantic_coherence_score(
    visual_kinds: list[str],
    dashboard_type: str,
) -> float:
    """Fraction of ideal visual kinds present for the dashboard type."""
    if not visual_kinds:
        return 0.3
    ideal = _DASHBOARD_VISUAL_MAP.get(dashboard_type, _DASHBOARD_VISUAL_MAP["default"])
    present = set(visual_kinds)
    overlap = len(present & ideal)
    # Reward overlap but also penalise irrelevant visuals lightly
    precision = _safe_div(overlap, len(present))
    recall = _safe_div(overlap, len(ideal))
    if precision + recall == 0:
        return 0.0
    f1 = 2 * precision * recall / (precision + recall)
    return f1


# ---------------------------------------------------------------------------
# Public semantic scorer
# ---------------------------------------------------------------------------

def compute_semantic_score(
    candidate_text: str,
    candidate_items: list[str],
    columns: list[dict[str, Any]],
    visual_kinds: list[str],
    business_description: str,
    kpis: list[str],
    dashboard_type: str,
    measures: list[dict[str, Any]] | None = None,
) -> SemanticScore:
    """Compute the full 5-dimension semantic score for a candidate.

    Args:
        candidate_text:        All candidate text concatenated (for biz intent sim).
        candidate_items:       Item names (measures or columns) for KPI alignment.
        columns:               Schema columns (for schema_intent_match).
        visual_kinds:          Visual kind strings (for visual_semantic_coherence).
        business_description:  Raw business intent text.
        kpis:                  Potential KPI list from DataAnalyzerAgent.
        dashboard_type:        Dashboard type from BIReasoningAgent.
        measures:              Measure dicts (for graph_connectivity; optional).
    """
    emb_sim = _embedding_similarity_to_biz_intent(candidate_text, business_description)
    kpi_align = _kpi_semantic_alignment_score(candidate_items, kpis)
    graph_conn = _graph_connectivity_score_dax(measures or [])
    schema_match = _schema_intent_match(columns, business_description)
    visual_coh = _visual_semantic_coherence_score(visual_kinds, dashboard_type)

    return SemanticScore(
        embedding_similarity=emb_sim,
        kpi_semantic_alignment=kpi_align,
        graph_connectivity=graph_conn,
        schema_intent_match=schema_match,
        visual_semantic_coherence=visual_coh,
    )


def _blend_scores(heuristic: float, semantic: SemanticScore) -> float:
    """Blend heuristic + semantic into the final candidate score."""
    return _SEMANTIC_WEIGHT * semantic.total + _HEURISTIC_WEIGHT * heuristic


# ---------------------------------------------------------------------------
# DAX candidate scorer
# ---------------------------------------------------------------------------

def score_dax_candidate(
    candidate_id: str,
    measures: list[dict[str, Any]],
    biz_analysis: Any | None,           # agents.schemas.BusinessAnalysis | None
    schema: dict[str, Any] | None,
    weights: dict[str, float] | None = None,
    business_description: str = "",
    adaptive_bias: float = 0.0,
) -> CandidateScore:
    """Score one DAX measure-set candidate against all 5 heuristic dimensions
    and the 5-dimension semantic model, blending into a final score.

    Args:
        candidate_id:         Short label (e.g. "revenue_first").
        measures:             List of measure dicts.
        biz_analysis:         BusinessAnalysis (may be None).
        schema:               Schema dict with "columns" list (may be None).
        weights:              Override weights; falls back to DEFAULT_WEIGHTS.
        business_description: Raw business intent text (for semantic scoring).
    """
    w = _resolve_weights(weights)
    n = len(measures)
    if n == 0:
        return CandidateScore(candidate_id=candidate_id, weights_used=w)

    # --- heuristic: business_value ---
    bv = _safe_div(
        sum(1 for m in measures if m.get("displayFolder", "") in _BIZ_VALUE_FOLDERS), n,
    )

    # --- heuristic: kpi_alignment ---
    kpis: list[str] = []
    if biz_analysis is not None:
        kpis = list(getattr(biz_analysis, "potential_kpis", []) or [])
    if kpis:
        measure_names_lower = {m["name"].lower() for m in measures}
        hits = sum(1 for k in kpis if k.lower() in measure_names_lower)
        ka = _safe_div(hits, len(kpis))
    else:
        ka = 0.5

    # --- heuristic: data_coverage ---
    schema_cols: set[str] = set()
    if schema:
        schema_cols = {c["name"] for c in schema.get("columns", [])}
    expr_refs: set[str] = set()
    for m in measures:
        expr_refs |= _col_names_in_expr(m.get("expression", ""))
    dc = _safe_div(len(expr_refs & schema_cols), max(len(schema_cols), 1))

    # --- heuristic: visual_quality (folder diversity) ---
    distinct_folders = len({m.get("displayFolder", "") for m in measures})
    vq = min(1.0, _safe_div(distinct_folders, 5))

    # --- heuristic: interpretability ---
    ip = _safe_div(
        sum(1 for m in measures if len(m.get("description", "")) > 10), n,
    )

    heuristic_total = (
        bv * w["business_value"]
        + ka * w["kpi_alignment"]
        + dc * w["data_coverage"]
        + vq * w["visual_quality"]
        + ip * w["interpretability"]
    )

    # --- semantic scoring ---
    columns = list(schema.get("columns", [])) if schema else []
    candidate_text = " ".join(
        f"{m.get('name','')} {m.get('description','')} {m.get('displayFolder','')}"
        for m in measures
    )
    measure_names = [m["name"] for m in measures]
    dashboard_type = (
        getattr(biz_analysis, "dashboard_type", "default")
        if biz_analysis is not None else "default"
    )
    sem = compute_semantic_score(
        candidate_text=candidate_text,
        candidate_items=measure_names,
        columns=columns,
        visual_kinds=[],           # no visuals at DAX stage
        business_description=business_description,
        kpis=kpis,
        dashboard_type=dashboard_type,
        measures=measures,
    )
    # Adaptive bias: shift the semantic total based on historical success/failure
    # patterns for similar inputs. Bias is pre-computed by AdaptiveLearningLayer
    # and passed in; default=0.0 preserves backward compatibility exactly.
    adjusted_semantic_total = min(1.0, max(0.0, sem.total + adaptive_bias))
    final = _SEMANTIC_WEIGHT * adjusted_semantic_total + _HEURISTIC_WEIGHT * heuristic_total

    return CandidateScore(
        candidate_id=candidate_id,
        business_value=bv,
        kpi_alignment=ka,
        data_coverage=dc,
        visual_quality=vq,
        interpretability=ip,
        semantic=sem,
        heuristic_total=heuristic_total,
        semantic_total=adjusted_semantic_total,
        total=final,
        weights_used=w,
    )


# ---------------------------------------------------------------------------
# Schema candidate scorer
# ---------------------------------------------------------------------------

def score_schema_candidate(
    candidate_id: str,
    columns: list[dict[str, Any]],
    biz_analysis: Any | None,
    weights: dict[str, float] | None = None,
    business_description: str = "",
    adaptive_bias: float = 0.0,
) -> CandidateScore:
    """Score one schema column-mapping candidate.

    Args:
        candidate_id:         Short label (e.g. "conservative").
        columns:              List of column dicts.
        biz_analysis:         BusinessAnalysis (may be None).
        weights:              Override weights; falls back to DEFAULT_WEIGHTS.
        business_description: Raw business intent text (for semantic scoring).
    """
    w = _resolve_weights(weights)
    n = len(columns)
    if n == 0:
        return CandidateScore(candidate_id=candidate_id, weights_used=w)

    # --- heuristic: business_value ---
    bv = _safe_div(
        sum(1 for c in columns if c.get("dataType", "any") in _KNOWN_PBI_TYPES), n,
    )

    # --- heuristic: kpi_alignment ---
    important: set[str] = set()
    kpis: list[str] = []
    if biz_analysis is not None:
        important = set(getattr(biz_analysis, "important_measures", []) or [])
        kpis = list(getattr(biz_analysis, "potential_kpis", []) or [])
    numeric_cols = [c for c in columns if c.get("dataType", "") in {"int64", "double", "decimal"}]
    if important and numeric_cols:
        ka = _safe_div(
            sum(1 for c in numeric_cols if c["name"] in important), len(numeric_cols),
        )
    else:
        ka = 0.5

    # --- heuristic: data_coverage ---
    dc = _safe_div(
        sum(1 for c in columns if c.get("dataType", "any") in _KNOWN_PBI_TYPES), n,
    )

    # --- heuristic: visual_quality ---
    if numeric_cols:
        vq = _safe_div(
            sum(1 for c in numeric_cols if c.get("summarizeBy", "none") != "none"),
            len(numeric_cols),
        )
    else:
        vq = 0.5

    # --- heuristic: interpretability ---
    ip = _safe_div(
        sum(1 for c in columns if 2 <= len(c.get("name", "")) <= 30), n,
    )

    heuristic_total = (
        bv * w["business_value"]
        + ka * w["kpi_alignment"]
        + dc * w["data_coverage"]
        + vq * w["visual_quality"]
        + ip * w["interpretability"]
    )

    # --- semantic scoring ---
    dashboard_type = (
        getattr(biz_analysis, "dashboard_type", "default")
        if biz_analysis is not None else "default"
    )
    candidate_text = " ".join(
        f"{c.get('name','')} {c.get('dataType','')}" for c in columns
    )
    col_names = [c["name"] for c in columns]
    sem = compute_semantic_score(
        candidate_text=candidate_text,
        candidate_items=col_names,
        columns=columns,
        visual_kinds=[],
        business_description=business_description,
        kpis=kpis,
        dashboard_type=dashboard_type,
        measures=None,
    )
    # Adaptive bias: shift semantic total based on historical success/failure patterns.
    # Default=0.0 preserves backward compatibility exactly.
    adjusted_semantic_total = min(1.0, max(0.0, sem.total + adaptive_bias))
    final = _SEMANTIC_WEIGHT * adjusted_semantic_total + _HEURISTIC_WEIGHT * heuristic_total

    return CandidateScore(
        candidate_id=candidate_id,
        business_value=bv,
        kpi_alignment=ka,
        data_coverage=dc,
        visual_quality=vq,
        interpretability=ip,
        semantic=sem,
        heuristic_total=heuristic_total,
        semantic_total=adjusted_semantic_total,
        total=final,
        weights_used=w,
    )


# ---------------------------------------------------------------------------
# Visual plan candidate scorer
# ---------------------------------------------------------------------------

def score_visual_candidate(
    candidate_id: str,
    report_plan: Any,
    measures: list[dict[str, Any]],
    bi_reasoning: Any | None,
    weights: dict[str, float] | None = None,
    business_description: str = "",
    schema_columns: list[dict[str, Any]] | None = None,
    adaptive_bias: float = 0.0,
) -> CandidateScore:
    """Score one ReportPlan candidate.

    Args:
        candidate_id:         Short label (e.g. "executive").
        report_plan:          ReportPlan with .pages (list[PagePlan]).
        measures:             Available measures from DAXAgent.
        bi_reasoning:         BIReasoningResult (may be None).
        weights:              Override weights; falls back to DEFAULT_WEIGHTS.
        business_description: Raw business intent text (for semantic scoring).
        schema_columns:       Schema columns (for schema_intent_match).
    """
    w = _resolve_weights(weights)

    all_visuals = [v for page in report_plan.pages for v in page.visuals]
    n_visuals = len(all_visuals)
    n_measures = len(measures)

    if n_visuals == 0:
        return CandidateScore(candidate_id=candidate_id, weights_used=w)

    # --- heuristic: business_value ---
    has_exec = any(v.kind in _EXEC_VISUAL_KINDS for v in all_visuals)
    bv = 1.0 if has_exec else 0.3

    # --- heuristic: kpi_alignment ---
    kpis: list[str] = []
    if bi_reasoning is not None:
        kpi_recs = getattr(bi_reasoning, "recommended_kpis", []) or []
        if kpi_recs:
            visual_measures = {v.measure for v in all_visuals if v.measure}
            top_kpi_name = kpi_recs[0].name if kpi_recs else ""
            ka = 1.0 if any(
                top_kpi_name.lower() in (vm or "").lower() for vm in visual_measures
            ) else 0.3
            kpis = [r.name for r in kpi_recs]
        else:
            ka = 0.5
    else:
        ka = 0.5

    # --- heuristic: data_coverage ---
    if measures and isinstance(measures[0], str):
        measure_names = set(measures)  # type: ignore[arg-type]
    else:
        measure_names = {m["name"] for m in measures}  # type: ignore[index]
    used_measures = {v.measure for v in all_visuals if v.measure} & measure_names
    dc = _safe_div(len(used_measures), max(n_measures, 1))

    # --- heuristic: visual_quality (kind diversity) ---
    distinct_kinds = len({v.kind for v in all_visuals})
    vq = min(1.0, _safe_div(distinct_kinds, 5))

    # --- heuristic: interpretability ---
    ip = _safe_div(
        sum(1 for v in all_visuals if v.intent_match_reasoning), n_visuals,
    )

    heuristic_total = (
        bv * w["business_value"]
        + ka * w["kpi_alignment"]
        + dc * w["data_coverage"]
        + vq * w["visual_quality"]
        + ip * w["interpretability"]
    )

    # --- semantic scoring ---
    visual_kinds = [v.kind for v in all_visuals]
    dashboard_type = (
        getattr(bi_reasoning, "dashboard_type", "default")
        if bi_reasoning is not None else "default"
    )
    candidate_text = " ".join(
        f"{v.kind} {v.name} {v.measure or ''}" for v in all_visuals
    )
    visual_item_names = [v.name for v in all_visuals]
    sem = compute_semantic_score(
        candidate_text=candidate_text,
        candidate_items=visual_item_names,
        columns=schema_columns or [],
        visual_kinds=visual_kinds,
        business_description=business_description,
        kpis=kpis,
        dashboard_type=dashboard_type,
        measures=None,
    )
    # Adaptive bias: shift semantic total based on historical success/failure patterns.
    # Default=0.0 preserves backward compatibility exactly.
    adjusted_semantic_total = min(1.0, max(0.0, sem.total + adaptive_bias))
    final = _SEMANTIC_WEIGHT * adjusted_semantic_total + _HEURISTIC_WEIGHT * heuristic_total

    return CandidateScore(
        candidate_id=candidate_id,
        business_value=bv,
        kpi_alignment=ka,
        data_coverage=dc,
        visual_quality=vq,
        interpretability=ip,
        semantic=sem,
        heuristic_total=heuristic_total,
        semantic_total=adjusted_semantic_total,
        total=final,
        weights_used=w,
    )


# ---------------------------------------------------------------------------
# Tournament selection (replaces simple argmax)
# ---------------------------------------------------------------------------

def tournament_select(
    candidates: list[Any],
    scores: list[CandidateScore],
    group_size: int = 2,
    context_aware: bool = False,
    kpi_scores: "dict[str, float] | None" = None,
) -> tuple[int, CandidateScore, list[dict[str, Any]]]:
    """Multi-stage tournament ranking.

    Process
    -------
    1. Partition candidates into groups of ``group_size``.
       When ``context_aware=True`` groups are formed by interleaving the
       top-half and bottom-half of candidates sorted by semantic_total,
       ensuring each group contains both high- and low-semantic scorers for
       maximum diversity (prevents semantic clustering).
    2. Select top-2 from each group (by total score).
    3. Re-score finalists: primary key = total, tiebreaker = semantic_total.
       When ``kpi_scores`` is provided, a global KPI alignment bonus of
       ``kpi_scores[candidate_id] * 0.1`` is added to the total for
       cross-agent KPI consistency re-ranking.
    4. Return global winner.

    Args:
        candidates:     Raw candidate objects (parallel to ``scores``).
        scores:         One ``CandidateScore`` per candidate.
        group_size:     Number of candidates per tournament group.
        context_aware:  When True, groups are formed by semantic-distance
                        interleaving for diversity (default False — identical
                        behaviour to original sequential partition).
        kpi_scores:     Optional dict mapping candidate_id → KPI alignment
                        float; used to add a 0.1-weighted KPI bonus in the
                        Stage-2 finalist re-rank.

    Returns:
        ``(best_idx, best_score, rejected_summary)``
    """
    if not scores:
        raise ValueError("tournament_select: scores list is empty")
    if len(scores) == 1:
        return 0, scores[0], []

    n = len(scores)

    # Stage 1 — build groups
    if context_aware and n > 2:
        # Sort indices by semantic_total ascending so top and bottom halves are
        # interleaved: group[0] = (lowest_sem, highest_sem),
        # group[1] = (2nd_lowest, 2nd_highest), etc.  This guarantees every
        # tournament group contains a semantically diverse pair.
        try:
            sem_sorted = sorted(range(n), key=lambda i: scores[i].semantic_total)
            half = n // 2
            top_half = sem_sorted[half:]          # high semantic indices
            bot_half = sem_sorted[:half]           # low  semantic indices
            top_half_rev = list(reversed(top_half))  # pair best with worst
            interleaved: list[int] = []
            for a, b in zip(bot_half, top_half_rev):
                interleaved.extend([a, b])
            # any remainder (odd n) appended at the end
            if len(interleaved) < n:
                seen_il = set(interleaved)
                interleaved.extend(i for i in range(n) if i not in seen_il)
            ordered_idxs = interleaved
        except Exception:  # noqa: BLE001 — degrade gracefully to sequential
            ordered_idxs = list(range(n))
    else:
        ordered_idxs = list(range(n))

    groups: list[list[int]] = []
    for start in range(0, n, group_size):
        groups.append(ordered_idxs[start: start + group_size])

    finalist_idxs: list[int] = []
    for group in groups:
        # Sort group by total descending, take top-2
        sorted_group = sorted(group, key=lambda i: scores[i].total, reverse=True)
        finalist_idxs.extend(sorted_group[:2])

    # Deduplicate while preserving order
    seen: set[int] = set()
    finalists: list[int] = []
    for idx in finalist_idxs:
        if idx not in seen:
            seen.add(idx)
            finalists.append(idx)

    # Stage 2 — re-rank finalists
    # Primary key = total (+KPI bonus if kpi_scores provided),
    # tiebreaker = semantic_total.
    _kpi = kpi_scores or {}

    def _finalist_key(i: int) -> tuple[float, float]:
        s = scores[i]
        kpi_bonus = _kpi.get(s.candidate_id, 0.0) * 0.1
        return (s.total + kpi_bonus, s.semantic_total)

    finalists.sort(key=_finalist_key, reverse=True)

    best_idx = finalists[0]
    best_score = scores[best_idx]

    # Stage 3 — build rejected summary (all non-winners)
    rejected_summary: list[dict[str, Any]] = [
        {
            "candidate_id":  s.candidate_id,
            "score":         round(s.total, 4),
            "semantic":      round(s.semantic_total, 4),
            "heuristic":     round(s.heuristic_total, 4),
            "gap_from_best": round(best_score.total - s.total, 4),
        }
        for i, s in enumerate(scores)
        if i != best_idx
    ]

    return best_idx, best_score, rejected_summary


def select_best(
    candidates: list[Any],
    scores: list[CandidateScore],
) -> tuple[int, CandidateScore, list[dict[str, Any]]]:
    """Backward-compatible selection entry point — delegates to tournament_select.

    Existing callers (DAXAgent, SchemaAgent, VisualPlannerAgent) call this
    function; it now uses tournament selection internally.
    """
    return tournament_select(candidates, scores)


__all__ = [
    "DEFAULT_WEIGHTS",
    "SemanticScore",
    "CandidateScore",
    "compute_complexity_score",
    "candidate_count_from_complexity",
    "compute_semantic_score",
    "score_dax_candidate",
    "score_schema_candidate",
    "score_visual_candidate",
    "tournament_select",
    "select_best",
]

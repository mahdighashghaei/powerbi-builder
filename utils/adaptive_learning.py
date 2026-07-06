"""utils/adaptive_learning.py — Adaptive Learning Signal Layer.

Architecture: Top-3 Kaggle Winning Architecture — Self-Tuning Intelligence.

Bridges historical success/failure patterns with semantic scoring by computing
an *adaptive bias* that shifts candidate semantic scores based on how similar
the current input context is to past successful or failed runs.

Formula
-------
    adaptive_bias = Σ(similarity_i × pattern_weight_i × strength_i)
    final_semantic_score = base_semantic_score + adaptive_bias

where positive contributions come from success patterns and negative
contributions come from failure patterns, resulting in a score that
*evolves across runs* rather than remaining static.

Fail-safe: all public methods are exception-safe and return neutral defaults
(0.0 / 0.5) on any internal error so existing scoring flows are never broken.
"""
from __future__ import annotations

import re
from typing import Any

# Maximum absolute bias added to any semantic score
_MAX_BIAS = 0.15

# Strength multiplier for success patterns (positive pull)
_SUCCESS_STRENGTH = 0.20

# Strength multiplier for failure patterns (negative push)
_FAILURE_STRENGTH = 0.15

# Minimum similarity threshold — patterns below this are ignored
_SIMILARITY_THRESHOLD = 0.10

# Business domain vocabulary (mirrors scoring.py intent keywords)
_DOMAIN_TOKENS: dict[str, list[str]] = {
    "revenue":    ["revenue", "sales", "income", "profit", "margin", "price", "amount"],
    "operations": ["count", "volume", "quantity", "orders", "units", "rate", "duration"],
    "customer":   ["customer", "client", "user", "segment", "account", "contact"],
    "time":       ["date", "year", "month", "quarter", "period", "ytd", "yoy", "mom"],
    "geography":  ["region", "country", "city", "market", "territory", "zone"],
    "product":    ["product", "category", "brand", "segment", "sku", "item", "type"],
}


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens from any text string."""
    return set(re.findall(r"[a-z][a-z0-9]*", text.lower()))


def _domain_fingerprint(text: str) -> set[str]:
    """Return the set of domain names that match keywords in *text*."""
    toks = _tokenize(text)
    return {domain for domain, kws in _DOMAIN_TOKENS.items() if any(k in toks for k in kws)}


def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    return num / den if den > 0 else default


class AdaptiveLearningLayer:
    """Computes context-similarity-weighted adaptive bias for semantic scoring.

    The layer is stateless — it holds no mutable run state. All data flows
    through the ``success_patterns`` / ``failure_patterns`` lists that the
    orchestrator passes in from ``LearningMemory``.

    Usage
    -----
    ::

        layer = AdaptiveLearningLayer()
        bias  = layer.compute_adaptive_bias(
            base_semantic_score  = sem.total,
            success_patterns     = memory.get_success_patterns(cluster),
            failure_patterns     = memory.get_failure_patterns(cluster),
            current_context      = {"description": ctx.business_description},
        )
        adjusted_semantic = min(1.0, max(0.0, sem.total + bias))
    """

    # ------------------------------------------------------------------
    # Public API (static — the layer is stateless so instance is optional)
    # ------------------------------------------------------------------

    @staticmethod
    def compute_context_similarity(
        ctx_a: dict[str, Any] | None,
        ctx_b: dict[str, Any] | None,
    ) -> float:
        """Return a [0, 1] similarity score between two context dicts.

        Combines:
          * Jaccard token overlap of ``description`` fields
          * Domain fingerprint overlap (both contexts cover the same domains)

        Args:
            ctx_a: dict with at least ``"description": str`` (or None)
            ctx_b: dict with at least ``"description": str`` (or None)
        """
        try:
            _ca = ctx_a or {}
            _cb = ctx_b or {}
            desc_a = str(_ca.get("description", "") or "")
            desc_b = str(_cb.get("description", "") or "")

            # --- Jaccard token overlap -----------------------------------
            toks_a = _tokenize(desc_a)
            toks_b = _tokenize(desc_b)
            if toks_a or toks_b:
                jaccard = _safe_div(
                    len(toks_a & toks_b),
                    len(toks_a | toks_b),
                )
            else:
                jaccard = 0.0

            # --- Domain fingerprint overlap ------------------------------
            dom_a = _domain_fingerprint(desc_a)
            dom_b = _domain_fingerprint(desc_b)
            if dom_a or dom_b:
                domain_sim = _safe_div(
                    len(dom_a & dom_b),
                    len(dom_a | dom_b),
                )
            else:
                domain_sim = 0.0

            # 60 % token overlap, 40 % domain overlap
            return 0.60 * jaccard + 0.40 * domain_sim
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def compute_adaptive_bias(
        base_semantic_score: float,
        success_patterns: list[dict[str, Any]] | None,
        failure_patterns: list[dict[str, Any]] | None,
        current_context: dict[str, Any] | None,
    ) -> float:
        """Compute the adaptive bias to add to a base semantic score.

        Algorithm
        ---------
        For each success pattern above the similarity threshold:
            bias += similarity × pattern_confidence × SUCCESS_STRENGTH

        For each failure pattern above the similarity threshold:
            bias -= similarity × pattern_confidence × FAILURE_STRENGTH

        The result is clamped to ``[-MAX_BIAS, +MAX_BIAS]`` so the bias
        never overwhelms the base semantic score.

        Args:
            base_semantic_score:  The raw semantic score before any bias.
            success_patterns:     Patterns from ``LearningMemory.get_success_patterns()``.
                                  Each dict has at least ``"context"`` and
                                  ``"semantic_score"`` keys.
            failure_patterns:     Patterns from ``LearningMemory.get_failure_patterns()``.
                                  Same schema as success_patterns.
            current_context:      Dict describing the current input (``"description"``,
                                  optionally ``"domain"``, ``"kpi_count"``, etc.).

        Returns:
            Bias float in ``[-MAX_BIAS, +MAX_BIAS]``.
        """
        try:
            _success = success_patterns or []
            _failure = failure_patterns or []
            _ctx = current_context or {}

            if not _success and not _failure:
                return 0.0

            bias = 0.0

            # --- Positive contribution from success patterns -------------
            for pattern in _success:
                pattern_ctx = pattern.get("context", {})
                if not isinstance(pattern_ctx, dict):
                    pattern_ctx = {"description": str(pattern_ctx)}
                sim = AdaptiveLearningLayer.compute_context_similarity(_ctx, pattern_ctx)
                if sim < _SIMILARITY_THRESHOLD:
                    continue
                # pattern_confidence = normalised semantic_score of the past winner
                confidence = float(pattern.get("semantic_score", 0.5))
                bias += sim * confidence * _SUCCESS_STRENGTH

            # --- Negative contribution from failure patterns -------------
            for pattern in _failure:
                pattern_ctx = pattern.get("context", {})
                if not isinstance(pattern_ctx, dict):
                    pattern_ctx = {"description": str(pattern_ctx)}
                sim = AdaptiveLearningLayer.compute_context_similarity(_ctx, pattern_ctx)
                if sim < _SIMILARITY_THRESHOLD:
                    continue
                confidence = float(pattern.get("semantic_score", 0.5))
                bias -= sim * confidence * _FAILURE_STRENGTH

            return max(-_MAX_BIAS, min(_MAX_BIAS, bias))
        except Exception:  # noqa: BLE001
            return 0.0


__all__ = ["AdaptiveLearningLayer"]

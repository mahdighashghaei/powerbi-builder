"""utils/learning_memory.py — Cross-Run Learning Memory.

Architecture: Top-3 Kaggle Winning Architecture — Self-Improving Memory.

Stores winning strategies and failure patterns per *input cluster* so the
system improves across runs, not just within one run.

Memory schema (``learning_memory.json``)
-----------------------------------------
::

    {
      "version": 1,
      "total_runs": 12,
      "total_judge_overrides": 4,
      "clusters": {
        "finance_kpi5to9_cols15to29": {
          "success": [
            {
              "candidate_id":   "kpi_focused",
              "semantic_score": 0.82,
              "context":        {"description": "sales revenue dashboard ..."},
              "timestamp":      "2026-07-04T12:00:00+00:00"
            }
          ],
          "failure": [
            {
              "candidate_id":   "conservative",
              "semantic_score": 0.41,
              "context":        {"description": "..."},
              "timestamp":      "2026-07-04T10:00:00+00:00"
            }
          ]
        }
      }
    }

Cluster key formula
-------------------
    domain_tag + "_kpi" + kpi_bucket + "_cols" + col_bucket

where:
    domain_tag  = top business domain detected (finance / ops / customer / …)
    kpi_bucket  = "0to4" | "5to9" | "10plus"
    col_bucket  = "0to9" | "10to29" | "30plus"

Fail-safe contract
------------------
All public methods are exception-safe and return neutral defaults on any
internal error so the pipeline is never blocked.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Cluster key helpers
# ---------------------------------------------------------------------------

_DOMAIN_KEYWORD_MAP: dict[str, list[str]] = {
    "finance":    ["revenue", "sales", "profit", "finance", "cost", "margin",
                   "income", "price", "payment"],
    "operations": ["operation", "inventory", "warehouse", "logistics", "supply",
                   "process", "workflow", "order", "shipment"],
    "customer":   ["customer", "client", "user", "segment", "account",
                   "retention", "churn", "acquisition", "crm"],
    "hr":         ["employee", "staff", "headcount", "payroll", "talent",
                   "workforce", "salary", "recruit"],
    "marketing":  ["marketing", "campaign", "ad", "conversion", "channel",
                   "traffic", "impression", "click"],
    "analytics":  ["analytics", "analysis", "trend", "forecast", "predict",
                   "model", "report", "insight", "kpi"],
}

_KPI_BUCKETS = [(0, 4, "0to4"), (5, 9, "5to9"), (10, 999, "10plus")]
_COL_BUCKETS = [(0, 9, "0to9"), (10, 29, "10to29"), (30, 9999, "30plus")]


def _detect_domain(business_description: str) -> str:
    """Return the best-matching domain name for a business description."""
    desc_lower = business_description.lower()
    scores: dict[str, int] = {}
    for domain, kws in _DOMAIN_KEYWORD_MAP.items():
        scores[domain] = sum(1 for kw in kws if kw in desc_lower)
    best = max(scores, key=lambda d: scores[d])
    return best if scores[best] > 0 else "general"


def _bucket(value: int, buckets: list[tuple[int, int, str]]) -> str:
    for lo, hi, label in buckets:
        if lo <= value <= hi:
            return label
    return buckets[-1][2]


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# Max patterns stored per cluster side (success / failure)
_MAX_PATTERNS_PER_SIDE = 20


class LearningMemory:
    """Persistent cross-run learning store.

    Persists ``learning_memory.json`` in the project's run directory.
    Multiple agents in the same run share a single in-process instance,
    retrieved via ``ctx.extra["_learning_memory"]``.

    Thread safety: not required — the orchestrator is single-threaded
    at the points where this object is read and written.
    """

    def __init__(self, memory_path: Path) -> None:
        self._path = Path(memory_path)
        self._data: dict[str, Any] = {
            "version": 1,
            "total_runs": 0,
            "total_judge_overrides": 0,
            "clusters": {},
            "strategies": {},
            "semantic_models": {},
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load memory from disk; silently no-op if file does not exist."""
        try:
            if self._path.exists():
                with open(self._path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self._data = loaded
        except Exception:  # noqa: BLE001
            pass  # fresh start on any parse error

    def save(self) -> None:
        """Write memory to disk atomically (best-effort)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
            tmp.replace(self._path)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Cluster key
    # ------------------------------------------------------------------

    def cluster_input(
        self,
        business_description: str,
        schema_columns: list[dict[str, Any]],
        kpi_list: list[str],
    ) -> str:
        """Return a stable cluster key for an input triple.

        The key is deterministic and human-readable so it can be inspected
        in ``learning_memory.json`` without decoding. Example::

            "finance_kpi5to9_cols15to29"

        Args:
            business_description: Raw business intent text.
            schema_columns:       List of column dicts (uses ``len()``).
            kpi_list:             List of KPI name strings (uses ``len()``).
        """
        try:
            domain = _detect_domain(business_description or "")
            kpi_bucket = _bucket(len(kpi_list or []), _KPI_BUCKETS)
            col_bucket  = _bucket(len(schema_columns or []), _COL_BUCKETS)
            return f"{domain}_kpi{kpi_bucket}_cols{col_bucket}"
        except Exception:  # noqa: BLE001
            return "general_kpi0to4_cols0to9"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        cluster: str,
        candidate_id: str,
        semantic_score: float,
        success: bool,
        judge_overridden: bool,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Record a run outcome into the cluster's success or failure list.

        Args:
            cluster:         Cluster key from :meth:`cluster_input`.
            candidate_id:    The winning candidate strategy ID.
            semantic_score:  The winning candidate's semantic_total score.
            success:         True when quality_score >= 80 for this run.
            judge_overridden: True when JudgeLayer emitted override_actions.
            context:         Optional dict to store alongside the pattern
                             (used by AdaptiveLearningLayer for similarity).
        """
        try:
            self._data["total_runs"] = self._data.get("total_runs", 0) + 1
            if judge_overridden:
                self._data["total_judge_overrides"] = (
                    self._data.get("total_judge_overrides", 0) + 1
                )

            clusters = self._data.setdefault("clusters", {})
            slot = clusters.setdefault(cluster, {"success": [], "failure": []})
            entry: dict[str, Any] = {
                "candidate_id":   candidate_id,
                "semantic_score": round(float(semantic_score), 4),
                "context":        context or {},
                "judge_overridden": judge_overridden,
                "timestamp":      _utc_now(),
            }
            side = "success" if success else "failure"
            slot[side].append(entry)
            # Trim to max to keep memory bounded
            if len(slot[side]) > _MAX_PATTERNS_PER_SIDE:
                # Keep the most recent patterns (FIFO trim from front)
                slot[side] = slot[side][-_MAX_PATTERNS_PER_SIDE:]
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_success_patterns(
        self,
        cluster: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Return the top-k success patterns for *cluster*.

        Patterns are sorted by ``semantic_score`` descending so the highest-
        quality winning strategies are considered first.

        Args:
            cluster: Cluster key from :meth:`cluster_input`.
            top_k:   Maximum number of patterns to return.
        """
        try:
            slot = self._data.get("clusters", {}).get(cluster, {})
            patterns = slot.get("success", [])
            sorted_p = sorted(
                patterns,
                key=lambda p: float(p.get("semantic_score", 0.0)),
                reverse=True,
            )
            return sorted_p[:top_k]
        except Exception:  # noqa: BLE001
            return []

    def get_failure_patterns(
        self,
        cluster: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Return the top-k failure patterns for *cluster*.

        Patterns with the highest ``semantic_score`` (i.e. high-scoring but
        still failing runs) are surfaced first — these are the most dangerous
        candidates to avoid.

        Args:
            cluster: Cluster key from :meth:`cluster_input`.
            top_k:   Maximum number of patterns to return.
        """
        try:
            slot = self._data.get("clusters", {}).get(cluster, {})
            patterns = slot.get("failure", [])
            sorted_p = sorted(
                patterns,
                key=lambda p: float(p.get("semantic_score", 0.0)),
                reverse=True,
            )
            return sorted_p[:top_k]
        except Exception:  # noqa: BLE001
            return []

    def get_judge_override_frequency(self) -> float:
        """Return the fraction of total runs that triggered a judge override.

        Returns:
            Float in [0, 1]; returns 0.0 when no runs have been recorded.
        """
        try:
            total = int(self._data.get("total_runs", 0))
            overrides = int(self._data.get("total_judge_overrides", 0))
            return overrides / total if total > 0 else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    # ------------------------------------------------------------------
    # Strategy Synthesis feedback loop (additive)
    # ------------------------------------------------------------------
    #
    # Tracks per-strategy usage/success independent of the per-cluster
    # success/failure lists above. ``strategy_id`` values produced by
    # ``utils.strategy_synthesizer.StrategySynthesizer`` are always prefixed
    # ``synth_`` -- that prefix is the cheap marker used to distinguish
    # synthesized from base (hand-written) strategies without threading a
    # new flag through every candidate structure.

    #: strength below this floor after decay marks a strategy for pruning.
    _DEFAULT_STRENGTH_FLOOR = 0.1

    def record_strategy_outcome(
        self,
        strategy_id: str,
        success: bool,
        is_synthesized: bool | None = None,
    ) -> None:
        """Record one usage of *strategy_id* and whether it won a successful run.

        Args:
            strategy_id:     The winning candidate's ``candidate_id``.
            success:         True when the run's overall quality was acceptable.
            is_synthesized:  Explicit override; auto-detected from the
                             ``synth_`` prefix when not provided.
        """
        try:
            if not strategy_id:
                return
            synthesized = (
                is_synthesized if is_synthesized is not None
                else strategy_id.startswith("synth_")
            )
            strategies = self._data.setdefault("strategies", {})
            rec = strategies.setdefault(strategy_id, {
                "uses": 0, "successes": 0, "is_synthesized": synthesized,
                "strength": 1.0, "last_used": None,
            })
            rec["uses"] = int(rec.get("uses", 0)) + 1
            if success:
                rec["successes"] = int(rec.get("successes", 0)) + 1
            rec["is_synthesized"] = synthesized
            rec["last_used"] = _utc_now()
        except Exception:  # noqa: BLE001
            pass

    def get_strategy_success_rate(self, strategy_id: str) -> float:
        """Return the historical success rate of *strategy_id* in ``[0, 1]``.

        Returns ``0.5`` (neutral) for a strategy with no recorded uses.
        """
        try:
            rec = self._data.get("strategies", {}).get(strategy_id)
            if not rec or int(rec.get("uses", 0)) == 0:
                return 0.5
            return float(rec.get("successes", 0)) / float(rec["uses"])
        except Exception:  # noqa: BLE001
            return 0.5

    def decay_weak_strategies(
        self,
        min_uses: int = 3,
        weak_threshold: float = 0.3,
        decay: float = 0.9,
    ) -> None:
        """Shrink ``strength`` for strategies that keep under-performing.

        A strategy qualifies for decay once it has at least ``min_uses``
        recorded uses AND its success rate is below ``weak_threshold``.
        Repeated decay calls (one per run) compound, so a persistently weak
        strategy's strength trends toward zero and eventually crosses the
        prune floor in :meth:`prune_strategies`.
        """
        try:
            for strategy_id, rec in self._data.get("strategies", {}).items():
                uses = int(rec.get("uses", 0))
                if uses < min_uses:
                    continue
                successes = int(rec.get("successes", 0))
                rate = successes / uses if uses > 0 else 0.5
                if rate < weak_threshold:
                    rec["strength"] = round(float(rec.get("strength", 1.0)) * decay, 6)
        except Exception:  # noqa: BLE001
            pass

    def prune_strategies(self, strength_floor: float | None = None) -> list[str]:
        """Remove strategies whose ``strength`` has decayed below the floor.

        Returns:
            The list of pruned strategy_ids (for logging/diagnostics). The
            caller can feed this list back in as an exclusion set to
            ``StrategySynthesizer`` / ``current_strategy_pool`` so
            known-bad synthesized strategies are not regenerated.
        """
        try:
            floor = (
                strength_floor if strength_floor is not None
                else self._DEFAULT_STRENGTH_FLOOR
            )
            strategies = self._data.get("strategies", {})
            pruned = [
                sid for sid, rec in strategies.items()
                if float(rec.get("strength", 1.0)) < floor
            ]
            for sid in pruned:
                del strategies[sid]
            return pruned
        except Exception:  # noqa: BLE001
            return []

    def get_active_strategy_pool(self, domain: str | None = None) -> list[str]:
        """Return known strategy_ids that have not been pruned.

        Args:
            domain: Optional filter — when provided, only ids containing
                    ``f"_{domain}_"`` (synthesized ids embed their domain,
                    e.g. ``synth_dax_kpi_gap_fill_1``) or base ids with no
                    domain marker are returned.
        """
        try:
            ids = list(self._data.get("strategies", {}).keys())
            if not domain:
                return sorted(ids)
            return sorted(
                sid for sid in ids
                if f"_{domain}_" in sid or not sid.startswith("synth_")
            )
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------
    # Semantic Truth Layer persistence (additive)
    # ------------------------------------------------------------------
    #
    # Stores the semantic model discovered by ``utils.semantic_model`` for a
    # given schema fingerprint, so the same relationships (e.g. "Sales =
    # Gross Sales - Discounts") are never empirically rediscovered on a
    # future run against the same schema shape.

    def get_semantic_model(self, fingerprint: str) -> dict[str, Any] | None:
        """Return the cached semantic model for *fingerprint*, or ``None``."""
        try:
            return self._data.get("semantic_models", {}).get(fingerprint)
        except Exception:  # noqa: BLE001
            return None

    def set_semantic_model(self, fingerprint: str, model: dict[str, Any]) -> None:
        """Persist the discovered semantic model for *fingerprint*."""
        try:
            if not fingerprint or not model:
                return
            models = self._data.setdefault("semantic_models", {})
            models[fingerprint] = model
        except Exception:  # noqa: BLE001
            pass

    def get_all_winning_strategies(self) -> dict[str, list[str]]:
        """Return per-cluster winning strategy IDs (for diagnostics/logging).

        Returns:
            Dict mapping cluster key → sorted list of unique candidate_ids
            that have appeared in success patterns for that cluster.
        """
        try:
            result: dict[str, list[str]] = {}
            for cluster, slot in self._data.get("clusters", {}).items():
                ids = sorted({p["candidate_id"] for p in slot.get("success", [])
                              if "candidate_id" in p})
                if ids:
                    result[cluster] = ids
            return result
        except Exception:  # noqa: BLE001
            return {}


__all__ = ["LearningMemory"]

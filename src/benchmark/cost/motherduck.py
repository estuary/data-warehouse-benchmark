"""MotherDuck cost telemetry — Duckling warm-time billing (ESTIMATED).

WHAT MOTHERDUCK ACTUALLY BILLS (motherduck.com/docs + vendor confirmation)
---------------------------------------------------------------------------
A "Duckling" is a compute instance, billed for the wall-clock time it is in a
*warm* state — i.e. while serving queries PLUS a trailing *cooldown* that keeps
it warm for follow-up queries. Metering is per-second.

  Tier      Billing basis                  Default cooldown   $/hour (US)
  ----      -------------                   ----------------   -----------
  Pulse     per Compute Unit (per query)    N/A                 0.60
  Standard  per-second wall-clock           1 min               2.40
  Jumbo     per-second wall-clock           1 min               4.80
  Mega      per-second wall-clock           5 min              12.00
  Giga      per-second wall-clock          10 min              24.00  (36.00 until Sep 2026)

Cooldown is configurable (1 min – 24 h); the above are defaults. Per-second
rate = $/hour ÷ 3600 (Standard = $2.40/3600 ≈ $0.04/min).

KEY CONSEQUENCE: only **Pulse** is genuinely per-query. For every other tier the
unit of cost is the Duckling's *continuous uptime window* — a single warm window
serves many queries, so cost is NOT the sum of individual query times.

COST MODEL: three run-level views
---------------------------------
  warm  cost = sum(query_time)             × $/min   (best case; no floor)
  cold  cost = sum(max(query_time, 1 min)) × $/min   (each query its own minute)
  suite cost = (end − start)               × $/min   (actual warm-window span)
  Per-query cost (for interpretability) = query_time × rate (a single warm unit),
  query_time being engine time once ``reconcile`` back-fills it from
  ``MD_QUERY_HISTORY()`` (it re-prices the rows then; wall clock until). Under
  concurrency that bills the Duckling once per query in flight, so the
  per-second tiers' concurrency and sustained rows are re-priced as windows of
  Duckling time, counted while a query ran and shared by engine time
  (``analysis.recost.recost_capacity_windows``, run when such a pass ends); Pulse
  keeps the per-query price.

  query_time = per-query duration; (end − start) = first-query start → last-query
  end, using the authoritative ``start_time``/``end_time`` back-filled into each
  row's metadata by ``reconcile`` from ``MD_QUERY_HISTORY()``. $/min = tier
  $/hour ÷ 60 (Standard $2.40/hr = $0.04/min). Pulse bills per-query (per CU); the
  warm/cold/suite split applies to the per-second tiers (Standard/Jumbo/Mega/Giga).

WHERE IT'S COMPUTED: per-query cost is inline (this collector, ``cost_from_result``).
The HOT/COLD suite aggregates are DERIVED at analysis time from the
per-query start/end timestamps via ``suite_cost`` — no engine round-trip — so
``reconcile`` is a no-op for MotherDuck **cost** (all tiers). It does, however,
back-fill MotherDuck **server time** (and those start/end timestamps) from the
durable ``MD_QUERY_HISTORY()``. See ``analysis/reconcile.py`` and
``MotherDuckConnector.lookup_server_times``.
"""

from __future__ import annotations

from typing import ClassVar

from ..connectors.base import QueryResult
from .base import CostCollector, CostConfidence

MIN_BILLED_SECONDS = (
    60.0  # 1-minute per-query minimum — used ONLY by cold_cost(); warm/suite have no floor
)


class MotherDuckCostCollector(CostCollector):
    TIER_USD_PER_HOUR: ClassVar[dict[str, float]] = {
        "pulse": 0.60,
        "standard": 2.40,
        "jumbo": 4.80,
        "mega": 12.00,
        "giga": 24.00,  # published rate (cut from $36.00/hr in Sep 2026); stored Giga costs use it
    }

    confidence = CostConfidence.ESTIMATED

    def __init__(self, tier: str = "standard") -> None:
        tier = tier.lower()
        if tier not in self.TIER_USD_PER_HOUR:
            raise ValueError(f"unknown MotherDuck tier '{tier}'")
        self.tier = tier
        self.rate_usd_per_second = self.TIER_USD_PER_HOUR[tier] / 3600.0

    def estimate_cost(self, wall_clock_ms: float) -> float:
        """Per-query cost: wall-clock seconds × rate. NO floor — the 1-minute
        minimum is a per-run cooldown, applied once in :meth:`suite_cost`, not
        per query."""
        return (wall_clock_ms / 1000.0) * self.rate_usd_per_second

    def cost_from_result(self, result: QueryResult) -> float:
        if result.error_message is not None:
            return 0.0
        return self.estimate_cost(result.wall_clock_ms)

    # ── run-level views, computed at analysis time per run_type ──────────────
    #   warm  = sum(query_time)             × rate    (best case, no floor)
    #   cold  = sum(max(query_time, 1 min)) × rate    (each query its own minute)
    #   suite = (end − start)               × rate    (the actual warm window)

    def warm_cost(self, query_seconds: list[float]) -> float:
        """Σ query_time × rate — no floor."""
        return sum(query_seconds) * self.rate_usd_per_second

    def cold_cost(self, query_seconds: list[float]) -> float:
        """Σ max(query_time, 1 min) × rate — per-query 1-minute minimum."""
        return sum(max(s, MIN_BILLED_SECONDS) for s in query_seconds) * self.rate_usd_per_second

    def suite_cost(self, span_seconds: float) -> float:
        """(end − start) × rate — the run's warm-window span, no floor."""
        return span_seconds * self.rate_usd_per_second

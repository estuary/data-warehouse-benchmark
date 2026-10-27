"""Databricks cost telemetry — DBU-rate estimate; reconciled post-run.

Pricing (serverless SQL warehouse — the benchmark config; confirm your
plan/region):
  * Serverless SQL DBU: ~$0.70 / DBU-hour list, ALL-INCLUSIVE (compute runs in
    Databricks' account, so unlike Pro/Classic there is no separate cloud VM
    bill invisible to this accounting).
  * Override via ``dbu_price_usd`` in ``results/cost_config.json``'s
    databricks entry if your negotiated/regional rate differs (reconcile is
    authoritative either way).
  * DBU/hour by SQL warehouse size (Databricks SQL sizing):
      2X-Small 4 · X-Small 6 · Small 12 · Medium 24 · Large 40 ·
      X-Large 80 · 2X-Large 144 · 3X-Large 272 · 4X-Large 528

The inline path estimates ``wall_clock × (DBU/hour × $/DBU)`` and is recorded
ESTIMATED; ``benchmark reconcile`` replaces it with a true per-statement
allocation from ``system.query.history`` × ``system.billing.usage`` (which
prices from ``system.billing.list_prices``, so the $/DBU here only affects the
inline estimate). That allocation shares each billed hour by execution time, which
overcharges overlapped statements and ignores that sizes sharing an hour bill at
different rates, so ``reconcile`` then re-splits every hour across the windows that
ran in it by list rate x time, each window capped at list rate x the time a query
ran in it, so idle warehouse time (auto-stop, resizes) is not query cost
(``analysis.recost.resplit_databricks_hours``).
"""

from __future__ import annotations

from typing import ClassVar

from ..connectors.base import QueryResult
from .base import CostCollector, CostConfidence


class DatabricksCostCollector(CostCollector):
    """Cost telemetry for Databricks SQL warehouses."""

    # DBU/hour by SQL warehouse size (Databricks SQL sizing docs; 2X-Small and
    # Small confirmed against docs — verify the largest sizes per the pricing
    # calculator for your region).
    DBU_PER_HOUR: ClassVar[dict[str, int]] = {
        "2x-small": 4,
        "x-small": 6,
        "small": 12,
        "medium": 24,
        "large": 40,
        "x-large": 80,
        "2x-large": 144,
        "3x-large": 272,
        "4x-large": 528,
    }
    # Serverless SQL warehouse list price per DBU, all-inclusive (also the
    # report's $/hr axis).
    DEFAULT_DBU_PRICE_USD: ClassVar[float] = 0.70
    # Inline estimate -> recorded ESTIMATED; reconcile upgrades to PROPORTIONAL.
    confidence = CostConfidence.ESTIMATED
    RECONCILED_CONFIDENCE = CostConfidence.PROPORTIONAL

    def __init__(self, size: str = "medium", dbu_price_usd: float = DEFAULT_DBU_PRICE_USD) -> None:
        size = size.lower()
        if size not in self.DBU_PER_HOUR:
            raise ValueError(f"unknown Databricks SQL warehouse size '{size}'")
        self.size = size
        self.dbu_per_hour = self.DBU_PER_HOUR[size]
        self.dbu_price_usd = dbu_price_usd
        self.usd_per_second = (self.dbu_per_hour * self.dbu_price_usd) / 3600.0

    def estimate_cost(self, wall_clock_ms: float) -> float:
        return (wall_clock_ms / 1000.0) * self.usd_per_second

    def cost_from_result(self, result: QueryResult) -> float:
        if result.error_message is not None:
            return 0.0
        return self.estimate_cost(result.wall_clock_ms)

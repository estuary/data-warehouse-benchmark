"""Snowflake cost telemetry — list rate x engine time (ESTIMATED), like every engine.

Pricing (US on-demand list — confirm your region/contract):
  * Standard:          $2.00 / credit  (the default: the cheapest edition that runs
    the benchmark, since Gen2 standard warehouses run on it)
  * Enterprise:        $3.00 / credit
  * Business Critical: $4.00 / credit

A warehouse bills credits per second while resumed. The inline path returns 0.0;
when a pass ends, ``analysis.recost`` prices each single-user query at its size's
list rate (``report.compute_rates``) x its engine time, and a pass under load by
window, as it does ClickHouse and MotherDuck. ``benchmark reconcile`` reads
``QUERY_ATTRIBUTION_HISTORY`` (6-8h lag) and keeps each query's credits as
``metadata.attributed_credits``, a cross-check that does not set the cost:
attribution counts whole warehouse-seconds, 1-2 s more than engine time per query,
1.44x list x engine time on the single-user passes, where every other engine is
priced to the millisecond (checked against metering;
``analysis.recost.price_snowflake_on_engine_time``).
"""

from __future__ import annotations

from typing import ClassVar

from ..connectors.base import QueryResult
from .base import CostCollector, CostConfidence


class SnowflakeCostCollector(CostCollector):
    """Cost telemetry for Snowflake.

    ``cost_from_result`` returns 0.0 so the runner records a row immediately; the
    pass then prices it on engine time (``analysis.recost``), and ``benchmark
    reconcile`` records its attributed credits beside the cost. The tier stays
    ESTIMATED.
    """

    CREDIT_PRICE_USD: ClassVar[dict[str, float]] = {
        "standard": 2.00,
        "enterprise": 3.00,
        "business_critical": 4.00,
    }
    # Rate-priced: ESTIMATED inline and after reconcile, which records attribution only.
    confidence = CostConfidence.ESTIMATED
    RECONCILED_CONFIDENCE = CostConfidence.ESTIMATED

    def __init__(self, edition: str = "standard") -> None:
        edition = edition.lower()
        if edition not in self.CREDIT_PRICE_USD:
            raise ValueError(f"unknown Snowflake edition '{edition}'")
        self.edition = edition
        self.credit_price = self.CREDIT_PRICE_USD[edition]

    def estimate_cost(self, wall_clock_ms: float) -> float:
        """Wall-clock cannot infer credits — returns 0.0; back-filled by reconcile."""
        return 0.0

    def cost_from_result(self, result: QueryResult) -> float:
        # Per-query cost requires QUERY_ATTRIBUTION_HISTORY (6-8h lag).
        return 0.0

"""BigQuery cost telemetry — DIRECT on demand; PROPORTIONAL on a reservation.

Two pricing models (pick to match your setup):
  * ``on-demand``: $6.25 per TiB scanned. ``total_bytes_billed`` from the API
    already includes BigQuery's 10 MB-per-query minimum + rounding, so it is
    used as-is. (The monthly free 1 TiB allowance is NOT modeled — this is a
    per-query figure.)
  * ``enterprise`` (Editions / reservation): slot-based, $0.06 per slot-hour.
    Two sub-models:
      - **consumed** (default): priced from ``slot_millis`` (slot-time actually
        used) — the autoscale / pay-per-use economics. Recorded DIRECT.
      - **committed** (when ``reservation_slots`` is set, e.g. a fixed-capacity
        slot sweep): you pay for the *provisioned* baseline whether used or not,
        so cost = ``reservation_slots × $0.06/hr × active-time``. This is the
        apples-to-apples analog of Snowflake's per-second warehouse billing and
        the number the slot-sweep frontier needs. It's an allocation of a
        committed rate across each query's active (server) time, so it's recorded
        PROPORTIONAL, not DIRECT.

The connector populates ``QueryResult.metadata`` with ``total_bytes_billed`` and
``slot_millis``; this collector converts the relevant one to USD.

The benchmark's reservations are fixed baselines, which bill whether used or not,
so the consumed price is a placeholder: when a ``sql``, ``concurrency`` or
``sustained`` pass ends, ``analysis.recost.recost_bigquery_capacity`` re-prices its
reservation rows at baseline slots x the slot-hour rate x the time the queries held
the reservation (one window per statement, or per pass and level under load),
split within a window by slot-time share, PROPORTIONAL, keeping the consumed price
under ``metadata.slot_ms_cost_usd``.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from ..connectors.base import QueryResult
from .base import CostCollector, CostConfidence

logger = logging.getLogger("benchmark.cost.bigquery")

BYTES_PER_TIB = 1024**4


class BigQueryCostCollector(CostCollector):
    """DIRECT cost telemetry for BigQuery."""

    ON_DEMAND_USD_PER_TIB = 6.25
    ENTERPRISE_USD_PER_SLOT_HOUR = 0.06  # Enterprise edition pay-as-you-go (US)
    TIERS: ClassVar[tuple[str, ...]] = ("on-demand", "enterprise")

    def __init__(self, tier: str = "on-demand", reservation_slots: int | None = None) -> None:
        if tier not in self.TIERS:
            raise ValueError(f"unknown BigQuery tier '{tier}'")
        self.tier = tier
        # Fixed baseline slot count for the committed-cost model (None = consumed).
        self.reservation_slots = reservation_slots
        # Committed pricing is a time-weighted allocation of a provisioned rate →
        # PROPORTIONAL; everything else is a per-query billing metric → DIRECT.
        self.confidence = (
            CostConfidence.PROPORTIONAL
            if (tier == "enterprise" and reservation_slots)
            else CostConfidence.DIRECT
        )

    def estimate_cost(self, wall_clock_ms: float) -> float:
        """Fallback — BigQuery cost is bytes/slot-based, not wall-clock."""
        logger.warning(
            "BigQuery cost collector called via wall-clock path; "
            "no bytes/slot metadata, returning 0",
        )
        return 0.0

    def cost_from_result(self, result: QueryResult) -> float:
        if result.error_message is not None:
            return 0.0
        meta = result.metadata or {}
        if self.tier == "enterprise":
            if self.reservation_slots:
                # Committed: provisioned slots × rate × this query's active time.
                # Prefer server-side time; fall back to wall-clock if unavailable.
                active_ms = result.server_time_ms
                if active_ms is None:
                    active_ms = result.wall_clock_ms
                return (
                    self.reservation_slots
                    * self.ENTERPRISE_USD_PER_SLOT_HOUR
                    * (active_ms / 1000.0 / 3600.0)
                )
            slot_ms = int(meta.get("slot_millis", 0) or 0)
            return (slot_ms / 1000.0 / 3600.0) * self.ENTERPRISE_USD_PER_SLOT_HOUR
        bytes_billed = int(meta.get("total_bytes_billed", 0) or 0)
        return (bytes_billed / BYTES_PER_TIB) * self.ON_DEMAND_USD_PER_TIB

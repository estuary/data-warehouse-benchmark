"""ClickHouse Cloud cost telemetry — ESTIMATED (compute-unit-time × rate).

Pricing (ClickHouse Cloud Scale tier, the tier the benchmark's service ran on;
confirm the rate for your region/CSP):
  * $0.2985 per compute-unit-hour; 1 compute unit = 2 vCPU / 8 GiB RAM.
    Sources, checked 2026-09-23: the unit definition is on
    https://clickhouse.com/pricing ("1 compute unit = 8 GiB RAM and 2 vCPU");
    the Scale rate follows from the worked Scale examples on
    https://clickhouse.com/docs/cloud/manage/billing/overview (2 replicas x
    8 GiB = $436.95 a month and 3 replicas x 16 GiB = $1,310.84 a month, i.e.
    $218.47 per compute unit per month = $0.2985 x ~732 hours). Scale and
    Enterprise support single-replica services above 12 GiB
    (https://clickhouse.com/docs/products/cloud/features/cloud-tiers).
  * The Scale tier runs a minimum of 2 replicas, so a service's hourly cost is
    ``(vCPU / 2) × replicas × $0.2985``.

ClickHouse Cloud bills running compute continuously (not per query) and exposes
no SQL-callable per-query billing record, so the per-query figure is a rough
``wall_clock × service-rate`` estimate. ESTIMATED confidence; not reconciled.
Warm and cold rows are re-priced on engine time (``elapsed_ns``, not the client's
wall clock) when the pass ends (``analysis.recost.recost_rate_priced_rows``). Under
concurrency the estimate bills the service once per query in flight, so concurrency
and sustained rows are re-priced as windows of service time, counted while a query
ran and shared by engine time (``analysis.recost.recost_capacity_windows``).
"""

from __future__ import annotations

from ..connectors.base import QueryResult
from .base import CostCollector, CostConfidence

USD_PER_COMPUTE_UNIT_HOUR = 0.2985  # Scale tier; 1 unit = 2 vCPU / 8 GiB
VCPU_PER_COMPUTE_UNIT = 2


class ClickHouseCostCollector(CostCollector):
    """ESTIMATED cost telemetry for ClickHouse Cloud (Scale tier)."""

    confidence = CostConfidence.ESTIMATED

    def __init__(self, vcpus: int = 16, replicas: int = 2) -> None:
        if vcpus <= 0 or replicas <= 0:
            raise ValueError("vcpus and replicas must be positive")
        self.vcpus = vcpus
        self.replicas = replicas
        units = vcpus / VCPU_PER_COMPUTE_UNIT
        self.usd_per_hour = units * replicas * USD_PER_COMPUTE_UNIT_HOUR
        self.usd_per_second = self.usd_per_hour / 3600.0

    def estimate_cost(self, wall_clock_ms: float) -> float:
        return (wall_clock_ms / 1000.0) * self.usd_per_second

    def cost_from_result(self, result: QueryResult) -> float:
        if result.error_message is not None:
            return 0.0
        return self.estimate_cost(result.wall_clock_ms)

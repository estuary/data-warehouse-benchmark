"""Microsoft Fabric cost telemetry — capacity-rate estimate; reconciled post-run.

Pricing (Fabric F-series PAYG list, ~$0.18 per CU-hour — confirm your region):
  * F2 $0.36 · F4 $0.72 · F8 $1.44 · F16 $2.88 · F32 $5.76 · F64 $11.52 · F128 $23.04
  (billed per-second with a 1-minute minimum per **active capacity period**, not
  per query)

The inline path estimates ``wall_clock × capacity $/hour`` — an upper bound, since
the whole capacity is shared across concurrent queries (under a 100-session burst it
charges the capacity a hundred times over) — and is recorded ESTIMATED. ``benchmark
reconcile`` back-fills each statement's engine time and CPU time from
``queryinsights.exec_requests_history``, then
:func:`benchmark.analysis.recost.recost_fabric_windows` prices what the capacity
bills and splits it across the statements that shared it:

  * A **window** is one burst or sustained hold (one invocation's rows at one level,
    while at least one statement held the capacity for its engine time), or a single
    warm/cold statement, which has the capacity to itself for its engine time.
  * A window costs ``$0.18/CU-h × max(SKU CUs × its span, CU-seconds consumed)``:
    the SKU time it held plus any bursting past it, which Fabric smooths forward and
    bills as overage when the capacity pauses. CU-seconds consumed are the
    statements' ``allocated_cpu_time_ms`` × :data:`CU_SECONDS_PER_CPU_SECOND`.
  * The window's cost is split across its successful statements by CPU share, or by
    elapsed-time share where CPU time is missing (rows whose ``queryinsights``
    history is gone with their workspace: that split carries SKU time only, no
    bursting).

``component`` (always ``dw``) is captured in metadata for provenance only.

.. warning:: The 1-minute minimum is **not** applied per query. It bills a
   capacity period, and a benchmark pass runs hundreds of queries back-to-back
   against one running capacity, so a per-query floor would assert hundreds of
   separate billing periods and collapse every un-reconciled row onto a single
   flat value (``cost.redshift`` documents the same hazard and its cost on
   Redshift). ``cost.motherduck`` shows the correct shape — no floor per query,
   the minimum applied once at run level in ``cold_cost``.
"""

from __future__ import annotations

from typing import ClassVar

from ..connectors.base import QueryResult
from ..settings import get_settings
from .base import CostCollector, CostConfidence

# Capacity is billed per-second with a 1-minute minimum per *active period*, not
# per query. Kept as a named constant for run-level sizing; deliberately NOT
# applied per query — see the module docstring.
MIN_BILLED_SECONDS = 60.0

#: Capacity-unit seconds Fabric bills per second of a statement's allocated CPU time.
#: Calibrated 2026-09-27: the Capacity Metrics app reported 680,518 CU(s) of
#: warehouse queries for the UTC day against 714,116 CPU-s in queryinsights (0.95).
#: Kept at 1.0, which overstates a bursting window by at most that 5%.
CU_SECONDS_PER_CPU_SECOND = 1.0


class FabricCostCollector(CostCollector):
    """Cost telemetry for Microsoft Fabric capacity."""

    CAPACITY_USD_PER_HOUR: ClassVar[dict[str, float]] = {
        "f2": 0.360,
        "f4": 0.720,
        "f8": 1.440,
        "f16": 2.880,
        "f32": 5.760,
        "f64": 11.520,
        "f128": 23.040,
    }
    # Inline estimate -> recorded ESTIMATED; reconcile upgrades to PROPORTIONAL.
    confidence = CostConfidence.ESTIMATED
    RECONCILED_CONFIDENCE = CostConfidence.PROPORTIONAL

    def __init__(self, capacity: str | None = None) -> None:
        # Default to the provisioned SKU via FABRIC_CAPACITY_SKU (.env); a wrong
        # SKU scales every estimate (F64 vs F8 is 8x).
        capacity = (capacity or get_settings().FABRIC.CAPACITY_SKU).lower()
        if capacity not in self.CAPACITY_USD_PER_HOUR:
            raise ValueError(f"unknown Fabric capacity tier '{capacity}'")
        self.capacity = capacity
        self.usd_per_hour = self.CAPACITY_USD_PER_HOUR[capacity]
        self.usd_per_second = self.usd_per_hour / 3600.0

    def estimate_cost(self, wall_clock_ms: float) -> float:
        """Provisional per-query price: elapsed time x capacity rate.

        No per-query 1-minute floor — the minimum belongs to the capacity's
        active period, not to each query in a continuous pass (see the module
        docstring). ``reconcile`` supersedes this from ``queryinsights``.
        """
        return (wall_clock_ms / 1000.0) * self.usd_per_second

    def cost_from_result(self, result: QueryResult) -> float:
        if result.error_message is not None:
            return 0.0
        return self.estimate_cost(result.wall_clock_ms)

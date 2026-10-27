"""Redshift Serverless cost telemetry — RPU-rate estimate; reconciled post-run.

Pricing (on-demand list; confirm your region):
  * $0.375 per RPU-hour, billed per-second with a 60-second minimum per
    **compute session** — not per query.

The inline path estimates ``wall_clock × baseline_rpu × $/RPU-hr`` and is
recorded ESTIMATED; ``benchmark reconcile`` replaces it with actual RPU-seconds
from ``SYS_SERVERLESS_USAGE`` (superuser-only; 7-day retention), upgrading the
row to PROPORTIONAL. The workgroup bills every second it is active, including the
harness's time between a single-user pass's queries, so reconcile then caps each
query's (or burst's) billed share at the RPU rate x the time it held the workgroup,
split within a burst by execution time (``analysis.recost.cap_redshift_idle``; the
billed share stays in ``metadata.billed_share_usd``).

.. warning:: The 60-second minimum is **not** applied per query. It bills a
   compute session that wakes to serve work; a benchmark
   pass runs hundreds of queries back-to-back inside ONE such session, so
   charging each query its own 60-second floor asserts hundreds of separate
   wake-ups. A per-query floor makes every un-reconciled row collapse onto one of a handful
   of flat values (60s × RPU × rate — e.g. exactly $0.40 at 64 RPU, whether the
   query took 355 ms or 60 s) and inflates a sub-second query's price ~170x.
   The results DB holds 387 such rows ($75.60 of fabricated spend, 13.7% of all
   Redshift cost recorded) whose billing window closed before they could be
   reconciled; ``analysis.recost.clear_fabricated_redshift_estimates`` NULLs
   them. Without the floor this estimate *under*-states an idle-capacity bill,
   which is the safer direction for a placeholder that reconcile is expected to
   replace.
"""

from __future__ import annotations

from ..connectors.base import QueryResult
from .base import CostCollector, CostConfidence

# Redshift Serverless bills a 60-second minimum per *compute session*, not per
# query. Kept as a named constant because it is real and load-bearing when sizing
# a whole pass, but deliberately NOT applied per query — see the module docstring.
MIN_BILLED_SECONDS = 60.0


class RedshiftCostCollector(CostCollector):
    """Cost telemetry for Redshift Serverless."""

    DEFAULT_RPU_PRICE_USD = 0.375  # per RPU-hour

    # Inline estimate -> recorded ESTIMATED; reconcile upgrades to PROPORTIONAL.
    confidence = CostConfidence.ESTIMATED
    RECONCILED_CONFIDENCE = CostConfidence.PROPORTIONAL

    def __init__(
        self, baseline_rpu: int = 32, rpu_price_usd: float = DEFAULT_RPU_PRICE_USD
    ) -> None:
        self.baseline_rpu = baseline_rpu
        self.rpu_price_usd = rpu_price_usd
        # Inline estimate uses baseline_rpu; reconcile reads the actual RPUs the
        # workgroup scaled to.
        self.usd_per_second_baseline = (baseline_rpu * rpu_price_usd) / 3600.0

    def estimate_cost(self, wall_clock_ms: float) -> float:
        """Provisional per-query price: elapsed time x baseline RPU rate.

        No per-query 60-second floor — a benchmark pass runs its queries inside
        one continuous compute session, so the floor belongs to the session, not
        the query (see the module docstring). ``reconcile`` supersedes this with
        real RPU-seconds.
        """
        return (wall_clock_ms / 1000.0) * self.usd_per_second_baseline

    def cost_from_result(self, result: QueryResult) -> float:
        if result.error_message is not None:
            return 0.0
        return self.estimate_cost(result.wall_clock_ms)

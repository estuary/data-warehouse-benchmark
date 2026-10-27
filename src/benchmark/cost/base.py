"""Cost collector base class.

The runner asks ``cost_from_result()`` for each measured run, passing the
full ``QueryResult`` so engines with per-query billing metadata
(BigQuery, Snowflake) can use it. The default implementation routes to the
wall-clock-only ``estimate_cost()`` path used by engines without per-query
billing (MotherDuck, ClickHouse).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..connectors.base import QueryResult


class CostConfidence:
    DIRECT = "direct"
    PROPORTIONAL = "proportional"
    ESTIMATED = "estimated"


class CostCollector(ABC):
    """Abstract cost collector.

    Subclasses must implement ``estimate_cost``. Engines with per-query
    billing data should also override ``cost_from_result`` to use the richer
    signal (e.g., ``total_bytes_billed`` on BigQuery).
    """

    confidence: str = CostConfidence.ESTIMATED

    @abstractmethod
    def estimate_cost(self, wall_clock_ms: float) -> float:
        """USD cost for a query given its wall-clock duration only."""

    def cost_from_result(self, result: QueryResult) -> float:
        """USD cost for a query result.

        Default: route to ``estimate_cost(wall_clock_ms)``. Engines with
        per-query billing metadata override to read from ``result.metadata``.
        """
        return self.estimate_cost(result.wall_clock_ms)

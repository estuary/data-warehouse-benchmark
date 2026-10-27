"""Cold-run pass.

Cold run = no result cache, no buffer pool warmth, no compiled plan
re-use. Each engine has a different "cold" recipe:

  * snowflake:  ``ALTER SESSION SET USE_CACHED_RESULT = FALSE`` (already on),
                + suspend/resume the warehouse between queries.
  * databricks: stop+start the SQL warehouse between queries.
  * bigquery:   ``use_query_cache=False`` (already on); BQ has no warm pool.
  * redshift:   ``SET enable_result_cache_for_session = off`` + bounce the
                workgroup (or wait for idle scale-down).
  * fabric:     no public knob to flush.
  * clickhouse: ``SYSTEM DROP MARK CACHE; SYSTEM DROP UNCOMPRESSED CACHE``
                between queries.
  * motherduck: ``CALL pragma_database_size(); ATTACH ... DETACH ...`` to
                evict the cached database.

The cold pass runs each canonical query once (no warmup, no repeat) at
``cold_scale`` (default SF1000); recorded with ``run_type='cold'``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

from ..connectors.base import WarehouseConnector
from ..cost.base import CostCollector
from ..queries.registry import QueryRegistry
from ..results import ResultsDB
from .runner import capture_runtime_environment, execute_with_timeout, resolve_query_timeout_s

logger = logging.getLogger("benchmark.sql_benchmark.cold")


def _drop_engine_cache(platform: str, connector: WarehouseConnector) -> None:
    """Best-effort cache-flush per engine; logged + swallowed on failure."""
    if platform == "snowflake":
        # Suspend the warehouse ACTUALLY in use (resolved per scale factor:
        # SF10→SMALL, SF100→MEDIUM, …), not a hardcoded one — otherwise the
        # cold pass suspends an idle warehouse while the one being queried
        # stays warm, and "cold" runs aren't cold. AUTO_RESUME brings it back
        # cold on the next query.
        wh = getattr(connector, "warehouse", None)
        statements = [f"ALTER WAREHOUSE {wh} SUSPEND"] if wh else []
    else:
        statements = {
            "databricks": [],  # warehouse bounce handled outside this loop
            "bigquery": [],
            "redshift": ["SET enable_result_cache_for_session = off"],
            "fabric": [],
            "clickhouse": [
                "SYSTEM DROP MARK CACHE",
                "SYSTEM DROP UNCOMPRESSED CACHE",
            ],
            "motherduck": [],
        }.get(platform, [])
    for stmt in statements:
        result = connector.execute_query(stmt)
        if result.error_message:
            logger.warning(
                "cold-cache drop step failed",
                extra={"platform": platform, "stmt": stmt, "error": result.error_message},
            )


def run_cold_pass(
    platform: str,
    connector: WarehouseConnector,
    cost_collector: CostCollector,
    results_db: ResultsDB,
    scale_factor: int,
    *,
    blocks: Iterable[str] = ("tpcds", "dsb", "clickbench"),
    inter_query_sleep_s: float = 0.0,
    provenance_id: str | None = None,
    cost_config: dict | None = None,
) -> None:
    """Single-pass cold execution. Each query gets its own cache-flush."""
    # Use the platform's translated SQL (not the canonical DuckDB dialect),
    # matching the warm runner.
    queries = [q for q in QueryRegistry(dialect=platform).all() if q.block in blocks]
    logger.info(
        "cold-pass starting",
        extra={"platform": platform, "scale_factor": scale_factor, "total_queries": len(queries)},
    )

    with connector:
        compute_label = capture_runtime_environment(
            results_db, platform, scale_factor, "cold", connector
        )
        if cost_config is not None and compute_label:
            from ..cost.discovery import collector_for

            cost_collector = collector_for(platform, cost_config, compute_label=compute_label)
        for q in queries:
            _drop_engine_cache(platform, connector)
            if inter_query_sleep_s:
                time.sleep(inter_query_sleep_s)

            result = execute_with_timeout(
                connector,
                q.sql,
                resolve_query_timeout_s(scale_factor, q.id),
                platform=platform,
                query_id=q.id,
            )
            cost_usd = cost_collector.cost_from_result(result)
            results_db.record_sql_result(
                platform=platform,
                query_id=q.id,
                block=q.block,
                scale_factor=scale_factor,
                run_type="cold",
                run_number=1,
                wall_clock_ms=result.wall_clock_ms,
                server_time_ms=result.server_time_ms,
                bytes_scanned=result.bytes_scanned,
                bytes_spilled=result.bytes_spilled,
                compile_ms=result.compile_ms,
                queue_ms=result.queue_ms,
                cache_hit_pct=result.cache_hit_pct,
                platform_query_id=result.platform_query_id,
                provenance_id=provenance_id,
                compute_label=compute_label,
                row_count=result.row_count if result.error_message is None else None,
                cost_usd=cost_usd,
                cost_confidence=cost_collector.confidence,
                error_message=result.error_message,
                metadata=result.metadata,
            )
            logger.info(
                "cold-pass query",
                extra={
                    "platform": platform,
                    "query_id": q.id,
                    "block": q.block,
                    "wall_clock_ms": result.wall_clock_ms,
                    "cost_usd": cost_usd,
                },
            )

    logger.info(
        "cold-pass complete",
        extra={"platform": platform, "scale_factor": scale_factor},
    )

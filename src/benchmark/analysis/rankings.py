"""Composite cross-engine rankings.

Two sub-scores per engine, each an integer rank, then summed:
  * sql_perf — rank of the mean per-scale suite geomean of per-query median
    server time (decision-support blocks, each scale weighted equally)
  * sql_cost — rank of the mean per-scale average $/query

The composite ``composite_rank`` orders the engines; the parquet preserves
both sub-ranks so a reader can see the breakdown.

Reads the shared ``bench`` view (:mod:`benchmark.report.views`): designated
tier, admission policy, latest-wins — the report's own methodology, so the
ranking cannot contradict the report it ships beside. (A tier-unaware,
row-pooled average would rank an engine measured mostly at 1 TB below one
measured mostly at 1 GB.)
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

logger = logging.getLogger("benchmark.analysis.rankings")


def emit_rankings(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/rankings.parquet"),
) -> Path:
    from ..report.views import install_views

    output_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        install_views(con)
        con.execute(
            f"""
            COPY (
                WITH per_q AS (
                    SELECT platform, scale_factor, query_id,
                           MEDIAN(server_time_ms) AS ms,
                           AVG(cost_usd) AS cost
                    FROM bench
                    WHERE run_type = 'warm' AND error_message IS NULL
                      AND server_time_ms > 0 AND block IN ('tpcds', 'dsb')
                    GROUP BY 1, 2, 3
                ),
                per_scale AS (
                    SELECT platform, scale_factor,
                           EXP(AVG(LN(ms))) AS geomean_ms,
                           AVG(cost) AS avg_cost
                    FROM per_q GROUP BY 1, 2
                ),
                per_engine AS (
                    SELECT platform,
                           AVG(geomean_ms) AS score_ms,
                           AVG(avg_cost) AS score_cost,
                           COUNT(*) AS scales_measured
                    FROM per_scale GROUP BY 1
                ),
                ranked AS (
                    SELECT
                        platform, score_ms, score_cost, scales_measured,
                        ROW_NUMBER() OVER (ORDER BY score_ms ASC) AS rank_perf,
                        ROW_NUMBER() OVER (ORDER BY score_cost ASC) AS rank_cost
                    FROM per_engine
                )
                SELECT
                    platform,
                    rank_perf, rank_cost,
                    (rank_perf + rank_cost) AS composite_rank,
                    score_ms, score_cost, scales_measured
                FROM ranked
                ORDER BY composite_rank, platform
            ) TO '{output_path.as_posix()}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()
    logger.info("rankings emitted to %s", output_path)
    return output_path

"""TPC-DS vs DSB skew-impact analysis.

For each query number that exists in both TPC-DS and DSB (q01–q99), compare
server-side latency between the two blocks on the same platform. The pairs run
identical SQL (the TPC-DS text, constants included) over DSB's skewed data, so
the delta is how an engine's runtime responds to the distribution change, not
how it handles queries aimed at hot keys: the TPC-DS constants were chosen for
uniform data, and on DSB's data they select less (5 of the 74 clean pairs at
100 GB return no rows on DSB) and read fewer bytes. DSB's own parameter
generator, which targets the skewed values, is not used.

Reads the shared ``bench`` view (:mod:`benchmark.report.views`) so the export
matches the population behind the report's skew section: designated tier,
admission policy, latest-wins, successful runs only.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

logger = logging.getLogger("benchmark.analysis.skew")


_SKEW_SQL = """
WITH deduped AS (
    -- The report's comparison population: designated tier, admission policy,
    -- latest-wins (see benchmark.report.views). Errored rows carry no latency
    -- and are excluded from the pairing.
    SELECT * FROM bench
    WHERE run_type = 'warm' AND error_message IS NULL
),
tpcds_rows AS (SELECT * FROM deduped WHERE block = 'tpcds'),
dsb_rows   AS (SELECT * FROM deduped WHERE block = 'dsb'),
tpcds AS (
    SELECT platform, CAST(REPLACE(query_id, 'tpcds_q', '') AS INTEGER) AS qnum,
           scale_factor,
           MEDIAN(server_time_ms) AS median_server_ms,
           MEDIAN(wall_clock_ms) AS median_wall_ms,
           AVG(cost_usd) AS mean_cost
    FROM tpcds_rows
    GROUP BY platform, qnum, scale_factor
),
dsb AS (
    SELECT platform, CAST(REPLACE(query_id, 'dsb_q', '') AS INTEGER) AS qnum,
           scale_factor,
           MEDIAN(server_time_ms) AS median_server_ms,
           MEDIAN(wall_clock_ms) AS median_wall_ms,
           AVG(cost_usd) AS mean_cost
    FROM dsb_rows
    GROUP BY platform, qnum, scale_factor
)
SELECT
    t.platform,
    t.qnum,
    t.scale_factor,
    t.median_server_ms AS tpcds_server_ms,
    d.median_server_ms AS dsb_server_ms,
    d.median_server_ms - t.median_server_ms AS delta_server_ms,
    CASE WHEN t.median_server_ms > 0
         THEN (d.median_server_ms - t.median_server_ms) / t.median_server_ms * 100
         ELSE NULL END AS delta_pct,
    t.median_wall_ms AS tpcds_wall_ms,
    d.median_wall_ms AS dsb_wall_ms,
    t.mean_cost AS tpcds_cost,
    d.mean_cost AS dsb_cost
FROM tpcds t
JOIN dsb d USING (platform, qnum, scale_factor)
WHERE t.qnum <= 99
ORDER BY t.platform, t.qnum, t.scale_factor
"""


def emit_skew_impact(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/skew_impact.parquet"),
) -> Path:
    from ..report.views import install_views

    output_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        install_views(con)
        con.execute(f"COPY ({_SKEW_SQL}) TO '{output_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    logger.info("skew-impact emitted to %s", output_path)
    return output_path

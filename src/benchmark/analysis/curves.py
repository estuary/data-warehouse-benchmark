"""Cost-scaling curves.

For each (platform, query_id) compute median latency + mean cost at each scale
factor that's been measured, and emit a Parquet that a reader can turn into
per-engine line charts.

Reads the shared ``bench`` view (:mod:`benchmark.report.views`): designated
tier, comparison-admission policy, latest-wins — the same population the
report's own tables aggregate, so a reader recomputing a curve from this
parquet reproduces the published numbers.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

logger = logging.getLogger("benchmark.analysis.curves")


_CURVE_SQL = """
SELECT
    platform, query_id, block, scale_factor,
    approx_quantile(server_time_ms, 0.5)
        FILTER (WHERE error_message IS NULL) AS median_server_ms,
    approx_quantile(wall_clock_ms, 0.5)
        FILTER (WHERE error_message IS NULL) AS median_ms,
    approx_quantile(wall_clock_ms, 0.95)
        FILTER (WHERE error_message IS NULL) AS p95_ms,
    AVG(cost_usd) FILTER (WHERE error_message IS NULL) AS mean_cost_usd,
    SUM(CASE WHEN error_message IS NOT NULL THEN 1 ELSE 0 END) AS failures
FROM bench
-- decision-support only: a per-scale curve needs scale_factor to be a data
-- volume, and ClickBench's is a campaign tag over one fixed ~100 GB dataset.
WHERE run_type = 'warm' AND block IN ('tpcds', 'dsb')
GROUP BY platform, query_id, block, scale_factor
"""


def emit_cost_curves(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/cost_curves.parquet"),
) -> Path:
    from ..report.views import install_views

    output_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        install_views(con)
        con.execute(f"COPY ({_CURVE_SQL}) TO '{output_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    logger.info("cost-curves emitted to %s", output_path)
    return output_path

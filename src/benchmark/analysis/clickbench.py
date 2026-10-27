"""ClickBench per-query summary.

ClickBench differs from the decision-support suites in a way that shapes this
export: it runs against **one fixed ~100 GB `hits` dataset**, so its
``scale_factor`` is a campaign tag rather than a data volume. What actually
varies across those tags is the **compute tier** the pass ran on — Snowflake
X-Small/Small/Medium, MotherDuck Standard/Jumbo, ClickHouse 16 GB/32 GB,
BigQuery on-demand vs a 200-slot reservation. That is why the other exports
(:mod:`benchmark.analysis.curves`, :mod:`benchmark.analysis.patterns`) exclude
the block outright: a per-scale *curve* over it would plot a compute ladder on
an axis labelled data volume.

This export keeps the tag but names it for what it is. Each row carries the
``compute_label`` that produced it, and ``is_entry_tier`` flags each engine's
entry tier: its lowest campaign tag in the designated-tier population below,
the one tier every engine has, and therefore the only slice on which all seven
compare without silently averaging across warehouse sizes. It is not always the
smallest size measured: the size sweeps in ``size_ladder.parquet`` ran smaller
sizes for BigQuery, ClickHouse, Databricks and MotherDuck. Consumers wanting a cross-engine comparison filter to that flag;
consumers wanting the ladder read the tiers directly.

Reads the shared ``bench`` view (:mod:`benchmark.report.views`) — designated
tier, admission policy, latest-wins — so the population matches the report's.
Latency medians are over successful runs only.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

logger = logging.getLogger("benchmark.analysis.clickbench")


_CLICKBENCH_SQL = """
WITH per_query AS (
    SELECT
        platform,
        query_id,
        -- Named for what it is: a compute-tier tag, not a data volume.
        scale_factor AS compute_tag,
        any_value(compute_label) AS compute_label,
        approx_quantile(server_time_ms, 0.5)
            FILTER (WHERE error_message IS NULL) AS median_server_ms,
        approx_quantile(wall_clock_ms, 0.5)
            FILTER (WHERE error_message IS NULL) AS median_ms,
        AVG(cost_usd) FILTER (WHERE error_message IS NULL) AS mean_cost_usd,
        SUM(CASE WHEN error_message IS NOT NULL THEN 1 ELSE 0 END) AS failures,
        COUNT(*) AS runs
    FROM bench
    WHERE run_type = 'warm' AND block = 'clickbench'
    GROUP BY platform, query_id, scale_factor
),
entry AS (
    SELECT platform, min(compute_tag) AS entry_tag
    FROM per_query GROUP BY platform
)
SELECT
    p.*,
    (p.compute_tag = e.entry_tag) AS is_entry_tier
FROM per_query p
JOIN entry e USING (platform)
"""


def emit_clickbench_summary(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/clickbench_summary.parquet"),
) -> Path:
    """Emit the per-query ClickBench summary.

    Args:
        db_path: Results database to read.
        output_path: Parquet file to write.

    Returns:
        The path written.
    """
    from ..report.views import install_views

    output_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        install_views(con)
        con.execute(f"COPY ({_CLICKBENCH_SQL}) TO '{output_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    logger.info("clickbench summary emitted to %s", output_path)
    return output_path

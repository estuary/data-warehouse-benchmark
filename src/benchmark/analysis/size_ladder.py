"""Warm-pass results per compute size.

:mod:`benchmark.analysis.curves` keeps each engine at its **designated** compute
size per scale, which is the right population for a cross-engine comparison and
the wrong one for the sizing question: "what does the next size up buy at this
data volume?" The warm pass ran several engines across their whole size ladder
at a scale (Snowflake X-Small to X-Large, Redshift 8 to 128 RPU, and so on), and
this export keeps one row per size so a reader can rebuild the report's ladder
tables.

The grain is one row per (platform, block, scale, compute size, query): the
query's median server-side time and mean cost over its successful measured
runs, with the run and failure counts beside them. The report's ladder verifiers
(:func:`benchmark.report.verifiers._ladder_common_complete`) aggregate exactly
these per-query values, over the queries **every** size at that (platform,
scale) completed. A consumer must apply the same common-complete restriction
before summing: a per-size total over each size's own completed set understates
the fast sizes, because the queries the small sizes dropped are the expensive
ones. The explorer does this in SQL.

Reads ``results_latest`` (latest-wins per size, not canonical-filtered) so a
sizing sweep keeps one row per size. ClickHouse multi-replica warm rows are
excluded by the same comparison-admission predicate the report's size tables
use: replicas add throughput, not single-query speed, and the warm ladder is a
single-session measurement.

ClickBench rows are included with ``scale_factor`` NULL. Its dataset is one fixed
~100 GB table, so its scale factor is a campaign tag, not a data volume; a size
measured under two tags is pooled into one row, the same way the report's size
tables pool it (``generator._scale_expr``). :mod:`benchmark.analysis.clickbench`
holds only the tiers the ``bench`` population admits; this is the whole ladder.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

logger = logging.getLogger("benchmark.analysis.size_ladder")


def _size_ladder_sql() -> str:
    """The per-query, per-size warm aggregate, with the replica predicate inlined."""
    from ..report.policy import single_session_replica_predicate

    return f"""
    SELECT
        r.platform, r.block,
        CASE WHEN r.block = 'clickbench' THEN NULL ELSE r.scale_factor END AS scale_factor,
        r.compute_label, r.query_id,
        median(r.server_time_ms)
            FILTER (WHERE r.error_message IS NULL AND r.server_time_ms > 0) AS median_server_ms,
        AVG(r.cost_usd)
            FILTER (WHERE r.error_message IS NULL AND r.server_time_ms > 0) AS mean_cost_usd,
        COUNT(*) AS runs,
        SUM(CASE WHEN r.error_message IS NOT NULL THEN 1 ELSE 0 END) AS failures,
        -- When the size ran, so a reader can see a size measured in a separate
        -- session from the rest of its ladder (the explorer annotates one).
        MIN(r.created_at) AS first_run_at,
        MAX(r.created_at) AS last_run_at
    FROM results_latest r
    WHERE r.run_type = 'warm'
      AND r.compute_label IS NOT NULL
      AND {single_session_replica_predicate("r")}
    GROUP BY 1, 2, 3, 4, 5
    """


def emit_size_ladder(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/size_ladder.parquet"),
) -> Path:
    """Emit the warm pass at every compute size it ran on.

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
        con.execute(f"COPY ({_size_ladder_sql()}) TO '{output_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    logger.info("size ladder emitted to %s", output_path)
    return output_path

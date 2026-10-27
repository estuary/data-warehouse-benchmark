"""Pattern-sliced summary tables.

Each canonical query is tagged by ``benchmark.queries.registry`` (window,
aggregation, complex_join, set_op, ...). This module joins those tags with
the warm-run latency + cost data and emits per-pattern aggregates so the
report can answer "where does engine X shine vs struggle?".

Reads the shared ``bench`` view (:mod:`benchmark.report.views`) — designated
tier, admission policy, latest-wins — so the export reproduces the population
behind the report's own pattern section. Latency medians are over successful
runs only.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from ..queries.registry import QueryRegistry

logger = logging.getLogger("benchmark.analysis.patterns")


def emit_pattern_summary(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/pattern_summary.parquet"),
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    registry = QueryRegistry()
    # Build a temp table of (query_id, pattern) pairs.
    tag_rows: list[tuple[str, str]] = []
    for q in registry.all():
        for tag in q.patterns:
            tag_rows.append((q.id, tag))

    from ..report.views import install_views

    con = duckdb.connect(str(db_path))
    try:
        install_views(con)
        con.execute(
            "CREATE OR REPLACE TEMP TABLE query_patterns (query_id VARCHAR, pattern VARCHAR)"
        )
        con.executemany("INSERT INTO query_patterns VALUES (?, ?)", tag_rows)
        con.execute(
            f"""
            COPY (
                SELECT
                    r.platform, p.pattern, r.scale_factor,
                    approx_quantile(r.server_time_ms, 0.5)
                        FILTER (WHERE r.error_message IS NULL) AS median_server_ms,
                    approx_quantile(r.wall_clock_ms, 0.5)
                        FILTER (WHERE r.error_message IS NULL) AS median_ms,
                    AVG(r.cost_usd) FILTER (WHERE r.error_message IS NULL) AS mean_cost_usd,
                    COUNT(*) AS runs
                FROM bench r
                JOIN query_patterns p USING (query_id)
                -- decision-support only: ClickBench queries carry pattern tags
                -- too, but their scale_factor is a campaign tag, not a volume.
                WHERE r.run_type = 'warm' AND r.block IN ('tpcds', 'dsb')
                GROUP BY r.platform, p.pattern, r.scale_factor
            ) TO '{output_path.as_posix()}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()
    logger.info("pattern summary emitted to %s", output_path)
    return output_path

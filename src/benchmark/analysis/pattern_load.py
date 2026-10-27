"""Per-pattern behaviour under concurrency, across scales and compute sizes.

:mod:`benchmark.analysis.patterns` answers "where does engine X shine vs
struggle?" from the warm pass — one compute size per engine, one session. The
concurrency ladder ran the same queries across every warehouse size and six
session counts, so the same question can be asked with two more axes: does an
engine's pattern profile *hold* as data grows and as sessions pile up, or does a
particular pattern fall apart first?

Two latencies are emitted per cell so the consumer can normalise:

``pattern_ms``
    Median server time for queries carrying that pattern tag.
``cell_ms``
    Median server time for *all* queries in the same
    (platform, scale, compute size, level) cell — the engine's typical query
    there.

Their ratio is the useful quantity. It is scoped **within** an engine-and-size,
so it divides out both the engine's raw speed and the size of the warehouse:
below 1 means the pattern is easier than that engine's average query, above 1
means harder. That makes the ratio safe to aggregate across compute sizes, which
a raw latency is not.

Queries carry more than one pattern tag, so a query contributes to every pattern
it matches and the tags do not partition the suite.

Reads ``results_latest`` rather than ``bench``: the concurrency ladder sweeps
compute sizes deliberately, so the designated-tier restriction ``bench`` applies
to warm comparisons must not apply here (see ``report/policy.py``).
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from ..queries.registry import QueryRegistry

logger = logging.getLogger("benchmark.analysis.pattern_load")

#: Minimum successful queries in a (cell, pattern) group before a median is meaningful.
MIN_ROWS_PER_GROUP = 20


_PATTERN_LOAD_SQL = f"""
WITH ok AS (
    SELECT platform, scale_factor, compute_label, concurrency, query_id, server_time_ms
    FROM results_latest
    WHERE run_type = 'concurrency'
      AND block IN ('tpcds', 'dsb')
      AND error_message IS NULL
      AND server_time_ms IS NOT NULL
      AND compute_label IS NOT NULL
      AND concurrency IS NOT NULL
),
cell AS (
    -- The engine's typical query in this cell: the normalisation denominator.
    SELECT platform, scale_factor, compute_label, concurrency,
           quantile_cont(server_time_ms, 0.5) AS cell_ms,
           count(*) AS cell_n
    FROM ok
    GROUP BY 1, 2, 3, 4
),
pat AS (
    SELECT o.platform, o.scale_factor, o.compute_label, o.concurrency, p.pattern,
           quantile_cont(o.server_time_ms, 0.5) AS pattern_ms,
           count(*) AS n
    FROM ok o
    JOIN query_patterns p USING (query_id)
    GROUP BY 1, 2, 3, 4, 5
    HAVING count(*) >= {MIN_ROWS_PER_GROUP}
)
SELECT pat.*, cell.cell_ms, cell.cell_n
FROM pat
JOIN cell USING (platform, scale_factor, compute_label, concurrency)
WHERE cell.cell_ms > 0
"""


def emit_pattern_load(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/pattern_load.parquet"),
) -> Path:
    """Emit per-pattern latency under concurrency, with its normalisation base.

    Args:
        db_path: Results database to read.
        output_path: Parquet file to write.

    Returns:
        The path written.
    """
    from ..report.views import install_views

    output_path.parent.mkdir(parents=True, exist_ok=True)

    registry = QueryRegistry()
    tag_rows = [(q.id, tag) for q in registry.all() for tag in q.patterns]

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        install_views(con)
        con.execute(
            "CREATE OR REPLACE TEMP TABLE query_patterns (query_id VARCHAR, pattern VARCHAR)"
        )
        con.executemany("INSERT INTO query_patterns VALUES (?, ?)", tag_rows)
        con.execute(f"COPY ({_PATTERN_LOAD_SQL}) TO '{output_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    logger.info("pattern-load summary emitted to %s", output_path)
    return output_path

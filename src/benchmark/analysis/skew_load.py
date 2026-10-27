"""TPC-DS vs DSB skew impact under concurrency, across scales and compute sizes.

:mod:`benchmark.analysis.skew` answers "what does data skew cost?" from the warm
pass: one compute size per engine, one session. The concurrency ladder ran both
suites at every warehouse size and six session counts, so the same question can
be asked with two more axes: does an engine's skew penalty hold under load, or
does the skewed variant fall over first?

The pairing is the same as the warm export. DSB reuses the TPC-DS schema with
skewed distributions and numbers its queries to match, so ``dsb_qNN`` against
``tpcds_qNN`` isolates the optimizer's handling of non-uniform cardinality on an
otherwise identical query. DSB's three extra queries (numbered above 99) have no
twin and are dropped.

One row is emitted per (platform, scale, compute size, level) cell:

``skew_ratio``
    Median over paired query numbers of ``dsb_ms / tpcds_ms``, each side being
    that query's median server time in the cell. Above 1 means the skewed
    variant is slower. The ratio is formed per query and per cell, so it
    divides out raw engine speed, warehouse size and the level's own queueing;
    that is what makes it comparable across cells and safe to pool.
``tpcds_ms`` / ``dsb_ms``
    Cell medians over the paired queries, for context in a tooltip.
``pairs``
    Query numbers with a successful measurement on both sides in the cell.

Errored and timed-out executions carry no latency and are excluded, so a cell
where the skewed variant failed outright shows the survivors' ratio; ``pairs``
says how many survived.

Reads ``results_latest`` rather than ``bench``: the concurrency ladder sweeps
compute sizes deliberately, so the designated-tier restriction ``bench`` applies
to warm comparisons must not apply here (see ``report/policy.py``).
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

logger = logging.getLogger("benchmark.analysis.skew_load")

#: Minimum paired query numbers in a cell before its median ratio is meaningful.
MIN_PAIRS_PER_CELL = 20

#: Highest query number shared by both suites; DSB's extras above it have no twin.
MAX_PAIRED_QNUM = 99


_SKEW_LOAD_SQL = f"""
WITH ok AS (
    SELECT platform, scale_factor, compute_label, concurrency, block,
           CAST(regexp_extract(query_id, '(\\d+)$', 1) AS INTEGER) AS qnum,
           server_time_ms
    FROM results_latest
    WHERE run_type = 'concurrency'
      AND block IN ('tpcds', 'dsb')
      AND error_message IS NULL
      AND server_time_ms IS NOT NULL
      AND server_time_ms > 0
      AND compute_label IS NOT NULL
      AND concurrency IS NOT NULL
),
per_query AS (
    -- Each query's median in the cell, per suite.
    SELECT platform, scale_factor, compute_label, concurrency, qnum,
           quantile_cont(server_time_ms, 0.5) FILTER (WHERE block = 'tpcds') AS tpcds_q_ms,
           quantile_cont(server_time_ms, 0.5) FILTER (WHERE block = 'dsb')   AS dsb_q_ms
    FROM ok
    WHERE qnum <= {MAX_PAIRED_QNUM}
    GROUP BY 1, 2, 3, 4, 5
),
paired AS (
    SELECT * FROM per_query WHERE tpcds_q_ms IS NOT NULL AND dsb_q_ms IS NOT NULL
)
SELECT platform, scale_factor, compute_label, concurrency,
       quantile_cont(dsb_q_ms / tpcds_q_ms, 0.5) AS skew_ratio,
       quantile_cont(tpcds_q_ms, 0.5)            AS tpcds_ms,
       quantile_cont(dsb_q_ms, 0.5)              AS dsb_ms,
       count(*)                                  AS pairs
FROM paired
GROUP BY 1, 2, 3, 4
HAVING count(*) >= {MIN_PAIRS_PER_CELL}
ORDER BY 1, 2, 3, 4
"""


def emit_skew_load(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/skew_load.parquet"),
) -> Path:
    """Emit the per-cell DSB/TPC-DS skew ratio under concurrency.

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
        con.execute(f"COPY ({_SKEW_LOAD_SQL}) TO '{output_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    logger.info("skew-under-load summary emitted to %s", output_path)
    return output_path

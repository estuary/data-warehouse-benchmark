"""Concurrency grid export: one row per (workload, engine, scale, compute size, level).

This is the per-level data behind :mod:`benchmark.analysis.concurrency_shape`,
shaped so an external consumer can recompute the **useful-concurrency** metric
at any SLA rather than only at the one this repo happens to render.

The split matters. Of the shape metrics, only `useful_concurrency` (and its
`censored` companion) depend on the latency target; `k`, the failure mode, the
shed rate at the top level and the solo latency do not. So the SLA-independent
per-cell values are joined onto every level row as constants, and the
SLA-dependent ones are left to the consumer:

    useful concurrency at S =
        max(concurrency) FILTER (WHERE p50_ms <= S AND shed_pct < SLA_MAX_SHED_PCT)

which reproduces :func:`benchmark.analysis.concurrency_shape.shapes` exactly.

**Two workloads, one grid.** ``workload`` is ``decision_support`` (TPC-DS and DSB
pooled, one cell per platform, scale factor and compute size) or ``clickbench``.
ClickBench runs one fixed ~100 GB table, so its ``scale_factor`` is a campaign
tag rather than a data volume: its cells pool the tags and carry
``scale_factor`` NULL, one cell per (platform, compute size), matching how the
report's ``bench_cb`` consumers pool it. Its 43-query suite also needs its own
minimum rows per level (one full pass, less a few rows, against the
decision-support floor of 50 out of 201), and it is restricted to the protocol
levels through 50 (:data:`CLICKBENCH_LEVELS`): a level 40 that some ClickBench
rows carry has no counterpart on the ladder the frontier views plot, and 50 was
run at every size so nothing is lost. Both settings live in
:data:`CLICKBENCH` so the reducer and the export cannot disagree; the
integration tests hold the SQL recomputation to the reducer per workload.

**Latency is the engine's time on a query, queue included, network excluded**
(``engine_ms`` in the ``concurrency_results`` view; see
:func:`benchmark.analysis.concurrency_shape.engine_ms_sql`). ``p50_ms`` is its
median over the level's successful queries. Three engines report execution only
and need their queue time captured (``queue_ms``); a level where under
``MIN_ENGINE_TIME_PCT`` of successful rows carry it has ``p50_ms`` NULL, and a cell
with any such level has every ``p50_ms`` NULL, ``publishable`` false and
``withheld_reason`` set, so its latency is withheld rather than published without
the engine's wait. ``server_p50_ms`` is the engine-reported figure as it stands
(the views' ``execution_ms``: execution only on those three), kept for comparison;
``engine_time_pct`` is the share of successful rows carrying engine time.

**Failures.** ``shed_pct`` is the share of the level's queries that did not
complete. ``timeout_pct`` (our 10-minute timeout, or our client giving up),
``connection_pct`` (the connection dropped under a running query) and
``engine_error_pct`` (the engine returned an error) split it by cause. Rows the
harness itself failed (a query sent into a session it had already lost) are not
counted anywhere, ``runs`` included.

`usd_per_hour` is the list-price capacity rate for the compute size
(:mod:`benchmark.report.compute_rates`), giving the frontier a cost axis that is
comparable across engines. It is NULL where a label cannot be priced per hour,
chiefly BigQuery on-demand, which is scan-priced.

Reads ``results_latest`` (latest-wins) rather than ``bench``: the concurrency
ladder deliberately sweeps compute sizes, so the designated-tier restriction
that ``bench`` applies to warm comparisons must NOT apply here. See
``report/policy.py``: concurrency carries its own compute designation.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
from pydantic import BaseModel, ConfigDict

from .concurrency_shape import (
    MAX_CLIENT_CAPPED_PCT,
    MIN_ENGINE_TIME_PCT,
    MIN_ROWS_PER_CELL,
    PROTOCOL_LEVELS,
    SLA_MAX_SHED_PCT,
    UNFINISHED_CLASSES,
    WITHHELD_NO_ENGINE_TIME,
    client_cancel_predicate,
    shapes,
)

logger = logging.getLogger("benchmark.analysis.concurrency_grid")


class GridWorkload(BaseModel):
    """How one workload family is cut into grid cells.

    The same values drive :func:`benchmark.analysis.concurrency_shape.shapes` and
    the per-level SQL below, so the two populations are one population.
    """

    model_config = ConfigDict(frozen=True)

    #: Value of the export's ``workload`` column.
    name: str
    #: Result blocks pooled into the cells.
    blocks: tuple[str, ...]
    #: Rows a level must carry to count as measured.
    min_rows: int
    #: Pool campaign scale tags into one cell per (platform, compute size).
    pool_scales: bool
    #: Keep only these concurrency levels; None keeps every level run.
    levels: tuple[int, ...] | None


#: One full ClickBench pass is 43 rows per level; a few may be missing without the
#: level ceasing to be measured. Partial ladders (a level run once or twice while
#: a size was being brought up) fall below this and are excluded.
MIN_ROWS_PER_CLICKBENCH_CELL = 40

#: ClickBench levels kept: the protocol ladder (concurrency_shape.PROTOCOL_LEVELS)
#: through 50. Some ClickBench rows carry a level 40 that has no rung on the
#: frontier's ladder, and 50 was run at every size, so dropping 40 loses no ceiling.
#: 100 is dropped too: the suite's 43 queries run once per level, so 100 sessions
#: add only idle connections to the 50-session level.
CLICKBENCH_LEVELS = tuple(n for n in PROTOCOL_LEVELS if n <= 50)

DECISION_SUPPORT = GridWorkload(
    name="decision_support",
    blocks=("tpcds", "dsb"),
    min_rows=MIN_ROWS_PER_CELL,
    pool_scales=False,
    levels=None,
)
CLICKBENCH = GridWorkload(
    name="clickbench",
    blocks=("clickbench",),
    min_rows=MIN_ROWS_PER_CLICKBENCH_CELL,
    pool_scales=True,
    levels=CLICKBENCH_LEVELS,
)
WORKLOADS: tuple[GridWorkload, ...] = (DECISION_SUPPORT, CLICKBENCH)


def _level_sql(workload: GridWorkload) -> str:
    """Per-level rows for one workload, grouped exactly as ``shapes`` groups them."""
    blocks = ", ".join(f"'{b}'" for b in workload.blocks)
    scale_expr = "NULL::INTEGER" if workload.pool_scales else "scale_factor"
    group_by = ["platform", *([] if workload.pool_scales else ["scale_factor"])]
    group_by += ["compute_label", "concurrency"]
    level_filter = (
        f" AND concurrency IN ({', '.join(str(n) for n in workload.levels)})"
        if workload.levels
        else ""
    )
    unfinished = ", ".join(f"'{c}'" for c in UNFINISHED_CLASSES)
    engine_p50 = "quantile_cont(engine_ms, 0.5) FILTER (WHERE error_message IS NULL)"
    timed_pct = (
        "100.0 * count(engine_ms) / NULLIF(count(*) FILTER (WHERE error_message IS NULL), 0)"
    )

    def pct(cls: str) -> str:
        return f"100.0 * count(*) FILTER (WHERE failure_class = '{cls}') / count(*)"

    return f"""
    SELECT '{workload.name}' AS workload, platform, {scale_expr} AS scale_factor,
           compute_label, concurrency,
           CASE WHEN coalesce({timed_pct}, 100) >= {MIN_ENGINE_TIME_PCT}
                THEN {engine_p50} END AS p50_ms,
           100.0 * count(*) FILTER (WHERE failure_class IN ({unfinished}))
             / count(*) AS shed_pct,
           100.0 * count(*) FILTER (WHERE {client_cancel_predicate()})
             / count(*) AS client_capped_pct,
           count(*) AS runs,
           quantile_cont(execution_ms, 0.5)
             FILTER (WHERE error_message IS NULL) AS server_p50_ms,
           {timed_pct} AS engine_time_pct,
           {pct("timeout")} AS timeout_pct,
           {pct("connection")} AS connection_pct,
           {pct("engine")} AS engine_error_pct
    FROM concurrency_results
    WHERE block IN ({blocks}) AND failure_class IS DISTINCT FROM 'harness'
      AND compute_label IS NOT NULL AND concurrency IS NOT NULL{level_filter}
    GROUP BY {", ".join(group_by)}
    HAVING count(*) >= {workload.min_rows}
    """


def emit_concurrency_grid(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/concurrency_grid.parquet"),
) -> Path:
    """Emit the per-level concurrency grid with per-cell shape constants.

    Args:
        db_path: Results database to read.
        output_path: Parquet file to write.

    Returns:
        The path written.
    """
    from ..report.compute_rates import effective_usd_per_hour
    from ..report.views import install_views

    output_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        install_views(con)

        # SLA-independent per-cell constants, from the same reducer the report uses,
        # once per workload with that workload's own cut.
        cells = [
            (
                workload.name,
                c.platform,
                c.scale_factor,
                c.compute_label,
                c.k,
                c.r_squared,
                c.shed_pct_at_max,
                c.max_level,
                c.solo_latency_ms,
                c.failure_mode,
                c.publishable,
                c.withheld_reason,
                effective_usd_per_hour(c.platform, c.compute_label),
            )
            for workload in WORKLOADS
            for c in shapes(
                con,
                blocks=workload.blocks,
                min_rows=workload.min_rows,
                pool_scales=workload.pool_scales,
                levels=workload.levels,
            )
        ]
        con.execute("""
            CREATE OR REPLACE TEMP TABLE _cell_shape (
                workload VARCHAR, platform VARCHAR, scale_factor INTEGER,
                compute_label VARCHAR,
                k DOUBLE, r_squared DOUBLE, shed_pct_at_max DOUBLE,
                max_level INTEGER, solo_latency_ms DOUBLE, failure_mode VARCHAR,
                publishable BOOLEAN, withheld_reason VARCHAR, usd_per_hour DOUBLE
            )
        """)
        con.executemany(
            "INSERT INTO _cell_shape VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", cells
        )

        levels = " UNION ALL ".join(_level_sql(w) for w in WORKLOADS)
        # `IS NOT DISTINCT FROM` on the scale: the pooled workload's cells carry NULL.
        con.execute(f"""
            COPY (
                SELECT g.* REPLACE (
                           CASE WHEN s.withheld_reason = '{WITHHELD_NO_ENGINE_TIME}'
                                THEN NULL ELSE g.p50_ms END AS p50_ms
                       ),
                       s.k, s.r_squared, s.shed_pct_at_max, s.max_level,
                       s.solo_latency_ms, s.failure_mode, s.publishable, s.withheld_reason,
                       s.usd_per_hour,
                       {SLA_MAX_SHED_PCT} AS sla_max_shed_pct,
                       {MAX_CLIENT_CAPPED_PCT} AS max_client_capped_pct
                FROM ({levels}) g
                JOIN _cell_shape s
                  ON s.workload = g.workload
                 AND s.platform = g.platform
                 AND s.scale_factor IS NOT DISTINCT FROM g.scale_factor
                 AND s.compute_label = g.compute_label
                ORDER BY g.workload, g.platform, g.scale_factor, g.compute_label, g.concurrency
            ) TO '{output_path.as_posix()}' (FORMAT PARQUET)
        """)
    finally:
        con.close()
    logger.info("concurrency grid emitted to %s", output_path)
    return output_path

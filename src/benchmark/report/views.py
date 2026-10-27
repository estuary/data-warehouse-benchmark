"""Shared deduplicated views over ``results/benchmark.duckdb``.

The results table is append-only (CLAUDE.md golden rule 5): a re-run inserts a
new row rather than overwriting, so every reader must take the most recent row
per logical execution. If that dedup were re-implemented per call site, the
partition keys and error filters would inevitably diverge and "the same number"
could legitimately differ between the report, the drift gate, and the exported
parquets.

:func:`install_views` is the one place that dedup lives. Every entry point
that reads the results DB (``benchmark report``, ``verify-figures``,
``render-status``, ``analyze``) installs the same session-scoped temp views on
its own connection, so a consumer never needs to know which command it is
running under.

The layer stack:

``results_latest``
    Latest-wins over the FULL logical key — ``(platform, query_id,
    scale_factor, run_type, run_number, concurrency, compute_label)`` — with two
    filters applied first, which ``bench`` and ``bench_coverage`` share: burst rows
    at a level off the protocol ladder
    (:data:`benchmark.analysis.concurrency_shape.PROTOCOL_LEVELS`) are left out, and
    so are rows set aside (``metadata.set_aside``, :data:`benchmark.results.SET_ASIDE_KEY`),
    so the measurement they would have replaced stands. One row per logical execution, size-aware: a compute-size sweep
    keeps one row per size. Same schema as ``main.sql_benchmark_results``.
    **Everything that wants "the current measurement" reads this or a view
    built on it; no other ``ROW_NUMBER`` dedup should exist.**

    One column differs from the table: ``server_time_ms`` is the engine's time on
    the query (:func:`benchmark.analysis.engine_time.engine_time_sql`): execution,
    plus compilation and queue wait on the engines that report them apart
    (Databricks' compile time; Databricks', Redshift's and BigQuery's queue time),
    on every run type, so warm and concurrency latency count the same time. It is
    NULL where an execution-only engine's queue time was not captured. The table
    keeps the engine's own execution-only figure, and the views add it as
    ``execution_ms`` (compile included). ``bench`` and ``bench_coverage`` carry the
    same two columns.

``concurrency_results``
    The concurrency rows of ``results_latest`` with two derived columns every
    concurrency consumer shares: ``engine_ms``, the ``server_time_ms`` above on a
    successful query (NULL on a failed one), and ``failure_class``
    (NULL, ``harness``, ``timeout``, ``connection`` or ``engine``). Both are defined
    in :mod:`benchmark.analysis.concurrency_shape`.

``sustained_results``
    The sustained-load rows (run type ``results.SUSTAINED_RUN_TYPE``),
    latest-wins, with the same two columns. They are left out of
    ``results_latest`` and of every view built on it or beside it: a sustained
    pass is a different experiment from the bursts every figure reads.

``bench`` / ``bench_coverage``
    The comparison populations (designated tier; ``bench`` adds the admission
    policy). These are NOT derived from ``results_latest`` on purpose: their
    dedup key deliberately omits ``compute_label`` so that a (platform, scale)
    with no canonical-tier entry falls back to plain latest-wins ACROSS sizes —
    deriving them from the size-aware view would keep one row per size in
    exactly those fallback cells and change what the report publishes.

``bench_cb``
    ClickBench's population: size-aware and not canonical-filtered (one fixed
    ~100 GB dataset has no designated-tier-per-scale), warm rows policy
    filtered. Derived from ``results_latest``; the policy predicates exclude
    whole partitions (a replica config, a DNF cell), never individual rows
    within one, so filtering after dedup is equivalent to before.

Alongside the views, two temp tables are installed for consumers that need the
policy *inputs* rather than the admitted population: ``_canonical_tier``
(see :mod:`benchmark.report.canonical`) and ``_dnf_cells``
(see :mod:`benchmark.report.policy`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

logger = logging.getLogger("benchmark.report.views")

#: The full logical-execution key. ``concurrency`` is NULL outside concurrency
#: runs and ``compute_label`` is NULL where environment capture failed, and SQL
#: window partitions would treat each NULL as its own group inconsistently
#: across engines — the COALESCEs pin both to one sentinel per key.
LOGICAL_KEY = (
    "platform, query_id, scale_factor, run_type, run_number, "
    "COALESCE(concurrency, -1), COALESCE(compute_label, '__default__')"
)


def install_views(con: duckdb.DuckDBPyConnection, *, force: bool = False) -> None:
    """Install the shared temp views/tables on ``con`` (idempotent).

    Safe on read-only connections (everything is session-scoped temp). With
    ``force=False`` an already-installed connection is left untouched, so
    helpers can call this defensively without re-resolving the canonical map;
    the report generator passes ``force=True`` once per render to guarantee a
    fresh install.
    """
    if not force:
        installed = con.execute(
            "SELECT count(*) FROM duckdb_views() WHERE view_name = 'results_latest'"
        ).fetchone()
        if installed and installed[0]:
            return

    # A database with no results table has nothing to install (and CREATE VIEW
    # would fail binding). This is the normal state of the synthetic DBs the
    # drift-gate mechanics tests build — they exercise table parsing and drift
    # classification with fake verifiers, not the real slices.
    has_table = con.execute(
        "SELECT count(*) FROM information_schema.tables"
        " WHERE table_schema = 'main' AND table_name = 'sql_benchmark_results'"
    ).fetchone()
    if not (has_table and has_table[0]):
        logger.debug("no main.sql_benchmark_results in this database; views not installed")
        return

    from ..analysis.concurrency_shape import (
        PROTOCOL_LEVELS,
        engine_ms_sql,
        failure_class_sql,
    )
    from ..analysis.engine_time import engine_time_sql, execution_sql
    from ..results import SET_ASIDE_KEY, SUSTAINED_RUN_TYPE
    from .canonical import create_canonical_table
    from .policy import (
        create_dnf_table,
        dnf_exclusion_sql,
        single_session_replica_predicate,
    )

    create_canonical_table(con)  # _canonical_tier
    create_dnf_table(con)  # _dnf_cells (policy notes; predicates inline the SQL)

    # Profiling columns a minimal test database may not carry read as NULL.
    present = {
        str(r[0])
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'main' AND table_name = 'sql_benchmark_results'"
        ).fetchall()
    }
    # server_time_ms as the engine's time on the query: compilation and queue wait
    # included where the engine reports them apart (analysis.engine_time), so warm,
    # cold and concurrency latency all count the same time.
    # execution_ms keeps the engine's execution figure (compile included) beside it, for
    # the comparisons that want the engine-reported number as it stands.
    as_engine_time = (
        f" REPLACE ({engine_time_sql(present=present)} AS server_time_ms),"
        f" {execution_sql(present=present)} AS execution_ms"
        if "server_time_ms" in present
        else ", NULL::DOUBLE AS execution_ms"
    )

    # Sustained-load rows (run type SUSTAINED_RUN_TYPE) are a different experiment
    # from every figure below, which reads bursts; they get their own view at the end.
    # Burst rows off the protocol ladder (ClickBench's development 40-session level)
    # are left out of every view (analysis.concurrency_shape.PROTOCOL_LEVELS).
    levels = ", ".join(str(n) for n in PROTOCOL_LEVELS)
    on_ladder = (
        f"(r.run_type <> 'concurrency' OR r.concurrency IS NULL OR r.concurrency IN ({levels}))"
    )
    # Rows set aside (metadata.set_aside, a reason; scripts/ops/set_aside_rows.py) stay in
    # the table but out of every view, before latest-wins, so the rows they would have
    # superseded stay published.
    if "metadata" in present:
        on_ladder += f" AND json_extract_string(r.metadata, '$.{SET_ASIDE_KEY}') IS NULL"
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW _results_all_latest AS
        SELECT * EXCLUDE (rn){as_engine_time} FROM (
            SELECT r.*, ROW_NUMBER() OVER (
                PARTITION BY {LOGICAL_KEY}
                ORDER BY created_at DESC
            ) AS rn
            FROM main.sql_benchmark_results r
            WHERE {on_ladder}
        ) WHERE rn = 1
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW results_latest AS
        SELECT * FROM _results_all_latest WHERE run_type <> '{SUSTAINED_RUN_TYPE}'
    """)

    # Apples-to-apples: restrict to each engine's DESIGNATED compute tier per
    # scale (run_plan-derived; see report/canonical.py) before latest-wins.
    # Without this, size-sweep rows make "latest" an arbitrary tier per key —
    # e.g. Snowflake's X-Large sweep vs ClickHouse's run-plan tier. NULL labels
    # are NOT admitted where a canonical entry exists (a missing label is a
    # capture failure, not an era marker — see report/canonical.py); a
    # (platform, scale) with no canonical entry keeps all rows (fallback),
    # which is why the dedup key here must NOT carry compute_label (see the
    # module docstring) and these two views stay independent of results_latest.
    #
    # On top of the tier filter, `bench` applies the comparison-admission policy
    # (report/policy.py): warm rows from DNF cells
    # and ClickHouse multi-replica warm rows are excluded from comparisons.
    # Concurrency rows are exempt — replicas are the throughput knob that axis
    # measures. Coverage and other bookkeeping read `bench_coverage`, which is
    # identical but WITHOUT the policy, so a measured-but-excluded cell never
    # masquerades as never-ran.
    canonical_dedup = f"""
        SELECT r.*, ROW_NUMBER() OVER (
            PARTITION BY r.platform, r.query_id, r.scale_factor, r.run_type,
                         r.run_number, COALESCE(r.concurrency, -1)
            ORDER BY r.created_at DESC
        ) AS rn
        FROM main.sql_benchmark_results r
        LEFT JOIN _canonical_tier c
          ON r.platform = c.platform AND r.scale_factor = c.scale_factor
         AND c.run_class = CASE WHEN r.run_type = 'concurrency'
                                THEN 'concurrency' ELSE 'warm' END
        WHERE (c.label IS NULL OR r.compute_label = c.label)
          AND r.run_type <> '{SUSTAINED_RUN_TYPE}' AND {on_ladder}
    """
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW bench AS
        SELECT * EXCLUDE (rn){as_engine_time} FROM (
            {canonical_dedup}
              AND (r.run_type = 'concurrency'
                   OR {single_session_replica_predicate("r")})
              AND (r.run_type = 'concurrency'
                   OR {dnf_exclusion_sql("r")})
        ) WHERE rn = 1
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW bench_coverage AS
        SELECT * EXCLUDE (rn){as_engine_time} FROM (
            {canonical_dedup}
        ) WHERE rn = 1
    """)
    # ClickBench's own view: size-aware and NOT canonical-filtered. The dedup
    # key (results_latest's) carries compute_label so a size sweep keeps one
    # row per size, and scale_factor stays in the key only to keep re-runs of
    # the same campaign from colliding — every consumer pools it via
    # generator._scale_expr. Warm rows are policy-filtered (single-replica only
    # for ClickHouse); the concurrency-protocol rows stay for the concurrency
    # section.
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW bench_cb AS
        SELECT * FROM results_latest r
        WHERE r.block = 'clickbench'
          AND (r.run_type = 'concurrency'
               OR {single_session_replica_predicate("r")})
          AND (r.run_type = 'concurrency'
               OR {dnf_exclusion_sql("r")})
    """)
    fill = "".join(
        f", NULL::{kind} AS {c}"
        for c, kind in (
            ("error_message", "VARCHAR"),
            ("server_time_ms", "DOUBLE"),
            ("queue_ms", "DOUBLE"),
            ("compile_ms", "DOUBLE"),
        )
        if c not in present
    )
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW concurrency_results AS
        SELECT r.*,
               {failure_class_sql("r.error_message")} AS failure_class,
               {engine_ms_sql("r")} AS engine_ms
        FROM (SELECT *{fill} FROM results_latest) r
        WHERE r.run_type = 'concurrency'
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW sustained_results AS
        SELECT r.*,
               {failure_class_sql("r.error_message")} AS failure_class,
               {engine_ms_sql("r")} AS engine_ms
        FROM (SELECT *{fill} FROM _results_all_latest) r
        WHERE r.run_type = '{SUSTAINED_RUN_TYPE}'
    """)
    logger.debug("shared report views installed")

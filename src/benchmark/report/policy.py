"""Comparison-admission policy: which measurements enter cross-engine comparisons.

Two rules, both about the same failure mode: a number that is real data but not a
fair comparison.

**DNF floor.** An engine that fails more than ``DNF_MAX_FAILURE_SHARE`` of a query
suite's warm pass at a scale **did not finish** that suite at that scale. Its
latency aggregates there are survivor statistics over whichever queries happened to
succeed, so they enter no cross-engine comparison; the report states the completion
figure instead. The rule is per suite (block) with the suite size read from the
data, so it binds any benchmark set added later without new code, and it is
suite-agnostic on purpose: failing a tenth of the warm pass is a result about the
workload whichever benchmark it came from. The verdict is judged **at the largest
compute size measured** for that (engine, suite, scale): a small tier that cannot
finish is a sizing result that belongs on the completion curve, and the engine is
DNF only when its biggest measured compute cannot clear the floor either. Nothing
is deleted — the rows stay in the results DB, the coverage matrix, and the
failure-mode analysis. The qualifying cells are computed from the data
(:func:`dnf_cells`) and named in the report's policy note.

**Single-replica single-session.** ClickHouse Cloud scales horizontally by
replicas, but by default replicas do not pool memory for a single query — they add
throughput and HA. Every other engine's sizing dial (warehouse size, RPUs, slots, Duckling
tier) accelerates a single query; a replica count does not. So single-session
(warm) comparisons admit only ClickHouse's single-replica configurations, keeping
its axis consistent with the other engines' vertical ladders. Multi-replica
configurations are reported on the concurrency axis only, which measures the thing
replicas actually buy. (The one multi-replica warm measurement we hold confirms
the rationale: two replicas returned a ClickBench geomean within 2% of one.)

Both rules bind **warm/single-session comparisons only**. Concurrency tables keep
their own compute designation (ClickHouse ran that ladder at 3 replicas, which is
the throughput knob being measured there).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

logger = logging.getLogger("benchmark.report.policy")

#: Failing more than this share of a suite's warm pass at a scale makes the engine
#: DNF for that suite at that scale: failing over a tenth of a warm pass is a problem
#: whichever benchmark it is. Exactly 10% failed is NOT more than 10%.
DNF_MAX_FAILURE_SHARE = 0.10

#: Blocks whose ``scale_factor`` is a campaign-tracking artifact, not a data
#: volume (one fixed dataset). DNF for these evaluates the suite pooled across
#: scale tags — per-tag evaluation would judge partial slices of one measurement.
SINGLE_SCALE_BLOCKS = ("clickbench",)

_SINGLE_SCALE_SQL = "(" + ", ".join(f"'{b}'" for b in SINGLE_SCALE_BLOCKS) + ")"

#: Pooled blocks collapse onto this sentinel scale key inside the policy tables.
_POOLED_SF_KEY = -1


def _sf_key_expr(alias: str) -> str:
    """The (block-aware) scale key an alias row is judged under."""
    return (
        f"CASE WHEN {alias}.block IN {_SINGLE_SCALE_SQL} "
        f"THEN {_POOLED_SF_KEY} ELSE {alias}.scale_factor END"
    )


def single_session_replica_predicate(alias: str = "r") -> str:
    """SQL predicate admitting only single-replica ClickHouse rows.

    Compute labels are ``<replicas>x<memory>GB`` for ClickHouse, so single-replica
    is the ``1x`` prefix. Other platforms pass untouched. NULL labels fail the
    LIKE and are excluded — for ClickHouse a label is the only record of the
    replica count, so an unlabelled row cannot be admitted to a replica-scoped
    comparison.
    """
    return f"({alias}.platform <> 'clickhouse' OR {alias}.compute_label LIKE '1x%')"


def _size_ordinal_sql(expr: str) -> str:
    """Provisioned-capacity ordinal for a compute label, in SQL.

    Only needs to order the sizes ONE engine measured at one scale against each
    other — never across engines — so a coarse ladder suffices: named T-shirt and
    Duckling tiers by rank, ``N RPU`` / ``N slots`` by N, ClickHouse ``RxMGB`` by
    total memory, ``on-demand`` below any reservation (it is a pricing model, not
    a size). Named tiers are matched before the numeric fallback because
    '2X-Small' contains a digit.
    """
    stripped = f"REPLACE({expr}, ' (Gen2)', '')"
    return f"""CASE {stripped}
        WHEN '2X-Small' THEN 1 WHEN 'X-Small' THEN 2 WHEN 'Small' THEN 3
        WHEN 'Medium' THEN 4 WHEN 'Large' THEN 5 WHEN 'X-Large' THEN 6
        WHEN '2X-Large' THEN 7 WHEN '3X-Large' THEN 8 WHEN '4X-Large' THEN 9
        WHEN 'Pulse' THEN 1 WHEN 'Standard' THEN 2 WHEN 'Jumbo' THEN 3
        WHEN 'Mega' THEN 4 WHEN 'Giga' THEN 5
        WHEN 'on-demand' THEN 0
        ELSE CASE
            WHEN REGEXP_MATCHES({expr}, '^\\d+x\\d+GB$')
            THEN TRY_CAST(REGEXP_EXTRACT({expr}, '^(\\d+)x', 1) AS BIGINT)
                 * TRY_CAST(REGEXP_EXTRACT({expr}, 'x(\\d+)GB$', 1) AS BIGINT)
            ELSE COALESCE(TRY_CAST(REGEXP_EXTRACT({expr}, '(\\d+)', 1) AS BIGINT), 0)
        END
    END"""


#: Per (platform, block, scale key): completion at the judged (largest) size, with
#: the suite size — the shared body behind the DNF floor and the allowance census.
_JUDGED_CELLS_SQL = f"""
    WITH warm AS (
        SELECT platform, block,
               CASE WHEN block IN {_SINGLE_SCALE_SQL}
                    THEN {_POOLED_SF_KEY} ELSE scale_factor END AS sf_key,
               compute_label, query_id, error_message
        FROM main.sql_benchmark_results
        WHERE run_type = 'warm'
    ),
    -- The verdict size: the largest labelled configuration measured in the cell.
    -- Cells with only NULL labels have no size to judge at and evaluate pooled.
    largest AS (
        SELECT platform, block, sf_key, compute_label AS label FROM (
            SELECT platform, block, sf_key, compute_label,
                   ROW_NUMBER() OVER (
                       PARTITION BY platform, block, sf_key
                       ORDER BY {_size_ordinal_sql("compute_label")} DESC,
                                compute_label DESC) AS rn
            FROM (SELECT DISTINCT platform, block, sf_key, compute_label
                  FROM warm WHERE compute_label IS NOT NULL)
        ) WHERE rn = 1
    ),
    judged AS (
        SELECT w.platform, w.block, w.sf_key, l.label,
               COUNT(DISTINCT w.query_id)
                   FILTER (WHERE w.error_message IS NULL) AS ok
        FROM warm w
        LEFT JOIN largest l
          ON w.platform = l.platform AND w.block = l.block AND w.sf_key = l.sf_key
        WHERE l.label IS NULL OR w.compute_label = l.label
        GROUP BY 1, 2, 3, 4
    )
    SELECT j.platform, j.block, j.sf_key, j.label, j.ok, s.n AS suite_n
    FROM judged j
    JOIN (
        SELECT block, COUNT(DISTINCT query_id) AS n
        FROM main.sql_benchmark_results GROUP BY 1
    ) s USING (block)
"""

_DNF_CELLS_SQL = f"""
    SELECT * FROM ({_JUDGED_CELLS_SQL})
    WHERE ok < suite_n * (1 - {DNF_MAX_FAILURE_SHARE})
"""

#: Every measured configuration judged on ITS OWN completion, rather than the scale
#: judged at its largest rung. Two different questions, both legitimate:
#:
#:   * ``_JUDGED_CELLS_SQL`` (largest rung) asks "can this engine finish the suite at
#:     this volume if given enough compute?" — the right question for an engine-level
#:     verdict, and what the DNF note reports.
#:   * this one asks "did THIS configuration finish the suite?" — the right question
#:     for any table that aggregates one specific cell.
#:
#: Conflating them is not academic: a large rung that clears the floor makes the
#: SCALE pass and silently admits a smaller, incomplete cell beneath it — e.g. a
#: ClickHouse 1x120GB rung completing 96% of the suite admitting the designated
#: 1x32GB cell at 80%, whose published median would be taken over survivors, the
#: exact bias the table's own prose warns about. Adding a LARGER configuration
#: must never admit a smaller, incomplete one.
_JUDGED_CELLS_BY_LABEL_SQL = f"""
    WITH warm AS (
        SELECT platform, block,
               CASE WHEN block IN {_SINGLE_SCALE_SQL}
                    THEN {_POOLED_SF_KEY} ELSE scale_factor END AS sf_key,
               compute_label, query_id, error_message
        FROM main.sql_benchmark_results
        WHERE run_type = 'warm' AND compute_label IS NOT NULL
    ),
    judged AS (
        SELECT platform, block, sf_key, compute_label AS label,
               COUNT(DISTINCT query_id)
                   FILTER (WHERE error_message IS NULL) AS ok
        FROM warm GROUP BY 1, 2, 3, 4
    )
    SELECT j.platform, j.block, j.sf_key, j.label, j.ok, s.n AS suite_n
    FROM judged j
    JOIN (
        SELECT block, COUNT(DISTINCT query_id) AS n
        FROM main.sql_benchmark_results GROUP BY 1
    ) s USING (block)
"""

_DNF_CELLS_BY_LABEL_SQL = f"""
    SELECT * FROM ({_JUDGED_CELLS_BY_LABEL_SQL})
    WHERE ok < suite_n * (1 - {DNF_MAX_FAILURE_SHARE})
"""

#: Cells that PASS the floor while using its allowance (some failures, but no more
#: than the permitted share). The allowance is directionally biased — dropped
#: queries are disproportionately the expensive ones, so an admitted-but-incomplete
#: cell flatters the engine that failed — which is why the policy note counts them.
ALLOWANCE_CELLS_SQL = f"""
    SELECT * FROM ({_JUDGED_CELLS_SQL})
    WHERE ok < suite_n
      AND ok >= suite_n * (1 - {DNF_MAX_FAILURE_SHARE})
"""


def dnf_exclusion_sql(alias: str = "r", *, by_label: bool = False) -> str:
    """Self-contained SQL predicate excluding an alias row from DNF cells.

    Inlines the floor computation over ``main.sql_benchmark_results`` rather than
    referencing the ``_dnf_cells`` temp table, so any slice helper can embed it
    without a view-setup ordering dependency. Applies to every block; single-scale
    blocks are judged pooled across their scale tags.

    Args:
        alias: the row alias to test.
        by_label: judge each configuration on its own completion instead of on the
            scale's largest rung. Pass ``True`` whenever the caller has already
            restricted rows to one configuration (a designated tier), because the
            aggregate then describes THAT cell and must be filtered by that cell's
            completion. Leaving it ``False`` there lets a larger rung admit a
            smaller, incomplete one — see ``_JUDGED_CELLS_BY_LABEL_SQL``.
    """
    if by_label:
        return f"""(NOT EXISTS (
        SELECT 1 FROM ({_DNF_CELLS_BY_LABEL_SQL}) _dnf
        WHERE _dnf.platform = {alias}.platform
          AND _dnf.block = {alias}.block
          AND _dnf.sf_key = {_sf_key_expr(alias)}
          AND _dnf.label = {alias}.compute_label))"""
    return f"""(NOT EXISTS (
        SELECT 1 FROM ({_DNF_CELLS_SQL}) _dnf
        WHERE _dnf.platform = {alias}.platform
          AND _dnf.block = {alias}.block
          AND _dnf.sf_key = {_sf_key_expr(alias)}))"""


#: One DNF verdict: (platform, block, scale-or-None, judged-label-or-None,
#: completed, suite size).
DnfCell = tuple[str, str, int | None, str | None, int, int]


def admitted_floor_headroom(
    con: duckdb.DuckDBPyConnection, blocks: tuple[str, ...]
) -> tuple[float, int] | None:
    """(worst completion share, count of incomplete cells) among ADMITTED cells.

    The floor's allowance is directionally biased — the queries an engine drops
    are disproportionately the expensive ones, so an admitted-but-incomplete cell
    always flatters the engine that failed. This measures how much of that
    allowance the current data actually uses, so the policy note can state it
    instead of asserting it. ``None`` when the blocks hold no judged cells.
    """
    blocks_sql = "(" + ", ".join(f"'{b}'" for b in blocks) + ")"
    row = con.execute(f"""
        SELECT MIN(ok * 1.0 / suite_n),
               COUNT(*) FILTER (WHERE ok < suite_n)
        FROM ({_JUDGED_CELLS_SQL})
        WHERE ok >= suite_n * (1 - {DNF_MAX_FAILURE_SHARE})
          AND block IN {blocks_sql}
    """).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0]), int(row[1])


def dnf_cells(con: duckdb.DuckDBPyConnection) -> list[DnfCell]:
    """(platform, block, scale-or-None, judged label, completed, suite) per cell.

    ``scale`` is ``None`` for single-scale blocks (judged pooled). Completion is
    distinct error-free queries in the warm pass **at the largest labelled compute
    size measured in the cell** (the judged label; ``None`` when only unlabelled
    rows exist and the cell evaluates pooled), over the suite size read from the
    data. The judgment considers everything measured — including configurations
    other admission rules exclude from comparisons — because a DNF verdict is
    about the engine finishing the workload, not about comparability. A query
    never attempted warm counts as not finished: warm passes attempt the whole
    suite, so an absence is a run that stopped, not a smaller suite.
    """
    rows = con.execute(f"{_DNF_CELLS_SQL} ORDER BY 1, 2, 3").fetchall()
    cells: list[DnfCell] = [
        (
            str(p),
            str(b),
            None if int(sf) == _POOLED_SF_KEY else int(sf),
            None if label is None else str(label),
            int(ok),
            int(n),
        )
        for p, b, sf, label, ok, n in rows
    ]
    for platform, block, scale, label, ok, n in cells:
        logger.info(
            "DNF: %s %s%s completed %d of %d at largest size %s (failed > %.0f%%)",
            platform,
            block,
            "" if scale is None else f" SF{scale}",
            ok,
            n,
            label or "(unlabelled)",
            DNF_MAX_FAILURE_SHARE * 100,
        )
    return cells


def create_dnf_table(con: duckdb.DuckDBPyConnection) -> list[DnfCell]:
    """Materialize DNF cells as temp table ``_dnf_cells`` (for the policy note and
    other renderers; slice predicates use :func:`dnf_exclusion_sql` inline)."""
    cells = dnf_cells(con)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _dnf_cells "
        "(platform VARCHAR, block VARCHAR, scale_factor INTEGER, "
        " judged_label VARCHAR, completed INTEGER, suite_n INTEGER)"
    )
    if cells:
        con.executemany("INSERT INTO _dnf_cells VALUES (?, ?, ?, ?, ?, ?)", cells)
    return cells

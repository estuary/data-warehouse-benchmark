"""Integration tests against the committed results database.

`tests/integration/test_e2e_pipeline.py` drives the pipeline over synthetic SF1
data it generates itself. That proves the plumbing runs, but it cannot catch a
reducer that silently disagrees with its own export on real measurements — the
synthetic corpus has no concurrency ladder, no compute-size sweep and no shed
behaviour to disagree about.

`results/benchmark.duckdb` ships with the repo (Git LFS), so those checks run
in CI against the real measurements: 261k measured executions across seven
engines, four scales and every warehouse size.

The invariant these tests exist for is the concurrency one. The published
useful-concurrency metric is computed in Python by
:func:`benchmark.analysis.concurrency_shape.shapes`, but the browser explorer
recomputes it in SQL from `concurrency_grid.parquet` so its SLA control can
re-target the metric without re-running the reducer. Those are two
implementations of one definition, and nothing but a test keeps them honest.

All tests here skip cleanly when the database is absent or is still an
unfetched LFS pointer, so a clone without `git lfs install` gets a skip rather
than a confusing failure.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from benchmark.analysis.concurrency_grid import (
    CLICKBENCH,
    WORKLOADS,
    GridWorkload,
    emit_concurrency_grid,
)
from benchmark.analysis.concurrency_shape import shapes
from benchmark.analysis.pattern_load import emit_pattern_load
from benchmark.analysis.query_catalog import emit_query_catalog
from benchmark.analysis.rate_card import emit_rate_card
from benchmark.analysis.size_ladder import emit_size_ladder
from benchmark.analysis.skew_load import MIN_PAIRS_PER_CELL, emit_skew_load
from benchmark.report.verifiers import _ladder_common_complete, returns_qnums

RESULTS_DB = Path("results/benchmark.duckdb")

#: First bytes of a Git LFS pointer file. A clone without LFS leaves a ~130-byte
#: text stub here instead of the database, which DuckDB reports as corruption.
LFS_POINTER_PREFIX = b"version https://git-lfs"

#: Latency targets to check the SQL/Python agreement at. One value could agree by
#: coincidence; the disagreement modes (an off-by-one on the shed threshold, a
#: missing zero-coalesce) show up at some targets and not others.
SLA_TARGETS_MS = (1_000.0, 5_000.0, 20_000.0)


def _skip_reason() -> str | None:
    """Why these tests cannot run here, or None if they can."""
    if not RESULTS_DB.exists():
        return f"{RESULTS_DB} not present"
    with RESULTS_DB.open("rb") as fh:
        if fh.read(len(LFS_POINTER_PREFIX)) == LFS_POINTER_PREFIX:
            return f"{RESULTS_DB} is an unfetched Git LFS pointer — run `git lfs pull`"
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


@pytest.fixture(scope="module")
def con() -> duckdb.DuckDBPyConnection:
    """Read-only connection to the committed results database."""
    from benchmark.report.views import install_views

    connection = duckdb.connect(str(RESULTS_DB), read_only=True)
    install_views(connection)
    yield connection
    connection.close()


def test_database_holds_the_concurrency_campaign(con: duckdb.DuckDBPyConnection) -> None:
    """The shipped database is the real campaign, not a truncated sample.

    The ladder is asserted exactly rather than by count. The explorer hard-codes
    it as the domain of its frontier axis (`MEASURED_LEVELS` in viz/src/constants.js),
    on the argument that useful concurrency can take no other value — so if the
    ladder ever changes, that axis is wrong and this should say so.

    ClickBench ran an extra level (40) and is excluded here for the same reason
    the grid export excludes it: the decision-support suites are what the
    concurrency views are built from.
    """
    engines, levels, scales, rows = con.execute(
        """
        SELECT count(DISTINCT platform), list(DISTINCT concurrency ORDER BY concurrency),
               count(DISTINCT scale_factor), count(*)
        FROM results_latest
        WHERE run_type = 'concurrency' AND block IN ('tpcds', 'dsb')
        """
    ).fetchone()
    assert engines == 7, f"expected all 7 engines, found {engines}"
    assert levels == [1, 5, 10, 20, 50, 100], f"unexpected ladder: {levels}"
    assert scales == 4, f"expected SF1-SF1000, found {scales}"
    assert rows > 100_000, f"suspiciously few concurrency rows: {rows}"


@pytest.mark.parametrize("sla_ms", SLA_TARGETS_MS)
@pytest.mark.parametrize("workload", WORKLOADS, ids=lambda w: w.name)
def test_grid_export_reproduces_the_reducer(
    tmp_path: Path, sla_ms: float, workload: GridWorkload
) -> None:
    """The explorer's SQL and the report's Python agree on useful concurrency.

    This is the load-bearing invariant. `concurrency_grid.parquet` deliberately
    ships per-level rows so a consumer can re-target the SLA, which only works
    if recomputing the metric in SQL lands exactly where `shapes()` does. Held
    per workload: ClickBench is cut differently (pooled tags, its own row floor,
    the unified level ladder) and the two cuts must each reproduce their reducer.
    """
    grid = emit_concurrency_grid(RESULTS_DB, tmp_path / "concurrency_grid.parquet")

    in_sql = {
        (platform, scale, label): useful
        for platform, scale, label, useful in duckdb.connect()
        .execute(
            f"""
            SELECT platform, scale_factor, compute_label,
                   coalesce(max(concurrency) FILTER (
                       WHERE p50_ms <= {sla_ms} AND shed_pct < sla_max_shed_pct), 0)
            FROM '{grid.as_posix()}'
            WHERE workload = '{workload.name}'
            GROUP BY 1, 2, 3
            """
        )
        .fetchall()
    }

    con = duckdb.connect(str(RESULTS_DB), read_only=True)
    try:
        in_python = {
            (c.platform, c.scale_factor, c.compute_label): c.useful_concurrency
            for c in shapes(
                con,
                sla_ms=sla_ms,
                blocks=workload.blocks,
                min_rows=workload.min_rows,
                pool_scales=workload.pool_scales,
                levels=workload.levels,
            )
        }
    finally:
        con.close()

    assert in_sql.keys() == in_python.keys(), "the export and the reducer cover different cells"
    mismatches = {k: (in_python[k], in_sql[k]) for k in in_python if in_python[k] != in_sql[k]}
    assert not mismatches, f"python vs sql disagree at SLA={sla_ms:.0f}ms: {mismatches}"


def test_grid_export_cuts_clickbench_as_documented(tmp_path: Path) -> None:
    """ClickBench cells pool the campaign tag and sit on the unified level ladder.

    The explorer offers ClickBench beside the scale factors; it relies on the
    tag being pooled (one dot per compute size, not one per tag) and on every
    level being a rung of the ladder its frontier axis draws.
    """
    grid = emit_concurrency_grid(RESULTS_DB, tmp_path / "grid.parquet")
    con = duckdb.connect()
    tagged, off_ladder, cells, engines = con.execute(
        f"""
        SELECT count(*) FILTER (WHERE scale_factor IS NOT NULL),
               count(*) FILTER (WHERE concurrency NOT IN {CLICKBENCH.levels}),
               count(DISTINCT (platform, compute_label)),
               count(DISTINCT platform)
        FROM '{grid.as_posix()}' WHERE workload = 'clickbench'
        """
    ).fetchone()
    assert tagged == 0, f"{tagged} ClickBench rows still carry a campaign tag as scale"
    assert off_ladder == 0, f"{off_ladder} ClickBench rows sit off the unified ladder"
    assert engines == 7, f"expected every engine in the ClickBench grid, found {engines}"
    assert cells >= engines, "fewer ClickBench cells than engines"
    untagged_ds = con.execute(
        f"SELECT count(*) FROM '{grid.as_posix()}' "
        "WHERE workload = 'decision_support' AND scale_factor IS NULL"
    ).fetchone()[0]
    assert untagged_ds == 0, "decision-support rows must keep their scale factor"


def test_grid_export_prices_every_published_cell(tmp_path: Path) -> None:
    """A frontier point needs a cost axis; an unpriced cell cannot be plotted."""
    grid = emit_concurrency_grid(RESULTS_DB, tmp_path / "grid.parquet")
    unpriced = (
        duckdb.connect()
        .execute(
            f"SELECT count(*) FROM '{grid.as_posix()}' WHERE publishable AND usd_per_hour IS NULL"
        )
        .fetchone()[0]
    )
    assert unpriced == 0, f"{unpriced} publishable rows have no hourly rate"


def test_pattern_load_normalisation_is_sound(tmp_path: Path) -> None:
    """`rel` must be a usable ratio: a positive base, and no absurd values.

    A zero or missing `cell_ms` would make the ratio infinite or undefined, and
    the explorer plots it on a log axis where either kills the mark silently.
    """
    export = emit_pattern_load(RESULTS_DB, tmp_path / "pattern_load.parquet")
    bad_base, min_rel, max_rel, engines = (
        duckdb.connect()
        .execute(
            f"""
            SELECT sum(CASE WHEN cell_ms IS NULL OR cell_ms <= 0 THEN 1 ELSE 0 END),
                   min(pattern_ms / cell_ms), max(pattern_ms / cell_ms),
                   count(DISTINCT platform)
            FROM '{export.as_posix()}'
            """
        )
        .fetchone()
    )
    assert bad_base == 0, f"{bad_base} rows have a non-positive normalisation base"
    assert engines == 7, f"expected all 7 engines, found {engines}"
    # Ratios are within-engine, so a pattern 100x its own engine's typical query
    # would mean the grouping broke rather than that a query is slow.
    assert 0.0 < min_rel < 1.0, f"implausible minimum ratio {min_rel}"
    assert 1.0 < max_rel < 100.0, f"implausible maximum ratio {max_rel}"


def test_skew_load_pairs_the_two_suites_per_cell(tmp_path: Path) -> None:
    """Every cell's ratio rests on enough paired query numbers, and the pairing
    is one-to-one: a cell can never have more pairs than shared query numbers.

    The explorer plots `skew_ratio` on a log axis, so a zero or missing value
    would drop the mark silently; and a ratio far from 1 in either direction
    would mean the two suites were not matched by query number.
    """
    export = emit_skew_load(RESULTS_DB, tmp_path / "skew_load.parquet")
    bad, min_pairs, max_pairs, min_ratio, max_ratio, engines, cells = (
        duckdb.connect()
        .execute(
            f"""
            SELECT sum(CASE WHEN skew_ratio IS NULL OR skew_ratio <= 0 THEN 1 ELSE 0 END),
                   min(pairs), max(pairs), min(skew_ratio), max(skew_ratio),
                   count(DISTINCT platform), count(*)
            FROM '{export.as_posix()}'
            """
        )
        .fetchone()
    )
    assert bad == 0, f"{bad} cells have a non-positive skew ratio"
    assert min_pairs >= MIN_PAIRS_PER_CELL
    assert max_pairs <= 99, f"a cell pairs {max_pairs} query numbers; only 99 are shared"
    assert engines == 7, f"expected all 7 engines, found {engines}"
    assert cells > 500, f"suspiciously few cells: {cells}"
    # Within-cell ratios of the same query's two variants: an order of magnitude
    # either way would mean the pairing broke, not that skew is that costly.
    assert min_ratio > 0.1 and max_ratio < 10.0, f"implausible ratio range {min_ratio}..{max_ratio}"


#: (platform, scale) ladders the report's verifiers publish; the explorer's
#: sizing views rebuild them from `size_ladder.parquet`.
LADDERS = (("snowflake", 100), ("snowflake", 1000), ("redshift", 1), ("motherduck", 1000))


def test_size_ladder_reproduces_the_report_ladders(
    con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """Common-complete suite totals from the export equal the report's own.

    The explorer sums `size_ladder.parquet` over the queries every size
    completed, the way `verifiers._ladder_common_complete` does over the
    database. Two implementations of one ladder; this keeps them equal.
    """
    export = emit_size_ladder(RESULTS_DB, tmp_path / "size_ladder.parquet")
    reader = duckdb.connect()
    for platform, scale in LADDERS:
        expected = _ladder_common_complete(con, platform, scale)
        got = {
            label: (n, suite_s, cost)
            for label, n, suite_s, cost in reader.execute(
                f"""
                WITH d AS (
                    SELECT * FROM '{export.as_posix()}'
                    WHERE platform = ? AND scale_factor = ? AND median_server_ms IS NOT NULL
                ),
                common AS (
                    SELECT query_id FROM d GROUP BY 1
                    HAVING count(DISTINCT compute_label)
                         = (SELECT count(DISTINCT compute_label) FROM d)
                )
                SELECT compute_label, count(*), sum(median_server_ms) / 1000,
                       sum(mean_cost_usd)
                FROM d JOIN common USING (query_id) GROUP BY 1
                """,
                [platform, scale],
            ).fetchall()
        }
        assert set(got) == set(expected), f"{platform} SF{scale}: sizes differ"
        for label, (n, suite_s, cost) in expected.items():
            g_n, g_s, g_c = got[label]
            assert g_n == n, f"{platform} SF{scale} {label}: {g_n} queries, report has {n}"
            assert g_s == pytest.approx(suite_s, rel=1e-9)
            assert (g_c or 0.0) == pytest.approx(cost, rel=1e-9)


def test_rate_card_prices_every_grid_size(tmp_path: Path) -> None:
    """Every priced concurrency cell's rate is the rate card's rate for that size.

    The explorer's price table reads the rate card while its charts read the
    grid's own `usd_per_hour`; the two must be one price list.
    """
    card = emit_rate_card(RESULTS_DB, tmp_path / "rate_card.parquet")
    grid = emit_concurrency_grid(RESULTS_DB, tmp_path / "grid.parquet")
    mismatched = (
        duckdb.connect()
        .execute(
            f"""
            SELECT DISTINCT g.platform, g.compute_label, g.usd_per_hour, c.usd_per_hour
            FROM '{grid.as_posix()}' g
            LEFT JOIN '{card.as_posix()}' c USING (platform, compute_label)
            WHERE g.usd_per_hour IS NOT NULL
              AND (c.usd_per_hour IS NULL OR abs(g.usd_per_hour - c.usd_per_hour) > 1e-3)
            """
        )
        .fetchall()
    )
    assert not mismatched, f"grid and rate card disagree: {mismatched}"


def test_query_catalog_lists_every_query_once(tmp_path: Path) -> None:
    """244 queries, one row each, and every SQL path resolves for every dialect."""
    catalog = emit_query_catalog(tmp_path / "query_catalog.parquet")
    rows = (
        duckdb.connect()
        .execute(f"SELECT query_id, sql_path FROM '{catalog.as_posix()}'")
        .fetchall()
    )
    assert len(rows) == 244
    assert len({q for q, _ in rows}) == 244
    for platform in (
        "duckdb",
        "snowflake",
        "databricks",
        "bigquery",
        "redshift",
        "fabric",
        "clickhouse",
        "motherduck",
    ):
        missing = [q for q, path in rows if not Path(path.format(platform=platform)).exists()]
        assert not missing, f"{platform}: no SQL file for {missing[:5]}"


def test_query_catalog_flags_the_report_returns_pairs(tmp_path: Path) -> None:
    """`returns_pair` is the report's skew split: both sides of each returns pair, nothing else."""
    catalog = emit_query_catalog(tmp_path / "query_catalog.parquet")
    flagged = (
        duckdb.connect()
        .execute(f"SELECT block, number FROM '{catalog.as_posix()}' WHERE returns_pair")
        .fetchall()
    )
    expected = {(block, n) for n in returns_qnums() for block in ("tpcds", "dsb")}
    assert set(flagged) == expected


def test_clickhouse_costs_follow_the_rate_card(con: duckdb.DuckDBPyConnection) -> None:
    """Every ClickHouse size's stored cost follows its list rate.

    ClickHouse cost is modeled from the size's hourly rate. A row stored at another
    size's rate (e.g. 1x120GB at $0.30/h against $4.48 list) passes every other
    check, so this one compares each size with its list rate. A query with the
    service to itself costs its engine time x rate; under shared load the service
    is billed once per window (``analysis.recost.recost_capacity_windows``), so
    those rows must equal the window price recomputed from the same rate card.
    """
    from benchmark.analysis.recost import (
        SHARED_RUN_TYPES,
        _engine_ms_sql,
        rate_window_pricing_sql,
    )
    from benchmark.report.compute_rates import effective_usd_per_hour

    shared = ", ".join(f"'{rt}'" for rt in SHARED_RUN_TYPES)
    rows = con.execute(
        f"""
        SELECT compute_label, sum(cost_usd) / (sum({_engine_ms_sql()}) / 3600000.0)
        FROM main.sql_benchmark_results r
        WHERE platform = 'clickhouse' AND error_message IS NULL AND cost_usd > 0
          AND run_type NOT IN ({shared})
        GROUP BY 1
        """
    ).fetchall()
    assert rows
    for label, implied in rows:
        assert implied == pytest.approx(effective_usd_per_hour("clickhouse", label), rel=1e-6), (
            label
        )

    labels = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT compute_label FROM main.sql_benchmark_results WHERE platform ="
            f" 'clickhouse' AND run_type IN ({shared}) AND compute_label IS NOT NULL"
        ).fetchall()
    ]
    rates = [
        ("clickhouse", label, effective_usd_per_hour("clickhouse", label) / 3_600_000.0)
        for label in labels
    ]
    priced = con.execute(
        f"""
        SELECT count(*), count(*) FILTER (WHERE abs(p.after_usd - r.cost_usd)
                                           > 1e-9 + 1e-6 * abs(p.after_usd))
        FROM ({rate_window_pricing_sql(rates)}) p
        JOIN main.sql_benchmark_results r USING (run_id)
        """,
        [None, None],
    ).fetchone()
    assert priced[0] > 0
    assert priced[1] == 0, f"{priced[1]} of {priced[0]} shared-load rows off the window price"

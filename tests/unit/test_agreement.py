"""Cross-engine row-count agreement.

This is the report's only correctness evidence above SF1, so the behaviours worth
pinning are the ones that would let a wrong answer look right: a divergence that
gets summarized away, a `LIMIT 100` cell counted as strong evidence, an engine
that contradicts itself being read as agreement, or ClickBench's vendor-authored
per-engine SQL leaking in and manufacturing a divergence that means nothing.
"""

from __future__ import annotations

import duckdb
import pytest

from benchmark.analysis.agreement import agreement_on

_COLS = (
    "platform VARCHAR, query_id VARCHAR, block VARCHAR, scale_factor INTEGER, "
    "run_type VARCHAR, run_number INTEGER, concurrency INTEGER, compute_label VARCHAR, "
    "row_count BIGINT, error_message VARCHAR, created_at TIMESTAMP"
)


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE main.sql_benchmark_results ({_COLS})")
    yield c
    c.close()


def _add(
    con,
    platform,
    query_id,
    row_count,
    *,
    block="tpcds",
    scale=100,
    run_type="warm",
    run_number=1,
    label="Medium",
    error=None,
    created_at="2026-08-01 00:00:00",
):
    con.execute(
        "INSERT INTO main.sql_benchmark_results VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        [
            platform,
            query_id,
            block,
            scale,
            run_type,
            run_number,
            label,
            row_count,
            error,
            created_at,
        ],
    )


def test_engines_agreeing_is_reported_as_unanimous(con):
    for plat in ("snowflake", "redshift", "bigquery"):
        _add(con, plat, "tpcds_q01", 42)

    report = agreement_on(con)

    assert report.total_cells == 1
    assert report.total_divergent == 0
    assert report.summary[0].unanimous == 1
    assert report.summary[0].engines_min == 3
    assert report.summary[0].engines_max == 3


def test_a_divergence_is_listed_in_full_not_just_counted(con):
    _add(con, "snowflake", "tpcds_q01", 42)
    _add(con, "redshift", "tpcds_q01", 42)
    _add(con, "bigquery", "tpcds_q01", 41)

    report = agreement_on(con)

    assert report.total_divergent == 1
    assert len(report.divergences) == 1, "a divergence must survive as a row, not a count"
    div = report.divergences[0]
    assert div.query_id == "tpcds_q01"
    # every engine's count is carried, so a reader can see WHICH engine is the
    # outlier without going back to the database
    assert {(e.platform, e.row_count) for e in div.engines} == {
        ("snowflake", 42),
        ("redshift", 42),
        ("bigquery", 41),
    }
    assert "bigquery=41" in div.describe()


def test_agreement_on_the_limit_cap_is_not_counted_as_discriminating(con):
    """Most TPC-DS templates end in LIMIT 100. Agreeing on 100 proves almost
    nothing, and conflating it with real agreement would overstate the evidence."""
    for plat in ("snowflake", "redshift"):
        _add(con, plat, "tpcds_q01", 100)
        _add(con, plat, "tpcds_q02", 7)

    report = agreement_on(con)

    assert report.total_cells == 2
    assert report.summary[0].unanimous == 2
    assert report.summary[0].discriminating == 1, "only the non-capped cell informs"


def test_an_engine_disagreeing_with_itself_across_sizes_is_flagged(con):
    """Row count must not depend on warehouse size. If it does, that engine is
    unstable on that query and the cell's agreement means less."""
    _add(con, "snowflake", "tpcds_q01", 42, label="Small")
    _add(con, "snowflake", "tpcds_q01", 43, label="Large")
    _add(con, "redshift", "tpcds_q01", 42)

    report = agreement_on(con)

    assert report.summary[0].unstable_cells == 1


def test_clickbench_is_excluded_because_engines_run_different_sql(con):
    """ClickBench uses each vendor's own query text, so a row-count difference
    there is not a correctness signal — admitting it would fabricate findings."""
    _add(con, "snowflake", "clickbench_q01", 10, block="clickbench")
    _add(con, "redshift", "clickbench_q01", 99, block="clickbench")

    report = agreement_on(con)

    assert report.total_cells == 0, "ClickBench must not reach the agreement table"
    assert report.divergences == []


def test_error_rows_and_concurrency_rows_do_not_participate(con):
    """An errored row has no result to compare, and concurrency rows exist to
    measure queuing — its shed errors would mix completion into correctness."""
    _add(con, "snowflake", "tpcds_q01", 42)
    _add(con, "redshift", "tpcds_q01", 42)
    _add(con, "bigquery", "tpcds_q01", None, error="OOM")
    con.execute(
        "INSERT INTO main.sql_benchmark_results VALUES "
        "('databricks','tpcds_q01','tpcds',100,'concurrency',1,10,'Medium',9999,NULL,"
        "'2026-08-01 00:00:00')"
    )

    report = agreement_on(con)

    assert report.total_divergent == 0
    assert report.summary[0].engines_max == 2


def test_a_rerun_supersedes_rather_than_double_counting(con):
    """Results are append-only: a re-run of the same key inserts a new row. The
    newer row must win, or a corrected result would show up as a divergence
    against its own earlier self."""
    _add(con, "snowflake", "tpcds_q01", 41, created_at="2026-08-01 00:00:00")
    _add(con, "snowflake", "tpcds_q01", 42, created_at="2026-08-02 00:00:00")
    _add(con, "redshift", "tpcds_q01", 42)

    report = agreement_on(con)

    assert report.total_divergent == 0
    assert report.summary[0].unstable_cells == 0


def test_missing_engines_are_reported_so_thin_agreement_is_visible(con):
    """Unanimity among two engines is weaker than among three, and the absences
    are load-correlated — so they are published, not left implicit."""
    for plat in ("snowflake", "redshift", "clickhouse"):
        _add(con, plat, "tpcds_q01", 42)
    _add(con, "snowflake", "tpcds_q02", 7)
    _add(con, "redshift", "tpcds_q02", 7)

    report = agreement_on(con)

    assert [(m.platform, m.missing_cells) for m in report.missing] == [("clickhouse", 1)]


def test_a_single_engine_cell_is_not_called_agreement(con):
    """One engine agreeing with itself is not cross-engine evidence."""
    _add(con, "snowflake", "tpcds_q01", 42)

    report = agreement_on(con)

    assert report.total_cells == 0

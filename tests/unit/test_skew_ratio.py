"""The skew step's slice discipline (Part III, `skew_ratio_by_engine`).

A report figure goes wrong mostly by selecting the wrong slice. The skew table has
three slices to get wrong (the returns/clean split, the designated compute tier,
and latest-wins dedup), so each is pinned here against a fixture where getting it
wrong changes the answer.
"""

from __future__ import annotations

import duckdb
import pytest

from benchmark.report.verifiers import (
    _median,
    returns_qnums,
    skew_pair_ratios,
    skew_ratio_by_engine,
)

#: Snowflake's run-plan designated warm tier at 100 GB, as the runners stamp it.
DESIGNATED = "Medium (Gen2)"


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE main.sql_benchmark_results (
            platform VARCHAR, query_id VARCHAR, block VARCHAR, scale_factor INTEGER,
            run_type VARCHAR, run_number INTEGER, concurrency INTEGER,
            compute_label VARCHAR, server_time_ms DOUBLE, error_message VARCHAR,
            created_at TIMESTAMP
        )
    """)
    yield c
    c.close()


def _row(
    con,
    query_id: str,
    block: str,
    ms: float,
    *,
    label: str = DESIGNATED,
    run_number: int = 1,
    created: str = "2026-08-01 00:00:00",
):
    con.execute(
        "INSERT INTO main.sql_benchmark_results VALUES "
        "('snowflake', ?, ?, 100, 'warm', ?, NULL, ?, ?, NULL, ?)",
        [query_id, block, run_number, label, ms, created],
    )


def test_returns_classification_derives_25_queries_from_the_suite():
    """The split is derived from the query SQL, not a hand-kept list. TPC-DS is
    documented as having exactly 25 q1-99 queries touching a *_returns table."""
    qnums = returns_qnums()

    assert len(qnums) == 25
    assert {1, 5, 16, 94, 95} <= qnums, "known returns-touching queries must classify"
    assert {3, 7, 42}.isdisjoint(qnums), "known clean queries must not"


def test_clean_and_returns_pairs_report_in_separate_columns(con):
    _row(con, "tpcds_q03", "tpcds", 100.0)  # clean pair, ratio 0.50
    _row(con, "dsb_q03", "dsb", 50.0)
    _row(con, "tpcds_q01", "tpcds", 100.0)  # returns pair, ratio 1.20
    _row(con, "dsb_q01", "dsb", 120.0)

    (row,) = skew_ratio_by_engine(con)

    assert row == ["Snowflake", "0.50", "1", "1.20", "1"]


def test_only_the_designated_tier_contributes(con):
    """A sweep row at another tier must not leak into the ratio — the wrong-tier
    slice is the exact defect class the gate exists for."""
    _row(con, "tpcds_q03", "tpcds", 100.0)
    _row(con, "dsb_q03", "dsb", 50.0)
    _row(con, "tpcds_q03", "tpcds", 1.0, label="X-Large (Gen2)")
    _row(con, "dsb_q03", "dsb", 9999.0, label="X-Large (Gen2)")

    assert [(p, q, round(r, 2)) for p, q, r in skew_pair_ratios(con)] == [("snowflake", 3, 0.5)]


def test_multi_replica_clickhouse_is_excluded_by_the_comparison_policy(con):
    """A two-replica ClickHouse cell (2x32GB) stays out of the single-session skew
    comparison: replicas do not pool memory for one query, so the policy drops it."""
    con.execute(
        "INSERT INTO main.sql_benchmark_results VALUES "
        "('clickhouse', 'tpcds_q03', 'tpcds', 100, 'warm', 1, NULL, '2x32GB', 100.0, "
        "NULL, '2026-08-01 00:00:00'),"
        "('clickhouse', 'dsb_q03', 'dsb', 100, 'warm', 1, NULL, '2x32GB', 50.0, "
        "NULL, '2026-08-01 00:00:00')"
    )

    assert skew_pair_ratios(con) == []


def test_latest_run_wins_within_the_designated_tier(con):
    """Re-runs append; a superseded measurement must not shift the median."""
    _row(con, "tpcds_q03", "tpcds", 1000.0, created="2026-07-01 00:00:00")  # superseded
    _row(con, "tpcds_q03", "tpcds", 100.0, created="2026-08-02 00:00:00")
    _row(con, "dsb_q03", "dsb", 50.0)

    assert [round(r, 2) for _p, _q, r in skew_pair_ratios(con)] == [0.5]


def test_a_pair_needs_both_sides(con):
    """A query completed in only one suite has no ratio — it must drop out rather
    than divide by a missing side."""
    _row(con, "tpcds_q03", "tpcds", 100.0)  # no dsb_q03
    _row(con, "dsb_q07", "dsb", 80.0)  # no tpcds_q07

    assert skew_pair_ratios(con) == []


def test_median_is_a_rank_statistic_not_a_mean():
    assert _median([1.0, 100.0, 2.0]) == 2.0
    assert _median([1.0, 3.0]) == 2.0

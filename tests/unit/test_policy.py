"""Comparison-admission policy (report/policy.py) and its wiring into the views.

The policy exists so that real-but-unfair measurements — survivor statistics from a
DNF cell, multi-replica ClickHouse rows on a vertical-sizing axis — cannot reach a
cross-engine comparison, while still reaching Coverage. Each test pins one way the
policy could silently stop holding.
"""

from __future__ import annotations

import duckdb
import pytest

from benchmark.report.generator import _create_dedup_view
from benchmark.report.policy import (
    DNF_MAX_FAILURE_SHARE,
    dnf_cells,
    single_session_replica_predicate,
)

COLUMNS = (
    "platform VARCHAR, query_id VARCHAR, block VARCHAR, scale_factor INTEGER, "
    "run_type VARCHAR, run_number INTEGER, concurrency INTEGER, "
    "compute_label VARCHAR, server_time_ms DOUBLE, error_message VARCHAR, "
    "created_at TIMESTAMP DEFAULT now()"
)


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE main.sql_benchmark_results ({COLUMNS})")
    yield c
    c.close()


def _row(
    con,
    platform: str,
    query_id: str,
    *,
    block: str = "tpcds",
    scale: int = 1000,
    run_type: str = "warm",
    label: str | None = "3x120GB",
    error: str | None = None,
):
    con.execute(
        "INSERT INTO main.sql_benchmark_results "
        "(platform, query_id, block, scale_factor, run_type, run_number, "
        " compute_label, server_time_ms, error_message) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, 100.0, ?)",
        [platform, query_id, block, scale, run_type, label, error],
    )


def test_dnf_flags_an_engine_failing_more_than_ten_percent(con):
    # suite of 10 distinct tpcds queries; engine A completes 8 of 10 at SF1000
    for i in range(10):
        _row(con, "other", f"tpcds_q{i:02d}", scale=100, label="X")
    for i in range(8):
        _row(con, "a", f"tpcds_q{i:02d}")
    _row(con, "a", "tpcds_q08", error="boom")

    assert dnf_cells(con) == [("a", "tpcds", 1000, "3x120GB", 8, 10)]


def test_exactly_ten_percent_failed_is_not_dnf(con):
    """The rule is 'fail MORE than 10%': the boundary passes."""
    for i in range(10):
        _row(con, "other", f"tpcds_q{i:02d}", scale=100, label="X")
    for i in range(9):
        _row(con, "a", f"tpcds_q{i:02d}")
    _row(con, "a", "tpcds_q09", error="boom")

    assert dnf_cells(con) == []
    assert 0 < DNF_MAX_FAILURE_SHARE < 1, "the floor is a share, not a count"


def test_dnf_is_judged_per_suite_not_per_family(con):
    """Failing one benchmark set must not disqualify the engine's clean results on
    another — the rule binds any suite, present or future, independently."""
    for i in range(10):
        _row(con, "other", f"tpcds_q{i:02d}", scale=100, label="X")
        _row(con, "other", f"dsb_q{i:02d}", block="dsb", scale=100, label="X")
    for i in range(10):
        _row(con, "a", f"tpcds_q{i:02d}", scale=100, label="1x32GB")  # clean tpcds
        _row(
            con,
            "a",
            f"dsb_q{i:02d}",
            block="dsb",
            scale=100,
            label="1x32GB",
            error=None if i < 8 else "boom",
        )  # 20% of dsb failed

    assert dnf_cells(con) == [("a", "dsb", 100, "1x32GB", 8, 10)]
    _create_dedup_view(con)
    blocks = {
        r[0]
        for r in con.execute("SELECT DISTINCT block FROM bench WHERE platform = 'a'").fetchall()
    }
    assert blocks == {"tpcds"}, "only the failed suite's rows leave the comparison"


def test_single_scale_blocks_are_judged_pooled_across_scale_tags(con):
    """ClickBench's scale tag records which campaign a run rode along with, not a
    data volume — DNF for it must judge the one dataset, not per-tag slices."""
    for i in range(10):
        # 10-query suite; each query measured under a different campaign's tag,
        # 8 clean and 2 failed. Per-tag slices would each be 100% failed or 100%
        # clean; the pooled verdict is 20% failed = DNF.
        _row(
            con,
            "a",
            f"clickbench_q{i:02d}",
            block="clickbench",
            scale=1 if i % 2 else 100,
            label="1x32GB",
            error=None if i < 8 else "boom",
        )

    assert dnf_cells(con) == [("a", "clickbench", None, "1x32GB", 8, 10)]


def test_dnf_is_judged_at_the_largest_measured_compute_size(con):
    """A small tier that cannot finish is a sizing result, not an engine verdict:
    when the biggest measured size clears the floor, the cell is not DNF."""
    for i in range(10):
        _row(con, "other", f"tpcds_q{i:02d}", scale=100, label="X")
    for i in range(10):
        # X-Small fails half the suite; Medium completes everything.
        _row(
            con,
            "a",
            f"tpcds_q{i:02d}",
            scale=100,
            label="X-Small (Gen2)",
            error=None if i < 5 else "oom",
        )
        _row(con, "a", f"tpcds_q{i:02d}", scale=100, label="Medium (Gen2)")

    assert dnf_cells(con) == []


def test_dnf_when_the_largest_size_fails_even_if_a_smaller_one_passed(con):
    for i in range(10):
        _row(con, "other", f"tpcds_q{i:02d}", scale=100, label="X")
    for i in range(10):
        _row(con, "a", f"tpcds_q{i:02d}", scale=100, label="X-Small (Gen2)")
        _row(
            con,
            "a",
            f"tpcds_q{i:02d}",
            scale=100,
            label="Medium (Gen2)",
            error=None if i < 5 else "oom",
        )

    assert dnf_cells(con) == [("a", "tpcds", 100, "Medium (Gen2)", 5, 10)]


def test_bench_excludes_dnf_warm_rows_but_coverage_keeps_them(con):
    for q in ("tpcds_q01", "tpcds_q02", "tpcds_q03", "tpcds_q04"):
        _row(con, "other", q, scale=100, label="X")
    _row(con, "a", "tpcds_q01")
    _row(con, "a", "tpcds_q02", error="boom")
    _create_dedup_view(con)

    in_bench = con.execute("SELECT count(*) FROM bench WHERE platform = 'a'").fetchone()[0]
    in_coverage = con.execute(
        "SELECT count(*) FROM bench_coverage WHERE platform = 'a'"
    ).fetchone()[0]

    assert in_bench == 0, "DNF warm rows must not reach comparisons"
    assert in_coverage == 2, "coverage must still show the measurement happened"


def test_clickhouse_multi_replica_warm_is_excluded_but_concurrency_kept(con):
    """Multi-replica warm rows fail the single-session replica policy, while the
    3-replica concurrency row survives: replicas are that axis's knob. (The lone 1x
    warm row drops too: with one query per size, the SF100 cell is DNF.)"""
    _row(con, "clickhouse", "tpcds_q01", scale=100, label="2x32GB")
    _row(con, "clickhouse", "tpcds_q02", scale=100, label="1x32GB")
    _row(con, "clickhouse", "tpcds_q03", scale=100, label="3x32GB", run_type="concurrency")
    _create_dedup_view(con)

    labels = {
        r[0]
        for r in con.execute(
            "SELECT compute_label FROM bench WHERE platform = 'clickhouse'"
        ).fetchall()
    }

    assert labels == {"3x32GB"}, (
        "only the 3-replica concurrency row is admissible: multi-replica warm rows "
        "fail the replica policy and the lone 1x warm row is a DNF cell"
    )


def test_other_platforms_pass_the_replica_predicate_untouched(con):
    _row(con, "snowflake", "tpcds_q01", scale=100, label="Medium (Gen2)")
    _create_dedup_view(con)

    assert con.execute("SELECT count(*) FROM bench").fetchone()[0] == 1
    predicate = single_session_replica_predicate("r")
    assert "clickhouse" in predicate and "1x" in predicate


def test_clickbench_view_applies_the_replica_policy_to_warm_only(con):
    # Both queries complete warm at both sizes (so the DNF floor is not in play —
    # the verdict size is the largest measured, 2x32GB); the 2x32GB warm rows must
    # drop, the 3x64GB concurrency row must stay.
    _row(con, "clickhouse", "clickbench_q01", block="clickbench", scale=100, label="2x32GB")
    _row(con, "clickhouse", "clickbench_q02", block="clickbench", scale=100, label="2x32GB")
    _row(con, "clickhouse", "clickbench_q01", block="clickbench", scale=100, label="1x32GB")
    _row(con, "clickhouse", "clickbench_q02", block="clickbench", scale=100, label="1x32GB")
    _row(
        con,
        "clickhouse",
        "clickbench_q02",
        block="clickbench",
        scale=100,
        label="3x64GB",
        run_type="concurrency",
    )
    _create_dedup_view(con)

    labels = {r[0] for r in con.execute("SELECT compute_label FROM bench_cb").fetchall()}

    assert labels == {"1x32GB", "3x64GB"}

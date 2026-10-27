"""The coverage audit checks every engine's full ladder, and skips match on every key.

Gap kinds pinned here: sizes missing at every scale, sizes never run under load,
ClickBench levels cut short. A skip matches on its suite too, so a DSB-only skip
(ClickHouse at 1 TB) does not hide TPC-DS gaps there.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from benchmark.analysis.coverage import (
    KINDS,
    Skip,
    _ejected_cells,
    _is_skipped,
    audit,
)
from benchmark.analysis.scale_skips import emit_scale_skips
from benchmark.results import ResultsDB
from benchmark.run_plan import RunPlan


def _row(db, platform, scale, label, *, block="tpcds", qid="tpcds_q01", run_type="warm", **kw):
    db.record_sql_result(
        platform=platform,
        query_id=qid,
        block=block,
        scale_factor=scale,
        run_type=run_type,
        run_number=kw.pop("run_number", 1),
        wall_clock_ms=10.0,
        server_time_ms=5.0,
        row_count=1,
        cost_usd=0.1,
        cost_confidence="estimated",
        error_message=kw.pop("error_message", None),
        compute_label=label,
        **kw,
    )


def _gaps(db, *, clickbench=False, **match):
    """The audit's gaps matching ``match``; ClickBench's only when asked for."""
    with db.connect() as con:
        gaps = audit(con)
    return [
        g
        for g in gaps
        if all(getattr(g, k) == v for k, v in match.items())
        and (g.block == "clickbench") == clickbench
    ]


def test_a_skip_matches_on_every_key_it_carries():
    skips = {
        Skip("clickhouse", 1000, "dsb", "*", "*"),
        Skip("redshift", 1000, "*", "8 RPU", "warm"),
    }
    assert _is_skipped(skips, "clickhouse", 1000, "3x120GB", block="dsb", run_type="warm")
    # A DSB-only skip says nothing about TPC-DS, or about a pooled (every-suite) cell.
    assert not _is_skipped(skips, "clickhouse", 1000, "3x120GB", block="tpcds", run_type="warm")
    assert not _is_skipped(skips, "clickhouse", 1000, "3x120GB", run_type="warm")
    # A single-user skip says nothing about the same size under load.
    assert _is_skipped(skips, "redshift", 1000, "8 RPU", block="tpcds", run_type="warm")
    assert not _is_skipped(skips, "redshift", 1000, "8 RPU", block="tpcds", run_type="concurrency")
    assert not _is_skipped(skips, "redshift", 100, "8 RPU", block="tpcds", run_type="warm")
    # ClickBench (any scale tag) is covered only by a skip recorded for ClickBench: an
    # every-suite skip at 1 TB is about that data volume.
    load_1tb = {Skip("clickhouse", 1000, "*", "*", "concurrency")}
    assert not _is_skipped(
        load_1tb, "clickhouse", None, "3x24GB", block="clickbench", run_type="concurrency"
    )
    cb = {Skip("clickhouse", 1, "clickbench", "3x24GB", "*")}
    assert _is_skipped(cb, "clickhouse", None, "3x24GB", block="clickbench", run_type="concurrency")


def test_a_size_never_run_under_load_is_a_load_hole(results_db):
    """MotherDuck Pulse at 100 GB ran single-user and never under load."""
    for block, qid in (("tpcds", "tpcds_q01"), ("dsb", "dsb_q01")):
        _row(results_db, "motherduck", 100, "Pulse", block=block, qid=qid)
    assert not _gaps(
        results_db,
        kind="sweep-hole",
        platform="motherduck",
        scale_factor=100,
        compute_label="Pulse",
    )
    holes = _gaps(
        results_db, kind="load-hole", platform="motherduck", scale_factor=100, compute_label="Pulse"
    )
    assert [g.block for g in holes] == ["tpcds,dsb"]
    assert holes[0].fill_command.startswith("uv run benchmark concurrency --scale 100")


def test_a_single_user_skip_leaves_the_load_hole(results_db):
    results_db.record_scale_skip(
        1000, "redshift", "cannot finish", compute_label="8 RPU", run_type="warm"
    )
    assert not _gaps(
        results_db, kind="sweep-hole", platform="redshift", scale_factor=1000, compute_label="8 RPU"
    )
    assert _gaps(
        results_db, kind="load-hole", platform="redshift", scale_factor=1000, compute_label="8 RPU"
    )


def test_a_dsb_skip_does_not_hide_tpcds(results_db):
    """A skip row must not hide the whole (platform, scale, size), only its suite."""
    results_db.record_scale_skip(1000, "clickhouse", "DSB skipped", block="dsb")
    holes = _gaps(
        results_db,
        kind="load-hole",
        platform="clickhouse",
        scale_factor=1000,
        compute_label="3x8GB",
    )
    assert [g.block for g in holes] == ["tpcds"]


def test_clickhouse_load_ladder_is_three_replicas_and_warm_is_one():
    plan = RunPlan.load()
    assert plan.sweep_ladder("clickhouse")[0] == "1x8GB"
    assert plan.load_ladder("clickhouse")[0] == "3x8GB"
    assert plan.load_ladder("fabric") == plan.sweep_ladder("fabric")


def test_bigquery_rungs_are_reservations_at_every_scale(results_db):
    """At 1 and 10 GB BigQuery's designated basis is on-demand; a rung is a reservation."""
    holes = _gaps(results_db, kind="sweep-hole", platform="bigquery", scale_factor=1)
    assert "ENTERPRISE / 100 slots" in {g.compute_label for g in holes}
    assert "on-demand" not in {g.compute_label for g in holes}


def test_clickbench_rows_count_under_any_scale_tag(results_db):
    """ClickBench is one fixed dataset: rows tagged 1 or 100 cover the size."""
    _row(results_db, "fabric", 1, "F2", block="clickbench", qid="clickbench_q01")
    cb = _gaps(results_db, kind="sweep-hole", platform="fabric", clickbench=True)
    assert "F2" not in {g.compute_label for g in cb}
    assert "F4" in {g.compute_label for g in cb}


def test_a_level_cut_short_is_reported_and_clickbench_tags_pool(results_db):
    # The suite: two warm TPC-DS queries and two ClickBench queries.
    for qid in ("tpcds_q01", "tpcds_q02"):
        _row(results_db, "clickhouse", 1, "1x16GB", qid=qid)
    for qid in ("clickbench_q01", "clickbench_q02"):
        _row(results_db, "clickhouse", 100, "1x16GB", block="clickbench", qid=qid)
    # A 10-session TPC-DS level that recorded one of two queries.
    _row(
        results_db,
        "clickhouse",
        1,
        "3x16GB",
        qid="tpcds_q01",
        run_type="concurrency",
        concurrency=10,
    )
    # ClickBench at one session: cut short under tag 1, finished under tag 100.
    _row(
        results_db,
        "clickhouse",
        1,
        "3x16GB",
        block="clickbench",
        qid="clickbench_q01",
        run_type="concurrency",
        concurrency=1,
    )
    for qid in ("clickbench_q01", "clickbench_q02"):
        _row(
            results_db,
            "clickhouse",
            100,
            "3x16GB",
            block="clickbench",
            qid=qid,
            run_type="concurrency",
            concurrency=1,
        )
    # An off-protocol level is not a gap.
    _row(
        results_db,
        "clickhouse",
        100,
        "3x16GB",
        block="clickbench",
        qid="clickbench_q01",
        run_type="concurrency",
        concurrency=40,
    )
    short = _gaps(results_db, kind="short-level", platform="clickhouse")
    assert [(g.scale_factor, g.block) for g in short] == [(1, "tpcds")]
    assert "1 of 2 queries" in short[0].detail


def test_a_clickbench_size_that_ran_one_level_is_a_level_gap(results_db):
    """ClickHouse 3x24GB recorded one ClickBench query at one session and nothing else."""
    _row(
        results_db,
        "clickhouse",
        1,
        "3x24GB",
        block="clickbench",
        qid="clickbench_q01",
        run_type="concurrency",
        concurrency=1,
    )
    for lvl in (1, 5, 10, 20, 50, 100):
        _row(
            results_db,
            "clickhouse",
            1,
            "3x8GB",
            block="clickbench",
            qid="clickbench_q01",
            run_type="concurrency",
            concurrency=lvl,
        )
    gaps = _gaps(results_db, kind="level-gap", platform="clickhouse", clickbench=True)
    assert [(g.compute_label, g.detail) for g in gaps] == [
        ("3x24GB", "ClickBench ladder missing level(s) [5, 10, 20, 50, 100]")
    ]


def test_a_single_user_pass_cut_short_is_reported(results_db):
    for qid in ("tpcds_q01", "tpcds_q02"):
        _row(results_db, "snowflake", 1, "X-Small (Gen2)", qid=qid)
    _row(results_db, "snowflake", 100, "Medium (Gen2)", qid="tpcds_q01")
    short = _gaps(results_db, kind="short-pass", platform="snowflake")
    assert [(g.scale_factor, g.compute_label) for g in short] == [(100, "Medium (Gen2)")]


def test_a_ladder_that_stopped_after_timeouts_was_ejected(results_db):
    """The sweeps eject a band after any level with a timeout; the levels above are
    unrun by rule, not by oversight. A ladder that simply stopped is not ejected."""
    for lvl in (1, 5):
        _row(results_db, "redshift", 1000, "8 RPU", run_type="concurrency", concurrency=lvl)
    _row(
        results_db,
        "redshift",
        1000,
        "8 RPU",
        run_type="concurrency",
        concurrency=5,
        qid="tpcds_q02",
        error_message="query exceeded timeout of 600s",
    )
    for lvl in (1, 5):
        _row(results_db, "redshift", 1000, "32 RPU", run_type="concurrency", concurrency=lvl)
    with results_db.connect() as con:
        assert _ejected_cells(con) == {("redshift", 1000, "8 RPU")}


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_carries_a_runnable_fill_command(results_db, kind):
    _row(results_db, "snowflake", 1, "X-Small (Gen2)")
    for g in _gaps(results_db, kind=kind):
        assert g.fill_command.startswith("uv run benchmark"), g
        assert g.platform in g.fill_command


def test_the_migration_adds_run_type_and_keys_on_it(tmp_path):
    """Databases without the column migrate in place; existing rows mean every run type."""
    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE scale_skips (
            scale_factor INTEGER NOT NULL, platform VARCHAR NOT NULL,
            block VARCHAR NOT NULL DEFAULT '*', compute_label VARCHAR NOT NULL DEFAULT '*',
            reason VARCHAR NOT NULL, skipped_by VARCHAR, skipped_at TIMESTAMP DEFAULT now(),
            PRIMARY KEY (scale_factor, platform, block, compute_label))
    """)
    con.execute("INSERT INTO scale_skips VALUES (1000, 'redshift', '*', '8 RPU', 'x', 'op', now())")
    con.close()

    db = ResultsDB(path)
    db.record_scale_skip(1000, "redshift", "warm only", compute_label="8 RPU", run_type="warm")
    with db.connect() as c:
        rows = c.execute("SELECT run_type, reason FROM scale_skips ORDER BY run_type").fetchall()
    assert rows == [("*", "x"), ("warm", "warm only")]
    ResultsDB(path)  # and a no-op the second time
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM scale_skips").fetchone() == (2,)


def test_the_export_carries_every_key(results_db, tmp_path):
    results_db.record_scale_skip(1000, "fabric", "too small", compute_label="F2")
    results_db.record_scale_skip(1000, "clickhouse", "no load", run_type="concurrency")
    out = emit_scale_skips(Path(results_db.path), tmp_path / "scale_skips.parquet")
    rows = duckdb.sql(
        f"SELECT platform, compute_label, run_type, reason FROM '{out}' ORDER BY platform"
    ).fetchall()
    assert rows == [
        ("clickhouse", "*", "concurrency", "no load"),
        ("fabric", "F2", "*", "too small"),
    ]


def test_the_report_reads_a_load_only_skip_as_no_reason_to_thin_a_warm_cell(results_db):
    from benchmark.report.generator import _skipped_cells

    results_db.record_scale_skip(1000, "clickhouse", "no load", run_type="concurrency")
    results_db.record_scale_skip(1000, "clickhouse", "no DSB", block="dsb")
    with results_db.connect() as con:
        con.execute("CREATE VIEW bench AS SELECT 'dsb' AS block UNION ALL SELECT 'tpcds'")
        assert _skipped_cells(con) == {("clickhouse", "dsb", 1000)}

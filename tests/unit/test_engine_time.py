"""Engine time: one definition, counted the same way on every run type.

Databricks reports execution without compilation; Databricks, Redshift and BigQuery
report execution without the wait in their own queue; Snowflake, Fabric, MotherDuck and
ClickHouse report total elapsed, both included. ``analysis.engine_time`` defines the
engine's time once. The shared views carry it as ``server_time_ms`` for warm and
concurrency rows alike (NULL where the queue time was not captured), ``engine_ms``
must not add either part again, and cost reads the same definition with an uncaptured
wait counted as zero.
"""

from __future__ import annotations

import time

import duckdb
import pytest

from benchmark.analysis.engine_time import (
    COMPILE_EXCLUDED_ENGINES,
    EXECUTION_ONLY_ENGINES,
    engine_time_sql,
    execution_sql,
)
from benchmark.analysis.recost import _engine_ms_sql
from benchmark.report.views import install_views


def _record(db, **kw) -> None:
    base = dict(
        query_id="tpcds_q01",
        block="tpcds",
        scale_factor=1,
        run_type="warm",
        run_number=1,
        wall_clock_ms=2000.0,
        server_time_ms=1000.0,
        row_count=1,
        cost_usd=0.01,
        cost_confidence="proportional",
        error_message=None,
        compute_label="X-Small",
    )
    base.update(kw)
    db.record_sql_result(**base)


@pytest.fixture
def con(results_db):
    _record(results_db, platform="databricks", compile_ms=300.0, queue_ms=0.0)
    _record(results_db, platform="databricks", query_id="tpcds_q02", compile_ms=None, queue_ms=0.0)
    _record(results_db, platform="databricks", query_id="tpcds_q03", compile_ms=300.0)
    _record(results_db, platform="bigquery", queue_ms=70.0)
    _record(results_db, platform="bigquery", query_id="tpcds_q02")
    _record(results_db, platform="snowflake", compile_ms=300.0)
    _record(
        results_db,
        platform="databricks",
        run_type="concurrency",
        concurrency=10,
        compile_ms=300.0,
        queue_ms=500.0,
    )
    _record(
        results_db,
        platform="bigquery",
        run_type="concurrency",
        concurrency=10,
        queue_ms=500.0,
    )
    c = duckdb.connect(str(results_db.path), read_only=True)
    install_views(c)
    yield c
    c.close()


def _warm(con, platform: str) -> dict[str, float | None]:
    return dict(
        con.execute(
            "SELECT query_id, server_time_ms FROM results_latest"
            " WHERE platform = ? AND run_type = 'warm'",
            [platform],
        ).fetchall()
    )


def test_databricks_warm_server_time_includes_compile_and_queue(con):
    got = _warm(con, "databricks")
    # missing compile counts as 0; a missing queue leaves the engine's time unknown
    assert got == {"tpcds_q01": 1300.0, "tpcds_q02": 1000.0, "tpcds_q03": None}


def test_warm_counts_the_queue_wait_like_concurrency(con):
    assert _warm(con, "bigquery") == {"tpcds_q01": 1070.0, "tpcds_q02": None}
    (ms,) = con.execute(
        "SELECT engine_ms FROM concurrency_results WHERE platform = 'bigquery'"
    ).fetchone()
    assert ms == 1000.0 + 500.0


def test_total_elapsed_engines_are_left_alone(con):
    (ms,) = con.execute(
        "SELECT server_time_ms FROM results_latest WHERE platform = 'snowflake'"
    ).fetchone()
    assert ms == 1000.0


def test_the_raw_table_keeps_the_engines_own_figure(con):
    (ms,) = con.execute(
        "SELECT server_time_ms FROM main.sql_benchmark_results"
        " WHERE platform = 'databricks' AND query_id = 'tpcds_q01' AND run_type = 'warm'"
    ).fetchone()
    assert ms == 1000.0


def test_engine_ms_counts_compile_and_queue_once(con):
    (ms,) = con.execute(
        "SELECT engine_ms FROM concurrency_results WHERE platform = 'databricks'"
    ).fetchone()
    assert ms == 1000.0 + 300.0 + 500.0


def test_bench_views_carry_the_same_column(con):
    for view in ("bench", "bench_coverage"):
        (ms,) = con.execute(
            f"SELECT server_time_ms FROM {view} WHERE platform = 'databricks'"
            " AND query_id = 'tpcds_q01' AND run_type = 'warm'"
        ).fetchone()
        assert ms == 1300.0, view


def test_cost_counts_an_uncaptured_wait_as_zero(con):
    got = dict(
        con.execute(
            f"SELECT query_id, {_engine_ms_sql('r')} FROM main.sql_benchmark_results r"
            " WHERE platform = 'bigquery' AND run_type = 'warm'"
        ).fetchall()
    )
    assert got == {"tpcds_q01": 1070.0, "tpcds_q02": 1000.0}


def test_latency_and_cost_share_one_definition():
    assert execution_sql("r") in engine_time_sql("r")
    assert execution_sql("r") in engine_time_sql("r", missing_wait="zero")
    assert "compile_ms" in execution_sql("r") and "r.platform = 'databricks'" in execution_sql("r")


def test_compile_is_not_also_an_engine_time_extra():
    for platform, column in COMPILE_EXCLUDED_ENGINES.items():
        assert column not in EXECUTION_ONLY_ENGINES.get(platform, ())


def test_absent_columns_are_left_out():
    """A minimal database without the profiling columns still gets a usable expression."""
    sql = engine_time_sql(present={"server_time_ms", "platform"})
    assert "compile_ms" not in sql and "queue_ms" not in sql


def test_redshift_single_user_wait_counts_as_zero_but_not_under_load(results_db):
    """Redshift's single-session wait measured 0 ms (SINGLE_SESSION_ZERO_WAIT), and some
    of its warm rows carry no captured wait. Under concurrency it is never assumed."""
    _record(results_db, platform="redshift")
    _record(results_db, platform="redshift", run_type="concurrency", concurrency=1)
    c = duckdb.connect(str(results_db.path), read_only=True)
    install_views(c)
    got = dict(
        c.execute(
            "SELECT run_type, server_time_ms FROM results_latest WHERE platform = 'redshift'"
        ).fetchall()
    )
    c.close()
    assert got == {"warm": 1000.0, "concurrency": None}


def test_a_set_aside_row_leaves_the_row_it_would_supersede_published(results_db):
    from benchmark.results import SET_ASIDE_KEY

    _record(results_db, platform="snowflake", server_time_ms=1000.0)
    time.sleep(0.01)
    _record(
        results_db,
        platform="snowflake",
        server_time_ms=2000.0,
        metadata={SET_ASIDE_KEY: "check run"},
    )
    c = duckdb.connect(str(results_db.path), read_only=True)
    install_views(c)
    for view in ("results_latest", "bench", "bench_coverage"):
        (ms,) = c.execute(
            f"SELECT server_time_ms FROM {view} WHERE platform = 'snowflake'"
        ).fetchone()
        assert ms == 1000.0, view
    (n,) = c.execute(
        "SELECT count(*) FROM main.sql_benchmark_results WHERE platform = 'snowflake'"
    ).fetchone()
    c.close()
    assert n == 2  # nothing deleted

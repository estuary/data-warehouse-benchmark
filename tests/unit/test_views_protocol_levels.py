"""Burst rows off the protocol ladder stay out of every shared view.

The results database holds ClickBench rows at a 40-session level outside the
protocol ladder (1, 5, 10, 20, 50, 100). The rows stay in the database; the views
every published figure reads leave them out.
"""

from __future__ import annotations

import duckdb
import pytest

from benchmark.analysis.concurrency_shape import PROTOCOL_LEVELS
from benchmark.report.views import install_views


@pytest.fixture
def con(results_db):
    for level in (20, 40, 50):
        results_db.record_sql_result(
            platform="motherduck",
            query_id="clickbench_q01",
            block="clickbench",
            scale_factor=100,
            run_type="concurrency",
            run_number=1,
            wall_clock_ms=1000.0,
            server_time_ms=900.0,
            row_count=1,
            cost_usd=0.001,
            cost_confidence="estimated",
            error_message=None,
            concurrency=level,
            compute_label="Pulse",
        )
    results_db.record_sql_result(
        platform="motherduck",
        query_id="clickbench_q01",
        block="clickbench",
        scale_factor=100,
        run_type="warm",
        run_number=1,
        wall_clock_ms=1000.0,
        server_time_ms=900.0,
        row_count=1,
        cost_usd=0.001,
        cost_confidence="estimated",
        error_message=None,
        compute_label="Pulse",
    )
    c = duckdb.connect(str(results_db.path), read_only=True)
    install_views(c)
    yield c
    c.close()


@pytest.mark.parametrize("view", ["results_latest", "concurrency_results", "bench_cb", "bench"])
def test_off_ladder_levels_are_left_out(con, view):
    levels = {
        r[0]
        for r in con.execute(
            f"SELECT concurrency FROM {view} WHERE run_type = 'concurrency'"
        ).fetchall()
    }
    assert 40 not in levels
    assert levels <= set(PROTOCOL_LEVELS)


def test_protocol_levels_and_other_run_types_stay(con):
    rows = con.execute(
        "SELECT run_type, concurrency FROM results_latest ORDER BY run_type, concurrency"
    ).fetchall()
    assert rows == [("concurrency", 20), ("concurrency", 50), ("warm", None)]
    assert con.execute("SELECT count(*) FROM main.sql_benchmark_results").fetchone()[0] == 4

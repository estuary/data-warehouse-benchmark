"""Behavioral tests for the SQL runner: query timeout + per-run resume."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

from benchmark.sql_benchmark.runner import (
    execute_query_runs,
    execute_with_timeout,
    resolve_query_timeout_s,
)


def test_timeout_aborts_fast_and_reconnects(fake_connector):
    conn = fake_connector(sleep_s=5.0)
    t0 = time.perf_counter()
    res = execute_with_timeout(conn, "SELECT 1", 1, platform="fake", query_id="q")
    elapsed = time.perf_counter() - t0
    assert elapsed < 3.0, f"did not time out promptly ({elapsed:.2f}s)"
    assert res.error_message and "timeout" in res.error_message
    assert res.row_count == 0
    assert conn.events == ["disconnect", "connect"]  # reconnect on timeout


def test_abandoned_query_does_not_hold_the_process_at_exit():
    """A query that never returns must not keep the interpreter alive after a timeout.

    Python joins non-daemon threads at exit, so a non-daemon executor worker would
    keep a pass alive for hours after it wrote its last row.
    """
    child = textwrap.dedent(
        """
        import threading
        from benchmark.sql_benchmark.runner import execute_with_timeout

        class Stuck:
            def execute_query(self, sql):
                threading.Event().wait()  # never returns, like an abandoned driver call

            def connect(self):
                pass

            def disconnect(self):
                pass

        res = execute_with_timeout(Stuck(), "SELECT 1", 1, platform="fake", query_id="q")
        assert "timeout" in res.error_message
        """
    )
    done = subprocess.run([sys.executable, "-c", child], timeout=30, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_fast_query_returns_without_reconnect(fake_connector):
    conn = fake_connector(rows=[(1,)])
    res = execute_with_timeout(conn, "SELECT 1", 5, platform="fake", query_id="q")
    assert res.error_message is None and res.row_count == 1
    assert conn.events == []


def test_timeout_policy_default_and_sf1000():
    assert resolve_query_timeout_s(1, "tpcds_q01") == 600
    # tpcds_q01 is not in the SF1000 exception set, so it gets the default too
    assert resolve_query_timeout_s(1000, "tpcds_q01") == 600


def test_resume_fills_only_missing_runs(fake_connector, fake_cost, make_query, results_db):
    conn = fake_connector(rows=[(1,)])
    execute_query_runs(
        make_query(sql="SELECT 1"),
        conn,
        fake_cost(),
        scale_factor=1,
        results_db=results_db,
        platform="fake",
        num_warmups=1,
        num_measured=3,
        completed_runs=frozenset({1, 2}),
    )
    assert conn.calls == 2  # 1 warmup + only the missing measured run (3)
    with results_db.connect() as con:
        runs = [
            r[0]
            for r in con.execute(
                "SELECT run_number FROM sql_benchmark_results ORDER BY run_number"
            ).fetchall()
        ]
    assert runs == [3]


def test_fresh_run_records_all_measured(fake_connector, fake_cost, make_query, results_db):
    conn = fake_connector()
    execute_query_runs(
        make_query(),
        conn,
        fake_cost(),
        scale_factor=1,
        results_db=results_db,
        platform="fake",
        num_warmups=1,
        num_measured=3,
        completed_runs=frozenset(),
    )
    assert conn.calls == 4  # 1 warmup + 3 measured
    with results_db.connect() as con:
        count = con.execute("SELECT count(*) FROM sql_benchmark_results").fetchone()[0]
    assert count == 3


def test_timeout_short_circuits_remaining_runs(fake_connector, fake_cost, make_query, results_db):
    # Warmup times out -> no further executions; all 3 measured runs recorded
    # as skipped-after-timeout error rows.
    conn = fake_connector(sleep_s=5.0)
    execute_query_runs(
        make_query(sql="SELECT 1"),
        conn,
        fake_cost(),
        scale_factor=1,
        results_db=results_db,
        platform="fake",
        num_warmups=1,
        num_measured=3,
        completed_runs=frozenset(),
        timeout_s=1,
    )
    assert conn.calls == 1  # only the warmup ever executed
    with results_db.connect() as con:
        rows = con.execute(
            "SELECT run_number, error_message, metadata FROM sql_benchmark_results "
            "ORDER BY run_number"
        ).fetchall()
    assert [r[0] for r in rows] == [1, 2, 3]
    assert all("skipped" in r[1] for r in rows)
    assert all("skipped_after_timeout" in (r[2] or "") for r in rows)

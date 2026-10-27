"""Sustained load: N sessions held busy for a fixed time, kept apart from bursts."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import duckdb

from benchmark.report.views import install_views
from benchmark.results import SUSTAINED_RUN_TYPE
from benchmark.sql_benchmark import concurrency


class _Clock:
    """Advances a fixed step on every read, so the pass ends deterministically."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            self.now += self.step
            return self.now


def _row(query, sessions: int, *, lost: bool = False) -> dict:
    return {
        "platform": "fabric",
        "query_id": query.id,
        "block": query.block,
        "scale_factor": 100,
        "concurrency": sessions,
        "run_number": 1,
        "wall_clock_ms": 5.0,
        "server_time_ms": 4.0,
        "bytes_scanned": None,
        "platform_query_id": None,
        "row_count": 1,
        "cost_usd": None,
        "cost_confidence": None,
        "error_message": "query exceeded timeout of 600s" if lost else None,
        "metadata": {"connection_lost": True} if lost else {},
    }


def _run(monkeypatch, *, sessions: int, n_queries: int, seconds: float, lost_slots=()):
    conns = [SimpleNamespace(slot=i, disconnect=lambda: None) for i in range(sessions)]
    monkeypatch.setattr(
        concurrency,
        "_open_pool",
        lambda _p, _cc, _costc, size, **_kw: [(conns[i], SimpleNamespace()) for i in range(size)],
    )

    def _execute_on(conn, _cost, _platform, q, _sf, lvl):
        time.sleep(0.002)  # a query takes time, so every session gets a turn
        return _row(q, lvl, lost=conn.slot in lost_slots)

    monkeypatch.setattr(concurrency, "_execute_on", _execute_on)
    recorded: list[dict] = []
    db = SimpleNamespace(record_sql_result=lambda **kw: recorded.append(kw))
    queries = [SimpleNamespace(id=f"tpcds_q{i:02d}", block="tpcds") for i in range(n_queries)]
    n = concurrency._run_sustained(
        "fabric", {}, {}, db, queries, 100, sessions, seconds, "F2", clock=_Clock(1.0)
    )
    assert n == len(recorded)
    return recorded


def test_sessions_cycle_the_suite_until_time_is_up(monkeypatch):
    recorded = _run(monkeypatch, sessions=3, n_queries=4, seconds=20.0)

    assert all(r["run_type"] == SUSTAINED_RUN_TYPE for r in recorded)
    assert all(r["concurrency"] == 3 and r["compute_label"] == "F2" for r in recorded)
    # More than one pass through the suite, and every (pass, query) at most once:
    # the rows never collapse under latest-wins.
    keys = [(r["run_number"], r["query_id"]) for r in recorded]
    assert max(r["run_number"] for r in recorded) >= 2
    assert len(keys) == len(set(keys))
    # Each row carries when it was submitted, inside the window.
    offsets = [r["metadata"]["submitted_s"] for r in recorded]
    assert all(0 <= o < 20.0 for o in offsets)


def test_a_lost_session_leaves_the_pass(monkeypatch):
    recorded = _run(monkeypatch, sessions=2, n_queries=3, seconds=30.0, lost_slots={0})
    lost = [r for r in recorded if r["error_message"]]
    assert len(lost) == 1  # the lost session took no further work


def test_sustained_rows_stay_out_of_every_burst_view():
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE main.sql_benchmark_results (platform VARCHAR, query_id VARCHAR, "
        "block VARCHAR, scale_factor INTEGER, run_type VARCHAR, run_number INTEGER, "
        "concurrency INTEGER, compute_label VARCHAR, error_message VARCHAR, "
        "server_time_ms DOUBLE, wall_clock_ms DOUBLE, created_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO main.sql_benchmark_results VALUES "
        "('fabric','tpcds_q01','tpcds',100,'concurrency',1,50,'F2',NULL,10,12,now()),"
        f"('fabric','tpcds_q01','tpcds',100,'{SUSTAINED_RUN_TYPE}',1,50,'F2',NULL,10,12,now()),"
        f"('fabric','tpcds_q01','tpcds',100,'{SUSTAINED_RUN_TYPE}',2,50,'F2',NULL,30,33,now())"
    )
    install_views(con)

    def run_types(view: str) -> list[str]:
        return sorted(r[0] for r in con.execute(f"SELECT run_type FROM {view}").fetchall())

    assert run_types("results_latest") == ["concurrency"]
    assert run_types("concurrency_results") == ["concurrency"]
    assert SUSTAINED_RUN_TYPE not in run_types("bench") + run_types("bench_coverage")
    assert run_types("sustained_results") == [SUSTAINED_RUN_TYPE, SUSTAINED_RUN_TYPE]
    assert con.execute("SELECT max(engine_ms) FROM sustained_results").fetchone()[0] == 30

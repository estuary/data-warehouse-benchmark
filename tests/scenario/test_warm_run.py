"""Scenario tests: the warm-run orchestration (run_sql_benchmark) over a small
query set, exercised with fake connectors — recording and resume.

QueryRegistry is stubbed so the flow runs on 2 synthetic queries instead of the
244-query suite / real translated SQL.
"""

from __future__ import annotations

import pytest

from benchmark.queries.registry import BenchmarkQuery
from benchmark.sql_benchmark import runner as runner_mod
from benchmark.sql_benchmark.runner import run_sql_benchmark


class _StubRegistry:
    def __init__(self, queries: list[BenchmarkQuery]) -> None:
        self._queries = queries

    def all(self) -> list[BenchmarkQuery]:
        return self._queries


@pytest.fixture
def two_queries() -> list[BenchmarkQuery]:
    return [
        BenchmarkQuery(id="tpcds_q01", block="tpcds", number=1, sql="SELECT 1", patterns=()),
        BenchmarkQuery(id="tpcds_q02", block="tpcds", number=2, sql="SELECT 2", patterns=()),
    ]


@pytest.fixture(autouse=True)
def _stub_registry(monkeypatch, two_queries):
    monkeypatch.setattr(
        runner_mod, "QueryRegistry", lambda dialect=None: _StubRegistry(two_queries)
    )


def _row_count(db) -> int:
    with db.connect() as con:
        return con.execute("SELECT count(*) FROM sql_benchmark_results").fetchone()[0]


def test_warm_run_records_three_measured_per_query(fake_connector, fake_cost, results_db):
    conn = fake_connector(rows=[(1,)])
    run_sql_benchmark(
        "fake",
        conn,
        fake_cost(),
        results_db,
        scale_factor=1,
        blocks=["tpcds"],
        num_warmups=1,
        num_measured=3,
    )
    with results_db.connect() as con:
        per_q = con.execute(
            "SELECT query_id, count(*) FROM sql_benchmark_results GROUP BY 1 ORDER BY 1"
        ).fetchall()
    assert per_q == [("tpcds_q01", 3), ("tpcds_q02", 3)]
    # 2 queries x (1 warmup + 3 measured)
    assert conn.calls == 8


def test_resume_skips_completed_queries_no_duplicates(fake_connector, fake_cost, results_db):
    run_sql_benchmark(
        "fake",
        fake_connector(),
        fake_cost(),
        results_db,
        scale_factor=1,
        blocks=["tpcds"],
        num_warmups=1,
        num_measured=3,
    )
    assert _row_count(results_db) == 6

    conn2 = fake_connector()
    run_sql_benchmark(
        "fake",
        conn2,
        fake_cost(),
        results_db,
        scale_factor=1,
        blocks=["tpcds"],
        num_warmups=1,
        num_measured=3,
    )
    assert conn2.calls == 0  # everything already recorded -> skipped
    assert _row_count(results_db) == 6  # no duplicate rows appended

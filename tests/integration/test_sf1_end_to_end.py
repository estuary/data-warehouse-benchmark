"""SF1 (parameterizable) local-DuckDB end-to-end integration tests.

Runs the *real* harness pipeline against a local DuckDB built with the TPC-DS
``dsdgen`` extension (see conftest): generate data -> compute ground-truth
hashes -> run the queries through a WarehouseConnector -> validate / record.
No cloud engine.

Opt-in: ``uv run pytest tests/integration --run-integration``.
Scale via ``BENCHMARK_TEST_SF`` (default 0.01; set 1 for true SF1).
"""

from __future__ import annotations

import duckdb
import pytest

from benchmark.queries.registry import BenchmarkQuery
from benchmark.sql_benchmark import runner as runner_mod
from benchmark.sql_benchmark import validator as validator_mod
from benchmark.sql_benchmark.runner import run_sql_benchmark
from benchmark.sql_benchmark.validator import (
    hash_query_result,
    serialize_rows,
    validate_platform,
)

pytestmark = pytest.mark.integration

# A few deterministic canonical-style queries over the TPC-DS schema.
QUERIES = [
    BenchmarkQuery(
        id="t_count", block="tpcds", number=1, sql="SELECT COUNT(*) FROM store_sales", patterns=()
    ),
    BenchmarkQuery(
        id="t_group",
        block="tpcds",
        number=2,
        sql="SELECT ss_store_sk, COUNT(*) AS n FROM store_sales "
        "GROUP BY ss_store_sk ORDER BY ss_store_sk",
        patterns=(),
    ),
    BenchmarkQuery(
        id="t_join",
        block="tpcds",
        number=3,
        sql="SELECT i_category, COUNT(*) AS n FROM store_sales "
        "JOIN item ON ss_item_sk = i_item_sk GROUP BY i_category "
        "ORDER BY i_category",
        patterns=(),
    ),
]


class _StubRegistry:
    def __init__(self, queries):
        self._queries = queries

    def all(self):
        return self._queries


def _seed_ground_truth(db_path, results_db) -> None:
    con = duckdb.connect(str(db_path), read_only=True)
    rows = []
    for q in QUERIES:
        result = [tuple(r) for r in con.execute(q.sql).fetchall()]
        rows.append(
            (
                q.id,
                q.block,
                1,
                hash_query_result(result, q.sql),
                len(result),
                0.0,
                None,
                "test",
                serialize_rows(result),
                None,
            )
        )
    con.close()
    results_db.record_ground_truth(rows)


def test_validate_sf1_end_to_end(tpcds_db, results_db, local_connector, monkeypatch):
    """Ground truth computed on the data must match the same queries run back
    through the harness connector + validator (no lenient fallback)."""
    _seed_ground_truth(tpcds_db, results_db)
    monkeypatch.setattr(validator_mod, "QueryRegistry", lambda dialect=None: _StubRegistry(QUERIES))

    summary = validate_platform(
        "local_duckdb", local_connector(tpcds_db), results_db, scale_factor=1, blocks=["tpcds"]
    )
    assert len(summary) == len(QUERIES)
    failed = [(r.query.id, r.reason) for r in summary if not r.passed]
    assert not failed, failed


def test_warm_run_end_to_end_records_results(
    tpcds_db, results_db, fake_cost, local_connector, monkeypatch
):
    """A full warm run over real local data records measured runs with timings."""
    monkeypatch.setattr(runner_mod, "QueryRegistry", lambda dialect=None: _StubRegistry(QUERIES))

    run_sql_benchmark(
        "local_duckdb",
        local_connector(tpcds_db),
        fake_cost(),
        results_db,
        scale_factor=1,
        blocks=["tpcds"],
        num_warmups=1,
        num_measured=2,
    )
    with results_db.connect() as con:
        ok, with_time = con.execute(
            "SELECT COUNT(*), COUNT(server_time_ms) FROM sql_benchmark_results "
            "WHERE error_message IS NULL"
        ).fetchone()
    assert ok == len(QUERIES) * 2  # 2 measured runs each
    assert with_time == ok  # local connector reports timing for every run

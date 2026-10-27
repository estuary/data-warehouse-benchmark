"""Integration-layer fixtures: a local-DuckDB WarehouseConnector + a TPC-DS
DuckDB built via the dsdgen extension.

Lets the real harness (validate_platform, run_sql_benchmark) run end-to-end
against a local ``.duckdb`` file — no cloud engine / credentials. Scale via
``BENCHMARK_TEST_SF`` (default 0.01 for speed; 1 = true SF1).
"""

from __future__ import annotations

import os
import time

import duckdb
import pytest

from benchmark.connectors.base import QueryResult, WarehouseConnector

TEST_SF = float(os.environ.get("BENCHMARK_TEST_SF", "0.01"))


class LocalDuckDBConnector(WarehouseConnector):
    """A read-only WarehouseConnector backed by a local DuckDB file."""

    def __init__(self, db_path: object, config: dict | None = None) -> None:
        super().__init__("local_duckdb", config or {})
        self._db_path = str(db_path)
        self._con: duckdb.DuckDBPyConnection | None = None

    def connect(self) -> None:
        self._con = duckdb.connect(self._db_path, read_only=True)

    def disconnect(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def execute_query(self, sql: str) -> QueryResult:
        start = time.perf_counter()
        try:
            cur = self._con.execute(sql)
            rows = [tuple(r) for r in cur.fetchall()]
            cols = [d[0] for d in (cur.description or [])]
            wall = (time.perf_counter() - start) * 1000
            return QueryResult(
                rows=rows,
                columns=cols,
                row_count=len(rows),
                wall_clock_ms=wall,
                server_time_ms=wall,
            )
        except Exception as e:  # record-and-continue, like the real connectors
            wall = (time.perf_counter() - start) * 1000
            return QueryResult(
                rows=[], columns=[], row_count=0, wall_clock_ms=wall, error_message=str(e)
            )

    def runtime_compute(self) -> dict:
        # The runner aborts a cell whose environment capture yields no
        # compute-size label (UnlabelledComputeError), so even this local stand-in
        # must self-report a size the way a healthy engine does.
        return {"size": "local"}

    def qualify_table(self, schema: str, table: str) -> str:
        return table


@pytest.fixture(scope="module")
def tpcds_db(tmp_path_factory):
    """A local DuckDB populated with TPC-DS data via the dsdgen extension."""
    path = tmp_path_factory.mktemp("tpcds") / f"tpcds_sf{TEST_SF}.duckdb"
    con = duckdb.connect(str(path))
    con.execute("INSTALL tpcds; LOAD tpcds;")
    con.execute(f"CALL dsdgen(sf={TEST_SF})")
    con.close()
    return path


@pytest.fixture
def local_connector():
    """The LocalDuckDBConnector class (tests instantiate with a db path)."""
    return LocalDuckDBConnector

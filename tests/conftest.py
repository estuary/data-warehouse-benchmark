"""Shared pytest fixtures + doubles for the benchmark harness test suite.

Three layers live under ``tests/``: ``unit/`` (behavioral logic), ``scenario/``
(multi-step flows with these doubles), and ``integration/`` (end-to-end on a
local DuckDB; opt-in via ``--run-integration``).
"""

from __future__ import annotations

import time

import pytest

from benchmark.connectors.base import QueryResult
from benchmark.queries.registry import BenchmarkQuery
from benchmark.results import ResultsDB


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="run integration tests (build data + local-DuckDB end-to-end)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="needs --run-integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


class FakeCost:
    """CostCollector double: fixed confidence + per-call cost."""

    def __init__(self, cost: float = 0.0, confidence: str = "estimated") -> None:
        self._cost = cost
        self.confidence = confidence

    def cost_from_result(self, result: QueryResult) -> float:
        return self._cost


class FakeConnector:
    """WarehouseConnector double. Records execute/connect/disconnect calls and
    returns a configurable QueryResult (or sleeps, or errors)."""

    def __init__(
        self,
        *,
        rows: list[tuple] | None = None,
        server_time_ms: float | None = None,
        bytes_scanned: int | None = None,
        platform_query_id: str | None = None,
        sleep_s: float = 0.0,
        error: str | None = None,
        environment: dict | None = None,
        compute: dict | None = None,
    ) -> None:
        self.calls = 0
        self.events: list[str] = []
        self._rows = [(1,)] if rows is None else rows
        self._server = server_time_ms
        self._bytes = bytes_scanned
        self._pqid = platform_query_id
        self._sleep = sleep_s
        self._error = error
        self._environment = environment or {"platform": "fake"}
        # A healthy connector must self-report a labelable compute size: the runner
        # aborts a cell whose capture yields no label (UnlabelledComputeError, the
        # guard against silently losing a cell to NULL labels). Default to a size so
        # tests model a working engine; pass compute={} to exercise the unlabelled
        # path deliberately.
        self._compute = {"size": "fake-size"} if compute is None else compute

    def connect(self) -> None:
        self.events.append("connect")

    def disconnect(self) -> None:
        self.events.append("disconnect")

    def execute_query(self, sql: str) -> QueryResult:
        self.calls += 1
        if self._sleep:
            time.sleep(self._sleep)
        if self._error:
            return QueryResult(
                rows=[], columns=[], row_count=0, wall_clock_ms=1.0, error_message=self._error
            )
        return QueryResult(
            rows=list(self._rows),
            columns=["c"],
            row_count=len(self._rows),
            wall_clock_ms=1.0,
            server_time_ms=self._server,
            bytes_scanned=self._bytes,
            platform_query_id=self._pqid,
        )

    def runtime_environment(self) -> dict:
        return dict(self._environment)

    def runtime_compute(self) -> dict:
        return dict(self._compute)

    def __enter__(self) -> FakeConnector:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()


@pytest.fixture
def results_db(tmp_path) -> ResultsDB:
    """A fresh on-disk ResultsDB in a temp dir."""
    return ResultsDB(tmp_path / "test.duckdb")


@pytest.fixture
def fake_connector():
    """The FakeConnector class (tests instantiate with kwargs)."""
    return FakeConnector


@pytest.fixture
def fake_cost():
    """The FakeCost class (tests instantiate with kwargs)."""
    return FakeCost


@pytest.fixture
def make_query():
    def _make(
        qid: str = "tpcds_q01",
        block: str = "tpcds",
        number: int = 1,
        sql: str = "SELECT 1",
        patterns: tuple[str, ...] = (),
    ) -> BenchmarkQuery:
        return BenchmarkQuery(id=qid, block=block, number=number, sql=sql, patterns=tuple(patterns))

    return _make

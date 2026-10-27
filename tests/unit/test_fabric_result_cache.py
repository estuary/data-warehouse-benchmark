"""FabricConnector refuses to connect while the warehouse's result-set cache is on."""

from __future__ import annotations

from typing import Any

import pyodbc
import pytest

from benchmark.connectors import fabric as fabric_mod
from benchmark.connectors.fabric import (
    RESULT_CACHE_CHECK_SQL,
    RESULT_CACHE_CHECK_TIMEOUT_S,
    RESULT_CACHE_OFF_SQL,
    FabricConnector,
)
from benchmark.exceptions import ConnectorError


class _Cursor:
    def __init__(self, value: object = False, exc: Exception | None = None) -> None:
        self.value = value
        self.exc = exc
        self.executed: list[str] = []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if self.exc is not None:
            raise self.exc

    def fetchone(self) -> tuple[object]:
        return (self.value,)

    def close(self) -> None: ...


class _Conn:
    autocommit = False

    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.closed = False
        self.timeout = 0
        self.cursor_timeouts: list[int] = []

    def cursor(self) -> _Cursor:
        # pyodbc fixes a cursor's statement timeout from the connection's at creation.
        self.cursor_timeouts.append(self.timeout)
        return self._cursor

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fresh_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fabric_mod, "_RESULT_CACHE_VERIFIED_OFF", set())


def _connector(
    monkeypatch: pytest.MonkeyPatch, cursor: _Cursor, **config: Any
) -> tuple[FabricConnector, _Conn]:
    conn = _Conn(cursor)
    monkeypatch.setattr(FabricConnector, "_token_struct", lambda self: b"")
    monkeypatch.setattr(fabric_mod.pyodbc, "connect", lambda *a, **k: conn)
    return FabricConnector({"endpoint": "e", "database": "BENCHMARK_DW", **config}), conn


def test_refuses_when_result_cache_on(monkeypatch: pytest.MonkeyPatch) -> None:
    fc, conn = _connector(monkeypatch, _Cursor(value=True))
    with pytest.raises(ConnectorError, match="result-set cache on") as err:
        fc.connect()
    assert RESULT_CACHE_OFF_SQL in str(err.value)
    assert conn.closed and fc.conn is None


def test_connects_when_result_cache_off(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _Cursor(value=False)
    fc, conn = _connector(monkeypatch, cursor)
    fc.connect()
    assert cursor.executed == [RESULT_CACHE_CHECK_SQL]
    assert not conn.closed


def test_disable_cache_false_skips_check(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _Cursor(value=True)
    fc, _ = _connector(monkeypatch, cursor, disable_cache=False)
    fc.connect()
    assert cursor.executed == []


def test_endpoint_without_setting_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _Cursor(exc=pyodbc.ProgrammingError("42S22", "Invalid column name"))
    fc, conn = _connector(monkeypatch, cursor)
    fc.connect()
    assert not conn.closed


def test_unreadable_setting_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _Cursor(exc=pyodbc.OperationalError("08S01", "Communication link failure"))
    fc, conn = _connector(monkeypatch, cursor)
    with pytest.raises(ConnectorError, match="could not read"):
        fc.connect()
    assert conn.closed


def test_check_has_a_timeout_and_runs_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reconnect after a query timeout must not re-run the check: queued behind the
    abandoned queries it can hang for good. Only the first connect checks, on its
    own cursor with a statement timeout, and the main cursor has none."""
    cursor = _Cursor(value=False)
    fc, conn = _connector(monkeypatch, cursor)
    fc.connect()
    assert conn.cursor_timeouts == [0, RESULT_CACHE_CHECK_TIMEOUT_S]
    assert conn.timeout == 0
    fc.disconnect()
    fc.connect()
    assert cursor.executed == [RESULT_CACHE_CHECK_SQL]


def test_a_cache_found_on_is_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _Cursor(value=True)
    fc, _ = _connector(monkeypatch, cursor)
    for _ in range(2):
        with pytest.raises(ConnectorError):
            fc.connect()
    assert cursor.executed == [RESULT_CACHE_CHECK_SQL, RESULT_CACHE_CHECK_SQL]

"""A session lost after a timeout: retried reconnect, no 0 ms failures, ids kept.

Without a retried reconnect, one failed reconnect after a timeout leaves the
connector closed and every later query on it fails instantly with "connector not
connected", which the concurrency analysis would count as the engine shedding
work.
"""

from __future__ import annotations

import functools
import threading
import time
from types import SimpleNamespace

from benchmark.connectors.base import QueryResult
from benchmark.connectors.connection import reconnect_in_place
from benchmark.connectors.databricks import DatabricksConnector
from benchmark.sql_benchmark import concurrency, runner
from benchmark.sql_benchmark.runner import SESSION_LOST_MESSAGE, execute_with_timeout


class _FlakyConnector:
    """Connects successfully ``connect_ok`` times, then refuses; queries can hang."""

    def __init__(self, *, connect_ok: int = 0, sleep_s: float = 0.0, inflight: str | None = None):
        self.connection_lost = False
        self.events: list[str] = []
        self.executed = 0
        self._connect_ok = connect_ok
        self._sleep = sleep_s
        self._inflight = inflight

    def connect(self) -> None:
        if self._connect_ok <= 0:
            self.events.append("connect-failed")
            raise OSError("Network is unreachable")
        self._connect_ok -= 1
        self.events.append("connect")

    def disconnect(self) -> None:
        self.events.append("disconnect")

    def execute_query(self, sql: str) -> QueryResult:
        self.executed += 1
        if self._sleep:
            time.sleep(self._sleep)
        return QueryResult(rows=[(1,)], columns=["x"], row_count=1, wall_clock_ms=1.0)

    def inflight_query_id(self) -> str | None:
        return self._inflight


def _no_sleep(monkeypatch) -> None:
    monkeypatch.setattr(
        runner, "reconnect_in_place", functools.partial(reconnect_in_place, sleep=lambda _s: None)
    )


def test_reconnect_in_place_retries_then_succeeds():
    conn = _FlakyConnector(connect_ok=0)
    calls = {"n": 0}

    def _connect() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("blip")

    conn.connect = _connect
    delays: list[float] = []
    assert reconnect_in_place(conn, sleep=delays.append) is True
    assert calls["n"] == 2 and len(delays) == 1
    assert conn.connection_lost is False


def test_reconnect_in_place_marks_the_session_lost():
    conn = _FlakyConnector(connect_ok=0)
    assert reconnect_in_place(conn, attempts=3, sleep=lambda _s: None) is False
    assert conn.events.count("connect-failed") == 3
    assert conn.connection_lost is True


def test_timeout_with_failed_reconnect_flags_the_row(monkeypatch):
    _no_sleep(monkeypatch)
    conn = _FlakyConnector(connect_ok=0, sleep_s=2.0)
    res = execute_with_timeout(conn, "SELECT 1", 1, platform="fake", query_id="q")
    assert res.error_message == "query exceeded timeout of 1s"
    assert res.metadata == {"connection_lost": True}
    assert conn.connection_lost is True


def test_lost_session_is_not_sent_queries(monkeypatch):
    """A lost session that still cannot reconnect records the query as not run."""
    _no_sleep(monkeypatch)
    conn = _FlakyConnector(connect_ok=0)
    conn.connection_lost = True
    res = execute_with_timeout(conn, "SELECT 1", 5, platform="fake", query_id="q")
    assert res.error_message == SESSION_LOST_MESSAGE
    assert res.metadata["connection_lost"] is True
    assert conn.executed == 0


def test_lost_session_that_reconnects_runs_the_query(monkeypatch):
    _no_sleep(monkeypatch)
    conn = _FlakyConnector(connect_ok=1)
    conn.connection_lost = True
    res = execute_with_timeout(conn, "SELECT 1", 5, platform="fake", query_id="q")
    assert res.error_message is None and conn.executed == 1
    assert conn.connection_lost is False


def test_timeout_keeps_the_inflight_statement_id(monkeypatch):
    _no_sleep(monkeypatch)
    conn = _FlakyConnector(connect_ok=1, sleep_s=2.0, inflight="stmt-7")
    res = execute_with_timeout(conn, "SELECT 1", 1, platform="fake", query_id="q")
    assert res.platform_query_id == "stmt-7"
    assert res.metadata == {}  # reconnected: the session is fine


def test_databricks_inflight_id_is_new_ids_only():
    """The cursor holds the previous statement's id until the warehouse acknowledges
    the next one, so only a changed id is the in-flight statement."""
    conn = DatabricksConnector({})
    conn.cursor = SimpleNamespace(query_id="old-id")
    conn._id_before_execute = "old-id"
    assert conn.inflight_query_id() is None  # submit not acknowledged yet
    conn.cursor.query_id = "new-id"
    assert conn.inflight_query_id() == "new-id"
    conn.cursor = None
    assert conn.inflight_query_id() is None


def _row(query, level: int, *, lost: bool = False) -> dict:
    return {
        "platform": "snowflake",
        "query_id": query.id,
        "block": query.block,
        "scale_factor": 1000,
        "concurrency": level,
        "run_number": 1,
        "wall_clock_ms": 1.0,
        "server_time_ms": 1.0,
        "bytes_scanned": None,
        "platform_query_id": None,
        "row_count": None if lost else 1,
        "cost_usd": None,
        "cost_confidence": None,
        "error_message": "query exceeded timeout of 600s" if lost else None,
        "metadata": {"connection_lost": True} if lost else {},
    }


def _run_level(monkeypatch, *, lost_slots: set[int], n_queries: int, level: int) -> list[dict]:
    conns = [SimpleNamespace(slot=i, disconnect=lambda: None) for i in range(level)]
    monkeypatch.setattr(
        concurrency,
        "_open_pool",
        lambda _p, _cc, _costc, size, **_kw: [(conns[i], SimpleNamespace()) for i in range(size)],
    )
    ran: dict[int, int] = {}

    def _fake_execute_on(conn, _cost, _platform, query, _sf, lvl):
        ran[conn.slot] = ran.get(conn.slot, 0) + 1
        time.sleep(0.01)  # let every worker take a query before the queue empties
        return _row(query, lvl, lost=conn.slot in lost_slots)

    monkeypatch.setattr(concurrency, "_execute_on", _fake_execute_on)
    recorded: list[dict] = []
    db = SimpleNamespace(record_sql_result=lambda **kw: recorded.append(kw))
    queries = [SimpleNamespace(id=f"tpcds_q{i:02d}", block="tpcds") for i in range(n_queries)]
    concurrency._run_level("snowflake", {}, {}, db, queries, 1000, level, "X-Large")
    assert sorted(r["query_id"] for r in recorded) == sorted(q.id for q in queries)
    for slot in lost_slots:
        assert ran[slot] == 1  # a lost session takes no further work
    return recorded


def test_lost_session_leaves_the_rest_to_open_sessions(monkeypatch):
    recorded = _run_level(monkeypatch, lost_slots={0}, n_queries=12, level=3)
    errors = [r for r in recorded if r["error_message"]]
    assert len(errors) == 1  # only the query that timed out; none in 0 ms


def test_every_session_lost_records_the_rest_as_not_run(monkeypatch):
    recorded = _run_level(monkeypatch, lost_slots={0, 1}, n_queries=6, level=2)
    not_run = [r for r in recorded if r["error_message"] == SESSION_LOST_MESSAGE]
    assert len(not_run) == 4
    assert all(r["metadata"]["not_run"] for r in not_run)


class _CancellableConnector(_FlakyConnector):
    """A query that blocks until cancelled (or forever when ``honour_cancel`` is off)."""

    def __init__(self, *, honour_cancel: bool):
        super().__init__(connect_ok=1)
        self._release = threading.Event()
        self._honour = honour_cancel

    def execute_query(self, sql: str) -> QueryResult:
        self.executed += 1
        self._release.wait(5)
        self.events.append("execute-returned")
        return QueryResult(rows=[], columns=[], row_count=0, wall_clock_ms=1.0)

    def cancel_running(self) -> None:
        self.events.append("cancel")
        if self._honour:
            self._release.set()

    def abandon_session(self) -> None:
        self.events.append("abandon")


def test_timeout_cancels_and_closes_only_after_the_call_returns(monkeypatch):
    """Closing a C driver's session under a running call can segfault the process
    (Fabric's ODBC driver does)."""
    _no_sleep(monkeypatch)
    conn = _CancellableConnector(honour_cancel=True)
    res = execute_with_timeout(conn, "SELECT 1", 0, platform="fake", query_id="q")
    assert res.error_message == "query exceeded timeout of 0s"
    assert conn.events[:3] == ["cancel", "execute-returned", "disconnect"]
    assert "abandon" not in conn.events and conn.events[-1] == "connect"


def test_a_query_still_in_the_driver_has_its_session_set_aside(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(runner, "ABANDON_GRACE_S", 0.2)
    conn = _CancellableConnector(honour_cancel=False)
    execute_with_timeout(conn, "SELECT 1", 0, platform="fake", query_id="q")
    assert conn.events[:2] == ["cancel", "abandon"]
    assert "disconnect" not in conn.events and conn.events[-1] == "connect"
    conn._release.set()


def test_fabric_sets_a_session_aside_without_closing_it():
    from benchmark.connectors import fabric

    closed: list[str] = []
    old_conn = SimpleNamespace(close=lambda: closed.append("conn"))
    old_cursor = SimpleNamespace(close=lambda: closed.append("cursor"))
    fc = fabric.FabricConnector({})
    fc.conn, fc.cursor = old_conn, old_cursor
    before = len(fabric._ABANDONED_SESSIONS)
    fc.abandon_session()
    assert fc.conn is None and fc.cursor is None and closed == []
    assert fabric._ABANDONED_SESSIONS[before:] == [(old_conn, old_cursor)]
    fabric._ABANDONED_SESSIONS[before:] = []


def test_fabric_execute_never_touches_a_replacement_cursor(monkeypatch):
    """The call binds its cursor once, so a reconnect during it cannot hand it another
    query's results."""
    from benchmark.connectors import fabric

    fc = fabric.FabricConnector({})
    monkeypatch.setattr(fc, "_qualify_sql", lambda sql: sql)
    monkeypatch.setattr(fc, "_database", lambda: "db")
    replacement = SimpleNamespace(
        description=[("y",)], fetchall=lambda: [(99,)], execute=lambda _s: None
    )

    def _execute(_statement: str) -> None:
        fc.cursor = replacement  # what a reconnect on the timeout path does

    first = SimpleNamespace(description=[("x",)], fetchall=lambda: [(1,)], execute=_execute)
    fc.cursor = first
    res = fc.execute_query("SELECT 1")
    assert res.rows == [(1,)] and res.columns == ["x"]

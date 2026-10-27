"""Pre-warm + barrier concurrency model.

Covers the slow-ramp session pool (``_open_pool``) and the barrier-synchronized
level runner (``_run_level``) without touching a real engine or network.
"""

from __future__ import annotations

from types import SimpleNamespace

from benchmark.exceptions import ConnectorError
from benchmark.sql_benchmark import concurrency


class _FakeConnector:
    def __init__(self) -> None:
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


def test_open_pool_staggers_and_returns_full_pool(monkeypatch):
    """N sessions opened one at a time, with a stagger between each (N-1 sleeps)."""
    made: list[_FakeConnector] = []

    def _connect(_factory, **_kw):
        c = _FakeConnector()
        made.append(c)
        return c

    monkeypatch.setattr(concurrency, "connect_with_retry", _connect)
    monkeypatch.setattr(concurrency, "collector_for", lambda *_a, **_k: SimpleNamespace())
    sleeps: list[float] = []

    pool = concurrency._open_pool("snowflake", {}, {}, size=5, stagger_s=0.1, sleep=sleeps.append)

    assert len(pool) == 5
    # A stagger between successive connects, but not after the last one.
    assert sleeps == [0.1, 0.1, 0.1, 0.1]


def test_open_pool_skips_failed_connects(monkeypatch):
    """A session that can't be opened is skipped (continue-on-failure), not fatal."""
    calls = {"n": 0}

    def _connect(_factory, **_kw):
        calls["n"] += 1
        if calls["n"] == 3:  # third slot fails after retries
            raise ConnectorError("Network is unreachable")
        return _FakeConnector()

    monkeypatch.setattr(concurrency, "connect_with_retry", _connect)
    monkeypatch.setattr(concurrency, "collector_for", lambda *_a, **_k: SimpleNamespace())

    pool = concurrency._open_pool("snowflake", {}, {}, size=4, stagger_s=0, sleep=lambda _s: None)

    assert len(pool) == 3  # 4 requested, 1 failed → achieved concurrency 3


def test_run_level_records_every_query(monkeypatch):
    """Every query in the work set produces exactly one recorded row, tagged with
    the level and compute label; sessions are torn down afterward."""
    conns = [_FakeConnector() for _ in range(3)]

    def _fake_open_pool(_p, _cc, _costc, size, **_kw):
        return [(conns[i], SimpleNamespace()) for i in range(size)]

    monkeypatch.setattr(concurrency, "_open_pool", _fake_open_pool)

    def _fake_execute_on(_conn, _cost, platform, query, scale_factor, concurrency_level):
        return {
            "platform": platform,
            "query_id": query.id,
            "block": query.block,
            "scale_factor": scale_factor,
            "concurrency": concurrency_level,
            "run_number": 1,
            "wall_clock_ms": 1.0,
            "server_time_ms": 1.0,
            "bytes_scanned": None,
            "platform_query_id": None,
            "row_count": 1,
            "cost_usd": None,
            "cost_confidence": "estimated",
            "error_message": None,
            "metadata": {},
        }

    monkeypatch.setattr(concurrency, "_execute_on", _fake_execute_on)

    recorded: list[dict] = []

    class _FakeDB:
        def record_sql_result(self, **kw):
            recorded.append(kw)

    queries = [
        SimpleNamespace(id=f"tpcds_q{i:02d}", block="tpcds", sql="SELECT 1") for i in range(7)
    ]

    concurrency._run_level(
        "snowflake",
        {},
        {},
        _FakeDB(),
        queries,
        scale_factor=1,
        level=3,
        compute_label="Small (Gen2)",
    )

    assert len(recorded) == len(queries)  # one row per query, none lost
    assert {r["query_id"] for r in recorded} == {q.id for q in queries}
    assert all(r["concurrency"] == 3 for r in recorded)
    assert all(r["compute_label"] == "Small (Gen2)" for r in recorded)
    assert all(r["run_type"] == "concurrency" for r in recorded)
    assert all(c.disconnected for c in conns)  # pool torn down after the level

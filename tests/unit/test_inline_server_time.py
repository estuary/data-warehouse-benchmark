"""Server-time collection must stay out of the measurement path where it is futile.

Measured on a live Databricks warehouse: reading ``system.query.history`` inline,
immediately after each query, costs **15.3 s per query** and returns ``None`` every
single time, because the system table has not yet materialized the row. A trivial
`SELECT 1` round-trip took 16.7 s, of which 1.4 s was the query. That is roughly 90%
of the wall-clock of a Databricks benchmark pass, spent on nothing, and it injects
an extra statement into the measured session, a load the engines that skip it never
see.

Two properties are pinned here. The lookup must not happen inline for engines whose
history table lags, and ``platform_query_id`` must still be captured — without it
``reconcile`` cannot find the row later, and server time would be lost rather than
merely deferred.
"""

from __future__ import annotations

import pytest

from benchmark.connectors.base import WarehouseConnector

#: engine -> whether reading its history table inline is worth the round-trip.
#: False where the table lags completion by minutes. Redshift's SYS_QUERY_HISTORY
#: lands promptly (measured overhead 1.5 s, and the lookup succeeds), so it stays.
EXPECTED_INLINE = {
    "databricks": False,
    "fabric": False,
    "redshift": True,
    "snowflake": True,
    "bigquery": True,
    "clickhouse": True,
    "motherduck": True,
}


@pytest.mark.parametrize(("engine", "expected"), sorted(EXPECTED_INLINE.items()))
def test_inline_server_time_policy_is_declared_per_engine(engine, expected):
    """The policy is a class attribute so it is visible and testable rather than
    implied by whether some line of `execute_query` happens to call a method."""
    from benchmark.connectors.registry import connector_class

    cls = connector_class(engine)

    assert cls.INLINE_SERVER_TIME is expected, (
        f"{engine}: INLINE_SERVER_TIME should be {expected}. Flipping this to True "
        "for an engine whose history table lags re-introduces a per-query cost that "
        "buys nothing and biases the concurrency measurement."
    )


def test_the_default_is_to_allow_inline_collection():
    """Engines opt OUT explicitly, so a new connector does not silently inherit a
    behaviour tuned for someone else's history table."""
    assert WarehouseConnector.INLINE_SERVER_TIME is True


@pytest.mark.parametrize("engine", ["databricks", "fabric"])
def test_the_measurement_path_does_not_query_history(engine, monkeypatch):
    """The whole point: `execute_query` must not call `get_server_timing`.

    If it does, the 15 s per-query cost returns, and so does the extra statement in
    the measured session.
    """
    from benchmark.connectors.registry import connector_class

    cls = connector_class(engine)
    called = []

    def _boom(self, *a, **k):
        called.append(True)
        raise AssertionError(
            f"{engine}.execute_query called get_server_timing on the measurement path"
        )

    monkeypatch.setattr(cls, "get_server_timing", _boom, raising=True)

    conn = _bare(cls, engine)

    result = conn.execute_query("SELECT 1")

    assert not called
    assert result.error_message is None, result.error_message


def test_the_statement_identifier_is_still_captured():
    """Deferring the lookup only works if reconcile can find the row afterwards.

    `databricks_reconcile_sql` keys on `statement_id`, so dropping the inline read
    while also dropping the identifier would lose server time instead of deferring
    it — a silent downgrade rather than an optimisation.
    """
    from benchmark.connectors.registry import connector_class

    conn = _bare(connector_class("databricks"), "databricks", query_id="01f19a70-dead-beef")

    result = conn.execute_query("SELECT 1")

    assert result.platform_query_id == "01f19a70-dead-beef"
    assert result.server_time_ms is None, "server time is reconcile's job now"


def _bare(cls, engine: str, query_id: str = "stub-statement-id"):
    """A connector instance with no connection, for exercising `execute_query` only.

    Built with ``__new__`` deliberately: ``__init__`` reads settings and would need
    credentials, and the code path under test does not depend on them. Attributes the
    query path touches are set explicitly, so a connector that grows a new dependency
    fails here loudly rather than being silently skipped.
    """
    conn = cls.__new__(cls)
    conn.platform = engine
    conn.config = {}
    conn.cursor = _StubCursor(query_id=query_id)
    conn._last_statement_id = None
    if engine == "fabric":
        conn.component = "dw"  # set by __init__, recorded in QueryResult.metadata
        conn._schema = None  # qualification is skipped when unset
    return conn


class _StubCursor:
    """Minimal DB-API-ish cursor: enough for execute_query, no engine required."""

    def __init__(self, query_id: str | None = "stub-statement-id") -> None:
        self.query_id = query_id
        self.description = [("col", None)]

    def execute(self, sql, *args, **kwargs):
        return self

    def fetchall(self):
        return [(1,)]

    def fetchone(self):
        return (1,)

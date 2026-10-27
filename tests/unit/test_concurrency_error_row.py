"""Concurrency error-path row must carry every key the recorder reads.

An error-path dict missing a key (e.g. ``bytes_scanned`` or ``platform_query_id``)
makes the first errored query under load raise KeyError inside
``record_sql_result``, which aborts the whole level and loses every cell in it.
"""

from __future__ import annotations

from types import SimpleNamespace

from benchmark.exceptions import QueryExecutionError
from benchmark.sql_benchmark import concurrency

# Keys record_sql_result reads from each row dict (see _run_level) — the error
# path must supply all of them.
_RECORDER_KEYS = {
    "platform",
    "query_id",
    "block",
    "scale_factor",
    "concurrency",
    "run_number",
    "wall_clock_ms",
    "server_time_ms",
    "bytes_scanned",
    "platform_query_id",
    "row_count",
    "cost_usd",
    "cost_confidence",
    "error_message",
    "metadata",
}


def test_error_path_row_has_all_recorder_keys(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise QueryExecutionError("boom")

    # Force the except branch (execute failure) deterministically.
    monkeypatch.setattr(concurrency, "execute_with_timeout", _boom)
    query = SimpleNamespace(id="tpcds_q01", block="tpcds", sql="SELECT 1")
    cost = SimpleNamespace(cost_from_result=lambda _r: None, confidence="estimated")

    row = concurrency._execute_on(
        object(), cost, "snowflake", query, scale_factor=100, concurrency=100
    )

    missing = _RECORDER_KEYS - set(row)
    assert not missing, f"error row missing recorder keys: {missing}"
    assert row["error_message"]  # it IS recorded as an error, not dropped
    assert row["bytes_scanned"] is None and row["platform_query_id"] is None

"""BigQuery's queue wait sits outside its server time and is captured separately."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from benchmark.connectors.bigquery import BigQueryConnector


def _job(created: datetime, started: datetime, ended: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        created=created,
        started=started,
        ended=ended,
        job_id="job-1",
        schema=[],
        cache_hit=False,
        total_bytes_billed=0,
        total_bytes_processed=0,
        slot_millis=0,
        statement_type="SELECT",
        result=lambda: iter([]),
    )


def test_queue_is_creation_to_start_and_server_time_is_start_to_end():
    t0 = datetime(2026, 8, 1, tzinfo=UTC)
    job = _job(t0, t0 + timedelta(seconds=12), t0 + timedelta(seconds=14))
    conn = BigQueryConnector({})
    conn.client = SimpleNamespace(query=lambda _sql, job_config: job)

    res = conn.execute_query("SELECT 1")

    assert res.queue_ms == 12_000.0  # PENDING in the query queue
    assert res.server_time_ms == 2_000.0  # running
    assert res.platform_query_id == "job-1"

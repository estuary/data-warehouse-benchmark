"""BigQuery slot-sweep: fixed-baseline validation + committed-cost pricing."""

from __future__ import annotations

import pytest

from benchmark.compute.bigquery_reservation import (
    MIN_SLOTS,
    SLOT_INCREMENT,
    validate_slots,
)
from benchmark.connectors.base import QueryResult
from benchmark.cost.base import CostConfidence
from benchmark.cost.bigquery import BigQueryCostCollector


def _result(server_time_ms: float | None, **meta: int) -> QueryResult:
    return QueryResult(
        rows=[],
        columns=[],
        row_count=0,
        wall_clock_ms=server_time_ms or 0.0,
        server_time_ms=server_time_ms,
        metadata=dict(meta),
    )


def test_runtime_compute_maps_reservation_without_column_names(monkeypatch):
    # The BigQuery connector returns rows with EMPTY `columns`, so a name-based
    # lookup finds nothing; runtime_compute() returning {} would mislabel
    # reservation runs as 'on-demand' and price them on-demand. It must fall back
    # to positional (slot_capacity, edition).
    from benchmark.connectors.bigquery import BigQueryConnector

    conn = BigQueryConnector(config={"region": "us-central1", "project": "p"})
    monkeypatch.setattr(
        conn,
        "execute_query",
        lambda _sql: QueryResult(
            rows=[(300, "ENTERPRISE")],
            columns=[],  # BigQuery connector doesn't populate column names
            row_count=1,
            wall_clock_ms=0.0,
            server_time_ms=None,
            metadata={},
        ),
    )
    assert conn.runtime_compute() == {"slot_capacity": 300, "edition": "ENTERPRISE"}


def test_runtime_compute_empty_when_no_reservation(monkeypatch):
    from benchmark.connectors.bigquery import BigQueryConnector

    conn = BigQueryConnector(config={"region": "us-central1", "project": "p"})
    monkeypatch.setattr(
        conn,
        "execute_query",
        lambda _sql: QueryResult(
            rows=[], columns=[], row_count=0, wall_clock_ms=0.0, server_time_ms=None, metadata={}
        ),
    )
    assert conn.runtime_compute() == {}


@pytest.mark.parametrize("slots", [50, 100, 150, 200, 250, 300, 350, 400, 450, 500])
def test_sweep_steps_are_valid_fixed_baselines(slots: int):
    # The 50→500 step-50 sweep: every step is a multiple of 50.
    assert validate_slots(slots) == slots


@pytest.mark.parametrize("bad", [0, 25, 49, 75, 101, 199, -50])
def test_invalid_slot_counts_rejected(bad: int):
    with pytest.raises(ValueError, match="multiple of"):
        validate_slots(bad)


def test_increment_and_minimum_constants():
    assert SLOT_INCREMENT == 50
    assert MIN_SLOTS == 50


def test_committed_cost_is_slots_rate_time_and_proportional():
    # 200 slots for a 3600 s (1 h) query → 200 × $0.06 = $12.00, allocated by time.
    c = BigQueryCostCollector(tier="enterprise", reservation_slots=200)
    assert c.confidence == CostConfidence.PROPORTIONAL
    cost = c.cost_from_result(_result(server_time_ms=3_600_000))
    assert cost == pytest.approx(200 * 0.06 * 1.0)  # $12.00


def test_committed_cost_scales_linearly_with_slots():
    # Fixed capacity: doubling slots doubles the committed cost for the same time.
    small = BigQueryCostCollector(tier="enterprise", reservation_slots=100)
    big = BigQueryCostCollector(tier="enterprise", reservation_slots=200)
    r = _result(server_time_ms=60_000)
    assert big.cost_from_result(r) == pytest.approx(2 * small.cost_from_result(r))


def test_committed_falls_back_to_wall_clock_without_server_time():
    c = BigQueryCostCollector(tier="enterprise", reservation_slots=100)
    r = QueryResult(rows=[], columns=[], row_count=0, wall_clock_ms=3_600_000, server_time_ms=None)
    assert c.cost_from_result(r) == pytest.approx(100 * 0.06 * 1.0)


def test_consumed_enterprise_still_direct_and_slot_ms_based():
    # No reservation_slots ⇒ the consumption model, recorded DIRECT.
    c = BigQueryCostCollector(tier="enterprise")
    assert c.confidence == CostConfidence.DIRECT
    cost = c.cost_from_result(_result(server_time_ms=1000, slot_millis=3_600_000))
    assert cost == pytest.approx(0.06)  # 1 slot-hour


def test_on_demand_unchanged():
    c = BigQueryCostCollector(tier="on-demand")
    assert c.confidence == CostConfidence.DIRECT
    cost = c.cost_from_result(_result(server_time_ms=1000, total_bytes_billed=1024**4))
    assert cost == pytest.approx(6.25)  # 1 TiB


def test_errored_query_is_free():
    c = BigQueryCostCollector(tier="enterprise", reservation_slots=200)
    r = _result(server_time_ms=5000)
    r.error_message = "boom"
    assert c.cost_from_result(r) == 0.0

"""Concurrency band labeling: deterministic fallback matches the warm-path label."""

from __future__ import annotations

import pytest

from benchmark.sql_benchmark.concurrency import _label_from_warehouse_override


@pytest.mark.parametrize(
    ("warehouse", "expected"),
    [
        ("BENCHMARK_WH_XSMALL", "X-Small (Gen2)"),
        ("BENCHMARK_WH_SMALL", "Small (Gen2)"),
        ("BENCHMARK_WH_MEDIUM", "Medium (Gen2)"),
        ("BENCHMARK_WH_LARGE", "Large (Gen2)"),
        ("BENCHMARK_WH_XLARGE", "X-Large (Gen2)"),
    ],
)
def test_fallback_label_matches_warm_path(warehouse: str, expected: str):
    # Must equal the string compute_label_from_environment produces for the warm
    # size sweep, else the concurrency band series won't line up in the report.
    assert _label_from_warehouse_override({"warehouse": warehouse}) == expected


def test_no_override_returns_none():
    # Default warehouse (no --compute-size) — nothing to disambiguate.
    assert _label_from_warehouse_override({}) is None
    assert _label_from_warehouse_override({"warehouse": "SOME_OTHER_WH"}) is None


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        # pinned (min == max): fixed-size label used by the concurrency sweep
        (
            {"num_replicas": 3, "min_replica_memory_gb": 120, "max_replica_memory_gb": 120},
            "3x120GB",
        ),
        ({"num_replicas": 2, "min_replica_memory_gb": 32, "max_replica_memory_gb": 32}, "2x32GB"),
        # unpinned range labels as a range so an autoscaling run is never
        # mistaken for a fixed size
        (
            {"num_replicas": 1, "min_replica_memory_gb": 16, "max_replica_memory_gb": 120},
            "1x16-120GB",
        ),
        # SQL-only fallback (no Cloud API): replicas + max only
        ({"replicas": 2, "max_replica_memory_gb": 32}, "2x32GB"),
        # nothing discernible
        ({}, None),
    ],
)
def test_clickhouse_label_from_replica_scaling(actual, expected):
    from benchmark.connectors.base import compute_label_from_environment

    env = {"platform": "clickhouse", "actual": actual}
    assert compute_label_from_environment(env) == expected


# ── achieved vs requested concurrency ────────────────────────────────────────


def test_a_degraded_level_records_what_it_actually_offered(monkeypatch, results_db):
    """When pre-warm cannot open every session the pass continues at the achieved
    count. Without recording that, the row claims a load never offered and nothing
    in the data reveals it — the level is the independent variable of the whole
    concurrency experiment."""
    from benchmark.sql_benchmark import concurrency as cc

    opened = 3  # asked for 5, got 3
    monkeypatch.setattr(
        cc, "_open_pool", lambda *a, **k: [(_FakeConn(), _FakeCost()) for _ in range(opened)]
    )

    cc._run_level(
        "motherduck",
        {},
        {},
        results_db,
        [_FakeQuery("tpcds_q01")],
        scale_factor=1,
        level=5,
        compute_label="Standard",
    )

    with results_db.connect() as con:
        meta = con.execute(
            "SELECT metadata, concurrency FROM sql_benchmark_results WHERE query_id='tpcds_q01'"
        ).fetchone()
    import json

    parsed = json.loads(meta[0]) if isinstance(meta[0], str) else meta[0]
    assert meta[1] == 5, "the requested level stays the recorded level"
    assert parsed["achieved_concurrency"] == opened
    assert parsed["requested_concurrency"] == 5


def test_a_full_level_stays_quiet(monkeypatch, results_db):
    """The marker must appear only when it matters, or it becomes noise that a
    reader learns to skip."""
    from benchmark.sql_benchmark import concurrency as cc

    monkeypatch.setattr(
        cc, "_open_pool", lambda *a, **k: [(_FakeConn(), _FakeCost()) for _ in range(2)]
    )

    cc._run_level(
        "motherduck",
        {},
        {},
        results_db,
        [_FakeQuery("tpcds_q02")],
        scale_factor=1,
        level=2,
        compute_label="Standard",
    )

    with results_db.connect() as con:
        raw = con.execute(
            "SELECT metadata FROM sql_benchmark_results WHERE query_id='tpcds_q02'"
        ).fetchone()[0]
    import json

    parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
    assert "achieved_concurrency" not in (parsed or {})


def test_a_tag_is_stamped_on_every_row(monkeypatch, results_db):
    """--tag tells replicate passes of one cell apart once latest-wins collapses them;
    it rides alongside the achieved/requested marker rather than replacing it."""
    import json

    from benchmark.sql_benchmark import concurrency as cc

    monkeypatch.setattr(
        cc, "_open_pool", lambda *a, **k: [(_FakeConn(), _FakeCost()) for _ in range(1)]
    )
    cc._run_level(
        "motherduck",
        {},
        {},
        results_db,
        [_FakeQuery("tpcds_q03")],
        scale_factor=1,
        level=2,
        compute_label="Standard",
        tag="cold-r1",
    )
    with results_db.connect() as con:
        raw = con.execute(
            "SELECT metadata FROM sql_benchmark_results WHERE query_id='tpcds_q03'"
        ).fetchone()[0]
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    assert parsed["tag"] == "cold-r1"
    assert parsed["achieved_concurrency"] == 1


class _FakeQuery:
    def __init__(self, qid: str) -> None:
        self.id = qid
        self.block = "tpcds"
        self.sql = "SELECT 1"


class _FakeConn:
    def disconnect(self) -> None: ...


class _FakeCost:
    confidence = "estimated"

    def cost_from_result(self, result) -> float:
        return 0.0

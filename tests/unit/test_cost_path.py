"""Validate the cost path: inline ESTIMATE -> post-run reconcile.

Two-stage model: the runner records an ESTIMATED inline cost, and
``benchmark reconcile`` upgrades it to the engine's authoritative tier
(PROPORTIONAL for Databricks/Redshift/Fabric; Snowflake stays ESTIMATED, with
attribution kept as a cross-check). Exercised with SQL-aware fake connectors —
no cloud engine.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from benchmark.analysis.reconcile import (
    RECONCILED_CONFIDENCE,
    _reconcile_databricks,
    _reconcile_fabric,
    _reconcile_redshift,
    reconcile_platform,
)
from benchmark.connectors.base import QueryResult
from benchmark.cost.base import CostConfidence
from benchmark.cost.databricks import DatabricksCostCollector
from benchmark.cost.fabric import FabricCostCollector
from benchmark.cost.redshift import RedshiftCostCollector
from benchmark.cost.snowflake import SnowflakeCostCollector


class _SqlAwareConnector:
    """Connector double: returns canned rows by SQL substring; may fail on one.

    Responses are matched in order, so list the MORE SPECIFIC substring first
    (e.g. ``SYS_SERVERLESS_USAGE`` before ``SYS_QUERY_HISTORY`` — the cost query
    mentions both tables).
    """

    def __init__(self, responses, *, fail_on=None, config=None) -> None:
        self._responses = responses
        self._fail_on = fail_on
        self.config = config or {}

    def execute_query(self, sql: str) -> QueryResult:
        if self._fail_on and self._fail_on in sql:
            return QueryResult(
                rows=[],
                columns=[],
                row_count=0,
                wall_clock_ms=1.0,
                error_message="permission denied",
            )
        for substr, rows in self._responses:
            if substr in sql:
                return QueryResult(
                    rows=list(rows), columns=[], row_count=len(rows), wall_clock_ms=1.0
                )
        return QueryResult(rows=[], columns=[], row_count=0, wall_clock_ms=1.0)

    def __enter__(self) -> _SqlAwareConnector:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _row(db, pqid: str) -> dict:
    with db.connect() as con:
        st, cost, conf = con.execute(
            "SELECT server_time_ms, cost_usd, cost_confidence FROM sql_benchmark_results "
            "WHERE platform_query_id = ?",
            [pqid],
        ).fetchone()
    return {"server_time_ms": st, "cost_usd": cost, "cost_confidence": conf}


def _record(db, **kw) -> None:
    base = dict(
        block="tpcds",
        scale_factor=1000,
        run_type="warm",
        run_number=1,
        wall_clock_ms=10.0,
        row_count=1,
        error_message=None,
    )
    base.update(kw)
    db.record_sql_result(**base)


# ── inline confidence: estimate until reconciled ────────────────────────────

_RECONCILED = {
    "databricks": (DatabricksCostCollector(), CostConfidence.PROPORTIONAL),
    "redshift": (RedshiftCostCollector(), CostConfidence.PROPORTIONAL),
    "fabric": (FabricCostCollector(capacity="f64"), CostConfidence.PROPORTIONAL),
}


@pytest.mark.parametrize("platform", list(_RECONCILED))
def test_inline_cost_is_estimated_until_reconciled(platform):
    collector, tier = _RECONCILED[platform]
    # what the runner records live is an estimate...
    assert collector.confidence == CostConfidence.ESTIMATED
    # ...and the tier reconcile will upgrade it to is declared consistently.
    assert tier == collector.RECONCILED_CONFIDENCE
    assert RECONCILED_CONFIDENCE[platform] == tier


def test_snowflake_is_rate_priced_and_stays_estimated():
    """Snowflake is priced at list rate x engine time; reconcile records attribution
    as a cross-check and never upgrades the tier."""
    collector = SnowflakeCostCollector()
    assert collector.confidence == collector.RECONCILED_CONFIDENCE == CostConfidence.ESTIMATED
    assert "snowflake" not in RECONCILED_CONFIDENCE
    assert collector.credit_price == 2.00, "Standard: the cheapest edition that runs it"


def test_snowflake_inline_cost_is_zero_placeholder():
    r = QueryResult(rows=[], columns=[], row_count=0, wall_clock_ms=1234.0)
    assert SnowflakeCostCollector().cost_from_result(r) == 0.0


def test_proportional_inline_cost_tracks_wallclock():
    c = DatabricksCostCollector(size="medium")  # 24 DBU/hr x default $/DBU
    one_hour = QueryResult(rows=[], columns=[], row_count=0, wall_clock_ms=3_600_000)
    expected = 24 * DatabricksCostCollector.DEFAULT_DBU_PRICE_USD
    assert c.cost_from_result(one_hour) == pytest.approx(expected)


# ── Snowflake: attribution recorded as a cross-check, 6-8h after the run ─────


def test_snowflake_reconcile_records_attribution_and_keeps_the_cost(results_db):
    _record(
        results_db,
        platform="snowflake",
        query_id="q1",
        server_time_ms=5.0,
        cost_usd=0.0001,
        cost_confidence="estimated",
        platform_query_id="sfqid-1",
        compute_label="X-Small (Gen2)",
    )
    assert results_db.rows_to_reconcile("snowflake", 1000, "warm") == ["sfqid-1"]

    conn = _SqlAwareConnector([("QUERY_ATTRIBUTION_HISTORY", [("sfqid-1", 2.0)])])
    summary = reconcile_platform(
        "snowflake",
        conn,
        results_db,
        scale_factor=1000,
        run_type="warm",
        window_iso=("2026-06-23T00:00:00", "2026-06-23T12:00:00"),
    )
    assert summary["updated"] == 1
    row = _row(results_db, "sfqid-1")
    assert row["cost_usd"] == pytest.approx(0.0001), "the engine-time price stays"
    assert row["cost_confidence"] == "estimated"
    assert row["server_time_ms"] == 5.0  # inline value preserved (COALESCE)
    with results_db.connect() as con:
        credits = con.execute(
            "SELECT json_extract_string(metadata, '$.attributed_credits') FROM sql_benchmark_results"
        ).fetchone()[0]
    assert float(credits) == pytest.approx(2.0)
    # Recorded credits take the row out of the candidates; its estimated tier does not
    # put it back.
    assert results_db.rows_to_reconcile("snowflake", 1000, "warm") == []


# ── Redshift: server time always; cost degrades without superuser ───────────


def test_redshift_reconcile_degrades_without_superuser():
    conn = _SqlAwareConnector(
        [("SYS_QUERY_HISTORY", [(101, 250.0)])], fail_on="SYS_SERVERLESS_USAGE"
    )
    out = _reconcile_redshift(conn, ["101"], window_iso=("a", "b"), rates={})
    assert len(out) == 1
    assert out[0].platform_query_id == "101"
    assert out[0].server_time_ms == 250.0  # owner-visible: still back-filled
    assert out[0].cost_usd is None  # superuser-only usage view inaccessible


def test_redshift_reconcile_prices_rpu_hours():
    conn = _SqlAwareConnector(
        [
            ("SYS_SERVERLESS_USAGE", [(101, 0.5)]),  # cost query (mentions both); specific first
            ("SYS_QUERY_HISTORY", [(101, 250.0)]),  # server-time query
        ]
    )
    out = _reconcile_redshift(
        conn, ["101"], window_iso=("a", "b"), rates={"redshift_rpu_usd_per_hour": 0.375}
    )
    assert out[0].server_time_ms == 250.0
    assert out[0].cost_usd == pytest.approx(0.5 * 0.375)
    # The idle cap starts from the billed share, so reconcile records it.
    assert out[0].metadata == {"billed_share_usd": pytest.approx(0.5 * 0.375)}


def test_reconcile_server_time_only_keeps_cost_estimated(results_db):
    # Redshift, no superuser: server time back-fills, cost stays an estimate.
    _record(
        results_db,
        platform="redshift",
        query_id="q1",
        server_time_ms=None,
        cost_usd=0.01,
        cost_confidence="estimated",
        platform_query_id="42",
    )
    conn = _SqlAwareConnector([("SYS_QUERY_HISTORY", [(42, 99.0)])], fail_on="SYS_SERVERLESS_USAGE")
    reconcile_platform(
        "redshift",
        conn,
        results_db,
        scale_factor=1000,
        run_type="warm",
        window_iso=("a", "b"),
        rates={},
    )
    row = _row(results_db, "42")
    assert row["server_time_ms"] == 99.0  # authoritative
    assert row["cost_confidence"] == "estimated"  # NOT upgraded
    assert row["cost_usd"] == 0.01  # estimate preserved


# ── Fabric: match the label column; CPU time for the window split ───────────

_WINDOW = ("2026-09-26T00:00:00+00:00", "2026-09-27T00:00:00+00:00")


def test_fabric_reconcile_matches_the_label_column():
    start = datetime(2026, 9, 26, 12, 0, 0)
    end = datetime(2026, 9, 26, 12, 0, 1)
    conn = _SqlAwareConnector(
        [
            (
                "queryinsights",
                [
                    # rows are (label, elapsed_ms, cpu_ms, start, end) — the label
                    # column, NOT command text with an embedded comment.
                    ("bmq_abc123def4", 120.0, 50.0, start, end),
                    ("bmq_other00000", 9.0, 1.0, start, end),
                ],
            )
        ]
    )
    out = _reconcile_fabric(conn, ["bmq_abc123def4"], window_iso=_WINDOW, rates={})
    assert len(out) == 1
    assert out[0].platform_query_id == "bmq_abc123def4"
    assert out[0].server_time_ms == 120.0
    # No per-statement price: a statement's cost depends on everything that shared
    # its capacity window, so reconcile_platform re-prices the cell afterwards.
    assert out[0].cost_usd is None
    assert out[0].metadata == {
        "cpu_ms": 50.0,
        "start_time": "2026-09-26T12:00:00",
        "end_time": "2026-09-26T12:00:01",
    }


def test_fabric_reconcile_prices_the_cell_by_window_after_back_filling(results_db):
    """End to end: two statements of one burst get engine and CPU time, then the
    window prices them as one capacity, split by CPU share."""
    for qid, label, end_s in (("q1", "bmq_1", 4.0), ("q2", "bmq_2", 10.0)):
        _record(
            results_db,
            platform="fabric",
            query_id=qid,
            scale_factor=100,
            run_type="concurrency",
            concurrency=2,
            wall_clock_ms=4000.0 if qid == "q1" else 10_000.0,
            server_time_ms=None,
            cost_usd=0.5,
            cost_confidence="estimated",
            platform_query_id=label,
            provenance_id="00000000-0000-0000-0000-000000000001",
            compute_label="F16",
        )
        with results_db.connect() as con:
            con.execute(
                "UPDATE sql_benchmark_results SET created_at = TIMESTAMP '2026-09-26 12:00:00'"
                " + to_microseconds(?) WHERE query_id = ?",
                [int(end_s * 1e6), qid],
            )
    conn = _SqlAwareConnector(
        [
            (
                "queryinsights",
                [("bmq_1", 3900.0, 1000.0, None, None), ("bmq_2", 9900.0, 3000.0, None, None)],
            )
        ]
    )
    summary = reconcile_platform(
        "fabric", conn, results_db, scale_factor=100, run_type="concurrency", window_iso=_WINDOW
    )

    assert summary["updated"] == 2
    window_usd = 9.9 * 2.88 / 3600.0  # 0.1-10 s of F16 held by engine time; 4 CPU-s inside
    assert summary["priced_usd"] == pytest.approx(window_usd, abs=1e-4)
    q1, q2 = _row(results_db, "bmq_1"), _row(results_db, "bmq_2")
    assert q1["cost_usd"] == pytest.approx(window_usd / 4, rel=1e-6)
    assert q2["cost_usd"] == pytest.approx(window_usd * 3 / 4, rel=1e-6)
    assert q1["cost_confidence"] == q2["cost_confidence"] == "proportional"
    assert q1["server_time_ms"] == 3900.0
    # Back-filled CPU time takes the rows out of the candidate list.
    assert results_db.rows_to_reconcile("fabric", 100, "concurrency", missing_cpu=True) == []


def test_fabric_rows_without_cpu_stay_candidates_while_history_lasts(results_db):
    """A reconciled row missing CPU time is a candidate for the CPU back-fill, but
    only within the history's 30 days: older rows can never get it."""
    for qid, age_days in (("recent", 1), ("old", 40)):
        _record(
            results_db,
            platform="fabric",
            query_id=qid,
            server_time_ms=100.0,
            cost_usd=0.01,
            cost_confidence="proportional",
            platform_query_id=f"bmq_{qid}",
            compute_label="F16",
        )
        with results_db.connect() as con:
            con.execute(
                "UPDATE sql_benchmark_results"
                " SET created_at = CAST(now() AS TIMESTAMP) - to_days(?) WHERE query_id = ?",
                [age_days, qid],
            )
    assert results_db.rows_to_reconcile("fabric", 1000, "warm") == []
    assert results_db.rows_to_reconcile("fabric", 1000, "warm", missing_cpu=True) == ["bmq_recent"]
    assert results_db.unreconciled_cells("fabric", missing_cpu=True) == [(1000, "warm")]


# ── Databricks: warehouse id derived from the HTTP path ─────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/sql/1.0/warehouses/abc123", "abc123"),
        ("/sql/1.0/warehouses/abc123/", "abc123"),
        ("sql/1.0/warehouses/def456", "def456"),
    ],
)
def test_databricks_warehouse_id_from_http_path(path, expected):
    from benchmark.connectors.databricks import DatabricksConnector

    assert DatabricksConnector._warehouse_id_from_http_path(path) == expected


# ── documented rates (validated against vendor pricing, 2026) ───────────────


def test_snowflake_credit_prices_match_on_demand_list():
    assert SnowflakeCostCollector("standard").credit_price == 2.00
    assert SnowflakeCostCollector("enterprise").credit_price == 3.00
    assert SnowflakeCostCollector("business_critical").credit_price == 4.00


def test_databricks_dbu_schedule_matches_docs():
    # Real SQL-warehouse schedule, not a power-of-2 doubling.
    assert DatabricksCostCollector(size="2x-small").dbu_per_hour == 4
    assert DatabricksCostCollector(size="small").dbu_per_hour == 12
    assert DatabricksCostCollector(size="medium").dbu_per_hour == 24
    assert DatabricksCostCollector(size="large").dbu_per_hour == 40


def test_motherduck_tier_table_us_rates():
    from benchmark.cost.motherduck import MotherDuckCostCollector

    assert MotherDuckCostCollector("pulse").rate_usd_per_second == pytest.approx(0.60 / 3600)
    assert MotherDuckCostCollector("standard").rate_usd_per_second == pytest.approx(2.40 / 3600)
    assert MotherDuckCostCollector("giga").rate_usd_per_second == pytest.approx(24.00 / 3600)


def test_clickhouse_priced_per_compute_unit():
    from benchmark.cost.clickhouse import ClickHouseCostCollector

    # 16 vCPU = 8 compute units; Scale tier runs 2 replicas; $0.2985/unit-hr.
    c = ClickHouseCostCollector(vcpus=16, replicas=2)
    assert c.usd_per_hour == pytest.approx(8 * 2 * 0.2985)


def test_redshift_inline_does_not_apply_a_per_query_60s_minimum():
    """The 60 s floor is per compute *session*, not per query.

    A benchmark pass runs hundreds of queries back-to-back inside one session, so a
    per-query floor would charge hundreds of separate wake-ups and flatten every
    un-reconciled row onto a handful of values, inverting the size-sweep cost
    curve. ``tests/unit/test_recost.py`` covers the rest of the billing-floor rules.
    """
    c = RedshiftCostCollector()
    one_ms = QueryResult(rows=[], columns=[], row_count=0, wall_clock_ms=1.0)
    two_min = QueryResult(rows=[], columns=[], row_count=0, wall_clock_ms=120_000.0)
    # Both bill their true elapsed time — price stays proportional to duration.
    assert c.cost_from_result(one_ms) == pytest.approx(0.001 * c.usd_per_second_baseline)
    assert c.cost_from_result(two_min) == pytest.approx(120.0 * c.usd_per_second_baseline)


def test_bigquery_enterprise_is_slot_based_not_bytes():
    from benchmark.cost.bigquery import BigQueryCostCollector

    r = QueryResult(
        rows=[],
        columns=[],
        row_count=0,
        wall_clock_ms=1.0,
        metadata={"slot_millis": 3_600_000, "total_bytes_billed": 10**12},
    )
    # enterprise: 1 slot-hour x $0.06, ignoring bytes
    assert BigQueryCostCollector("enterprise").cost_from_result(r) == pytest.approx(0.06)
    # on-demand: bytes x $6.25/TiB, ignoring slots
    assert BigQueryCostCollector("on-demand").cost_from_result(r) == pytest.approx(
        (10**12 / (1024**4)) * 6.25
    )


# ── queue time: Databricks and Redshift report execution only ──────────────


def test_redshift_reconcile_back_fills_queue_and_compile():
    conn = _SqlAwareConnector(
        [("SYS_QUERY_HISTORY", [(101, 250.0, 4000.0, 12.0)])], fail_on="SYS_SERVERLESS_USAGE"
    )
    out = _reconcile_redshift(conn, ["101"], window_iso=("a", "b"), rates={})
    assert out[0].server_time_ms == 250.0
    assert out[0].queue_ms == 4000.0 and out[0].compile_ms == 12.0


def test_databricks_reconcile_back_fills_queue_and_compile():
    # No warehouse id on the connector: reconcile reads it from the history.
    conn = _SqlAwareConnector([("system.query.history", [("sid-1", 900.0, 0.02, 58000.0, 30.0)])])
    out = _reconcile_databricks(conn, ["sid-1"], window_iso=("a", "b"), rates={})
    assert out[0].server_time_ms == 900.0 and out[0].cost_usd == 0.02
    assert out[0].queue_ms == 58000.0 and out[0].compile_ms == 30.0
    # The hour re-split starts from the billed share, so reconcile records it.
    assert out[0].metadata == {"billed_share_usd": 0.02}


def test_databricks_reconcile_batches_statement_ids(monkeypatch: pytest.MonkeyPatch):
    import benchmark.analysis.reconcile as reconcile

    monkeypatch.setattr(reconcile, "DATABRICKS_RECONCILE_BATCH", 2)
    seen: list[str] = []

    class _Recording:
        def __init__(self) -> None:
            self.config: dict = {}

        def execute_query(self, sql: str):
            seen.append(sql)
            ids = [i for i in ("sid-1", "sid-2", "sid-3") if f"'{i}'" in sql]
            rows = [(i, 1.0, 0.01, 0.0, 0.0) for i in ids]
            return QueryResult(rows=rows, columns=[], row_count=len(rows), wall_clock_ms=1.0)

    out = _reconcile_databricks(
        _Recording(), ["sid-1", "sid-2", "sid-3"], window_iso=("a", "b"), rates={}
    )
    assert len(seen) == 2
    assert sorted(m.platform_query_id for m in out) == ["sid-1", "sid-2", "sid-3"]


def test_queue_back_fill_keeps_reconciled_rows_as_candidates(results_db):
    # A row reconciled before queue time was captured is picked up again, and
    # drops out once queue_ms lands.
    _record(
        results_db,
        platform="databricks",
        query_id="q1",
        server_time_ms=900.0,
        cost_usd=0.02,
        cost_confidence="proportional",
        platform_query_id="sid-1",
    )
    assert results_db.rows_to_reconcile("databricks", 1000, "warm") == []
    assert results_db.rows_to_reconcile("databricks", 1000, "warm", missing_queue=True) == ["sid-1"]
    # The lookback must reach back to that row too, or the history lookup
    # searches only the default window and matches nothing.
    assert results_db.oldest_reconcile_candidate("databricks", 1000, "warm") is None
    assert (
        results_db.oldest_reconcile_candidate("databricks", 1000, "warm", missing_queue=True)
        is not None
    )
    results_db.update_reconciled_metrics(
        "sid-1", server_time_ms=None, cost_usd=None, cost_confidence=None, queue_ms=0.0
    )
    assert results_db.rows_to_reconcile("databricks", 1000, "warm", missing_queue=True) == []

"""Re-cost repair passes.

A BigQuery reservation row priced through the collector's **on-demand** branch
(``total_bytes_billed`` x $6.25/TiB) gets a cost that ignores reservation size,
because bytes scanned do not vary with it: a slot sweep's cost axis comes out
identical at every size, which reads as a finding ("cost is flat across
reservation size") but is an artifact. It happens when ``capture-cost-config``
runs after a sweep has torn its reservation down, so ``cost_config.json`` records
``tier: "on-demand"``. Across 21,619 sweep rows, bytes pricing overstates spend
~15.7x ($3,661 vs $234 on slot time).

The invariant these tests pin: **a row whose ``compute_label`` says a reservation
was live is never priced on bytes.**
"""

from __future__ import annotations

import pytest

from benchmark.analysis.recost import (
    clear_fabricated_redshift_estimates,
    mixed_confidence_cells,
    recost_bigquery_reservations,
)
from benchmark.connectors.bigquery import BigQueryConnector
from benchmark.cost.bigquery import BYTES_PER_TIB, BigQueryCostCollector
from benchmark.cost.clickhouse import ClickHouseCostCollector
from benchmark.cost.databricks import DatabricksCostCollector
from benchmark.cost.fabric import FabricCostCollector
from benchmark.cost.motherduck import MotherDuckCostCollector
from benchmark.cost.redshift import RedshiftCostCollector

RATE = BigQueryCostCollector.ENTERPRISE_USD_PER_SLOT_HOUR


def _record(db, *, label, slot_millis, bytes_billed, cost, error=None, qid="tpcds_q01"):
    """Insert one BigQuery row carrying both pricing inputs in its metadata."""
    db.record_sql_result(
        platform="bigquery",
        query_id=qid,
        block="tpcds",
        scale_factor=1000,
        run_type="warm",
        run_number=1,
        wall_clock_ms=1000.0,
        server_time_ms=900.0,
        row_count=1,
        cost_usd=cost,
        cost_confidence="direct",
        error_message=error,
        metadata={"slot_millis": slot_millis, "total_bytes_billed": bytes_billed},
        compute_label=label,
    )


def _cost_of(db, qid):
    with db.connect() as con:
        return con.execute(
            "SELECT cost_usd FROM sql_benchmark_results WHERE query_id = ?", [qid]
        ).fetchone()[0]


def test_reservation_rows_are_repriced_from_consumed_slot_time(results_db):
    # 3.6e6 slot-ms = 1 slot-hour, so the expected price is exactly one rate unit.
    _record(
        results_db,
        label="ENTERPRISE / 800 slots",
        slot_millis=3_600_000,
        bytes_billed=10 * BYTES_PER_TIB,
        cost=10 * BigQueryCostCollector.ON_DEMAND_USD_PER_TIB,  # the wrong, bytes-priced value
        qid="tpcds_q01",
    )

    summary = recost_bigquery_reservations(results_db)

    assert summary["rows"] == 1
    assert _cost_of(results_db, "tpcds_q01") == RATE
    # The bytes-priced value was 62.5; the correction must be a real change.
    assert summary["cost_before_usd"] > summary["cost_after_usd"]


def test_on_demand_rows_keep_their_bytes_pricing(results_db):
    on_demand_cost = 2.0 * BigQueryCostCollector.ON_DEMAND_USD_PER_TIB
    _record(
        results_db,
        label="on-demand",
        slot_millis=3_600_000,
        bytes_billed=2 * BYTES_PER_TIB,
        cost=on_demand_cost,
        qid="tpcds_q02",
    )

    recost_bigquery_reservations(results_db)

    # Priced on bytes and must stay there — on-demand billing really is per-TiB.
    assert _cost_of(results_db, "tpcds_q02") == on_demand_cost


def test_errored_reservation_rows_price_to_zero(results_db):
    _record(
        results_db,
        label="ENTERPRISE / 200 slots",
        slot_millis=0,
        bytes_billed=0,
        cost=0.0,
        error="query timed out",
        qid="tpcds_q03",
    )

    recost_bigquery_reservations(results_db)

    assert _cost_of(results_db, "tpcds_q03") == 0.0


def test_rows_missing_slot_millis_are_left_untouched(results_db):
    """A missing measurement must never be silently converted into a $0 one."""
    results_db.record_sql_result(
        platform="bigquery",
        query_id="tpcds_q04",
        block="tpcds",
        scale_factor=1000,
        run_type="warm",
        run_number=1,
        wall_clock_ms=1000.0,
        server_time_ms=900.0,
        row_count=1,
        cost_usd=1.25,
        cost_confidence="direct",
        error_message=None,
        metadata={"total_bytes_billed": 123},  # no slot_millis recorded
        compute_label="ENTERPRISE / 400 slots",
    )

    summary = recost_bigquery_reservations(results_db)

    assert summary["rows_without_slot_millis"] == 1
    assert _cost_of(results_db, "tpcds_q04") == 1.25


def test_recost_is_idempotent(results_db):
    _record(
        results_db,
        label="ENTERPRISE / 600 slots",
        slot_millis=7_200_000,
        bytes_billed=5 * BYTES_PER_TIB,
        cost=99.0,
        qid="tpcds_q05",
    )

    first = recost_bigquery_reservations(results_db)
    after_first = _cost_of(results_db, "tpcds_q05")
    second = recost_bigquery_reservations(results_db)

    assert after_first == _cost_of(results_db, "tpcds_q05") == 2 * RATE
    # Re-running recomputes from immutable metadata, so the "after" total is stable.
    assert first["cost_after_usd"] == second["cost_after_usd"]


def test_dry_run_reports_without_writing(results_db):
    _record(
        results_db,
        label="ENTERPRISE / 1000 slots",
        slot_millis=3_600_000,
        bytes_billed=8 * BYTES_PER_TIB,
        cost=50.0,
        qid="tpcds_q06",
    )

    summary = recost_bigquery_reservations(results_db, dry_run=True)

    assert summary["dry_run"] is True
    assert summary["cost_after_usd"] == RATE
    assert _cost_of(results_db, "tpcds_q06") == 50.0  # unchanged on disk


def test_reservation_compute_kwargs_never_select_the_on_demand_branch():
    """The introspection translator must map a live reservation to `enterprise`.

    This is the upstream half of the same hazard: if `cost_kwargs_from_compute`
    reports on-demand while a reservation is live, every row it prices is wrong
    at the source, before any repair pass can see it.
    """
    kwargs = BigQueryConnector.cost_kwargs_from_compute(
        {"slot_capacity": 800, "edition": "ENTERPRISE"}
    )

    assert kwargs["tier"] == "enterprise"
    # Carried for provenance so a per-size entry says which size it describes.
    assert kwargs["slot_capacity"] == 800

    collector = BigQueryCostCollector(tier=kwargs["tier"])
    assert collector.tier == "enterprise"

    # No reservation live -> on-demand is the honest answer.
    assert BigQueryConnector.cost_kwargs_from_compute({}) == {"tier": "on-demand"}


# ── no per-query billing floor ───────────────────────────────────────────────
#
# Redshift Serverless and Fabric both bill a 60-second minimum. That minimum
# covers a compute *session* / capacity *period* that wakes to serve work — one
# wake-up amortized across every query that follows. Applying it per query
# asserts hundreds of separate wake-ups in a back-to-back benchmark pass and
# collapses every un-reconciled row onto a single flat value, which can make a
# smaller size (Redshift 64 RPU) look more expensive than a larger one (128 RPU).


@pytest.mark.parametrize(
    "collector",
    [
        RedshiftCostCollector(baseline_rpu=64),
        FabricCostCollector(),
        ClickHouseCostCollector(),
        DatabricksCostCollector(),
        MotherDuckCostCollector(),
    ],
    ids=["redshift", "fabric", "clickhouse", "databricks", "motherduck"],
)
def test_no_collector_applies_a_per_query_billing_floor(collector):
    """A 100 ms query must cost ~1/10th of a 1 s query — not the same flat value.

    Any per-query minimum makes short queries indistinguishable in price, which
    destroys exactly the signal a size sweep is trying to measure.
    """
    short = collector.estimate_cost(100.0)
    long = collector.estimate_cost(1000.0)

    assert short < long, f"{type(collector).__name__} flattens short queries onto a floor"
    assert long == pytest.approx(short * 10, rel=1e-6)


def test_redshift_estimate_scales_linearly_with_elapsed_time():
    c = RedshiftCostCollector(baseline_rpu=64)
    # 64 RPU x $0.375/RPU-hr over 1 s. A per-query 60 s floor would make it a flat $0.40.
    assert c.estimate_cost(1000.0) == pytest.approx(64 * 0.375 / 3600.0)
    assert c.estimate_cost(1000.0) < 0.40


def test_voiding_redshift_estimates_nulls_cost_and_preserves_the_original(results_db):
    results_db.record_sql_result(
        platform="redshift",
        query_id="dsb_q64",
        block="dsb",
        scale_factor=10,
        run_type="warm",
        run_number=1,
        wall_clock_ms=355.0,
        server_time_ms=355.2,
        row_count=1,
        cost_usd=0.40,  # the fabricated 60s-floor value
        cost_confidence="estimated",
        error_message=None,
        metadata={"query_id": "10965225"},
        compute_label="64 RPU",
    )

    summary = clear_fabricated_redshift_estimates(results_db)

    assert summary["rows_voided"] == 1
    assert summary["fabricated_usd_removed"] == pytest.approx(0.40)
    with results_db.connect() as con:
        cost, conf, meta = con.execute(
            "SELECT cost_usd, cost_confidence, metadata FROM sql_benchmark_results"
        ).fetchone()
    # Unknown, not zero — a voided row must fall out of sum()/median() aggregates.
    assert cost is None
    assert conf is None
    # ...but the correction stays auditable and reversible.
    assert '"voided_cost_usd":0.4' in meta.replace(" ", "")


def test_voiding_leaves_reconciled_redshift_rows_alone(results_db):
    results_db.record_sql_result(
        platform="redshift",
        query_id="dsb_q01",
        block="dsb",
        scale_factor=10,
        run_type="warm",
        run_number=1,
        wall_clock_ms=355.0,
        server_time_ms=355.2,
        row_count=1,
        cost_usd=0.0127,
        cost_confidence="proportional",
        error_message=None,
        metadata={},
        compute_label="64 RPU",
    )

    clear_fabricated_redshift_estimates(results_db)

    with results_db.connect() as con:
        cost, conf = con.execute(
            "SELECT cost_usd, cost_confidence FROM sql_benchmark_results"
        ).fetchone()
    assert cost == pytest.approx(0.0127)
    assert conf == "proportional"


def test_mixed_confidence_cells_flags_a_contaminated_cell(results_db):
    for conf, cost in (("proportional", 0.0127), ("estimated", 0.40)):
        results_db.record_sql_result(
            platform="redshift",
            query_id=f"dsb_{conf}",
            block="dsb",
            scale_factor=10,
            run_type="warm",
            run_number=1,
            wall_clock_ms=355.0,
            server_time_ms=355.2,
            row_count=1,
            cost_usd=cost,
            cost_confidence=conf,
            error_message=None,
            metadata={},
            compute_label="64 RPU",
        )

    cells = mixed_confidence_cells(results_db)

    assert len(cells) == 1
    assert cells[0]["compute_label"] == "64 RPU"
    assert set(cells[0]["tiers"].split("+")) == {"proportional", "estimated"}


# ── cost must VARY with compute size ────────────────────────────────────────
#
# A collector built at constructor defaults (an empty cost_config, or a session
# pool whose collectors are not size-aware) prices every size at one rate, so cost
# stops being a function of compute size: ClickHouse would imply $4.776/hr across
# every replica config and MotherDuck $2.40/hr across every tier.

RATE_CONFIG = {
    "platforms": {
        "clickhouse": {
            "sizes": {
                "3x8GB": {"vcpus": 2, "replicas": 3},
                "3x120GB": {"vcpus": 30, "replicas": 3},
            }
        },
        "motherduck": {"sizes": {"Pulse": {"tier": "pulse"}, "Giga": {"tier": "giga"}}},
        "fabric": {"sizes": {"F2": {"capacity": "f2"}, "F128": {"capacity": "f128"}}},
    }
}


@pytest.mark.parametrize(
    ("platform", "small", "large", "expected_ratio"),
    [
        # ClickHouse: (vcpus/2) x replicas x $0.2985 -> 3x120GB is 15x 3x8GB.
        ("clickhouse", "3x8GB", "3x120GB", 15.0),
        # MotherDuck: giga $24.00/hr vs pulse $0.60/hr -> 40x.
        ("motherduck", "Pulse", "Giga", 40.0),
        # Fabric: F128 $23.04/hr vs F2 $0.36/hr -> 64x.
        ("fabric", "F2", "F128", 64.0),
    ],
)
def test_cost_varies_with_compute_size(platform, small, large, expected_ratio):
    """A bigger compute size must cost more per unit time. Equal rates mean the
    collector was built at constructor defaults and the size axis is dead."""
    from benchmark.cost.discovery import collector_for

    cheap = collector_for(platform, RATE_CONFIG, small).estimate_cost(1000.0)
    dear = collector_for(platform, RATE_CONFIG, large).estimate_cost(1000.0)

    assert cheap > 0
    assert dear > cheap, f"{platform}: {large} priced no higher than {small}"
    assert dear / cheap == pytest.approx(expected_ratio, rel=1e-6)


def test_rate_recost_leaves_reconciled_rows_alone(results_db):
    """Databricks mixes tiers: proportional rows come from system.billing.usage and
    are authoritative. Overwriting them with a rate model would degrade them."""
    from benchmark.analysis.recost import recost_rate_priced_rows

    for conf, cost in (("proportional", 0.0042), ("estimated", 0.0500)):
        results_db.record_sql_result(
            platform="databricks",
            query_id=f"tpcds_{conf}",
            block="tpcds",
            scale_factor=100,
            run_type="concurrency",
            run_number=1,
            wall_clock_ms=1000.0,
            server_time_ms=900.0,
            row_count=1,
            cost_usd=cost,
            cost_confidence=conf,
            error_message=None,
            metadata={},
            concurrency=10,
            compute_label="Medium",
        )

    cfg = {"platforms": {"databricks": {"sizes": {"Medium": {"size": "medium"}}}}}
    recost_rate_priced_rows(results_db, cfg, platforms=("databricks",))

    with results_db.connect() as con:
        kept = con.execute(
            "SELECT cost_usd FROM sql_benchmark_results WHERE query_id = 'tpcds_proportional'"
        ).fetchone()[0]
        repriced = con.execute(
            "SELECT cost_usd FROM sql_benchmark_results WHERE query_id = 'tpcds_estimated'"
        ).fetchone()[0]

    assert kept == pytest.approx(0.0042), "reconciled row must not be overwritten"
    # Medium = 24 DBU/hr x $0.70 -> $16.80/hr over its 0.9 s of engine time.
    assert repriced == pytest.approx(16.80 * 0.9 / 3600.0, rel=1e-6)


# ── memory-witness audit ─────────────────────────────────────────────────────
#
# ClickHouse caps a query's memory at 0.90 x the replica's RAM, so "maximum: N GiB"
# in an OOM message is an engine-emitted witness of the size that was actually live.
# Comparing it against the stamped label catches rows recorded mid-resize.


def _ch_oom_row(db, *, label, ceiling_gib, qid, scale=1000):
    db.record_sql_result(
        platform="clickhouse",
        query_id=qid,
        block="tpcds",
        scale_factor=scale,
        run_type="warm",
        run_number=1,
        wall_clock_ms=5000.0,
        server_time_ms=4900.0,
        row_count=None,
        cost_usd=0.0,
        cost_confidence="estimated",
        error_message=(
            f"Memory limit (total) exceeded: would use 30.00 GiB, maximum: {ceiling_gib} GiB"
        ),
        metadata={},
        compute_label=label,
    )


def test_memory_witness_flags_a_label_that_contradicts_the_oom_ceiling(results_db):
    from benchmark.analysis.recost import mislabelled_by_memory_witness

    # 108.00 GiB / 0.90 = 120 GB per replica — agrees with the label.
    _ch_oom_row(results_db, label="3x120GB", ceiling_gib="108.00", qid="tpcds_ok")
    # 28.80 GiB / 0.90 = 32 GB per replica — contradicts a 120 GB label.
    _ch_oom_row(results_db, label="3x120GB", ceiling_gib="28.80", qid="tpcds_bad")

    flagged = mislabelled_by_memory_witness(results_db)

    assert [f["query_id"] for f in flagged] == ["tpcds_bad"]
    assert flagged[0]["measured_gb_per_replica"] == pytest.approx(32.0)
    assert flagged[0]["labelled_gb"] == pytest.approx(120.0)


def test_quarantine_nulls_the_label_and_preserves_it(results_db):
    from benchmark.analysis.recost import (
        mislabelled_by_memory_witness,
        quarantine_mislabelled_rows,
    )

    _ch_oom_row(results_db, label="3x120GB", ceiling_gib="108.00", qid="tpcds_ok")
    _ch_oom_row(results_db, label="3x120GB", ceiling_gib="28.80", qid="tpcds_bad")

    summary = quarantine_mislabelled_rows(results_db)

    assert summary["rows"] == 1
    assert not mislabelled_by_memory_witness(results_db), "audit must be clean after"
    with results_db.connect() as con:
        bad_label, bad_meta = con.execute(
            "SELECT compute_label, metadata FROM sql_benchmark_results WHERE query_id = 'tpcds_bad'"
        ).fetchone()
        ok_label = con.execute(
            "SELECT compute_label FROM sql_benchmark_results WHERE query_id = 'tpcds_ok'"
        ).fetchone()[0]

    assert bad_label is None, "contradicted label must not survive"
    assert '"compute_label_quarantined":"3x120GB"' in bad_meta.replace(" ", "")
    # The quarantine marker must stay distinct from the backfill marker: "captured
    # and wrong" is not the same state as "never captured".
    assert "compute_label_was_null" not in bad_meta
    assert ok_label == "3x120GB", "agreeing rows must be untouched"


# ── fabric capacity-window pricing ──────────────────────────────────────────
#
# Pricing each Fabric statement at its own elapsed time x its SKU's full rate bills
# a shared capacity once per statement in flight: one concurrency round costs $483
# that way and $35.71 priced by window.

T0 = "2026-09-26 12:00:00"
PROV_1 = "00000000-0000-0000-0000-000000000001"
PROV_2 = "00000000-0000-0000-0000-000000000002"


def _fabric_row(
    db,
    *,
    qid,
    label="F16",
    conf="proportional",
    server_ms=None,
    wall_ms=1000.0,
    cost=0.0004,
    err=None,
    run_type="warm",
    level=None,
    provenance="00000000-0000-0000-0000-00000000000a",
    cpu_ms=None,
    end_s=0.0,
):
    """One Fabric row ending ``end_s`` seconds after :data:`T0`."""
    db.record_sql_result(
        platform="fabric",
        query_id=qid,
        block="tpcds",
        scale_factor=100,
        run_type=run_type,
        run_number=1,
        wall_clock_ms=wall_ms,
        server_time_ms=server_ms,
        row_count=1,
        cost_usd=cost,
        cost_confidence=conf,
        error_message=err,
        metadata={} if cpu_ms is None else {"cpu_ms": cpu_ms},
        concurrency=level,
        provenance_id=provenance,
        compute_label=label,
    )
    with db.connect() as con:
        con.execute(
            "UPDATE sql_benchmark_results SET created_at = ?::TIMESTAMP + to_microseconds(?)"
            " WHERE query_id = ?",
            [T0, int(end_s * 1e6), qid],
        )


def _fabric_rows(db) -> dict[str, tuple]:
    with db.connect() as con:
        rows = con.execute(
            "SELECT query_id, cost_usd, cost_confidence,"
            " json_extract_string(metadata, '$.cost_basis') FROM sql_benchmark_results"
        ).fetchall()
    return {qid: (cost, conf, basis) for qid, cost, conf, basis in rows}


def test_fabric_warm_statement_pays_its_own_sku(results_db):
    """A warm statement has the capacity to itself: one second on F2 and one on F128
    differ 64x, and an estimated row prices its wall clock at its label's rate."""
    from benchmark.analysis.recost import recost_fabric_windows

    _fabric_row(results_db, qid="q_f2", label="F2", server_ms=1000.0)
    _fabric_row(results_db, qid="q_f128", label="F128", server_ms=1000.0)
    _fabric_row(results_db, qid="q_est", label="F32", conf="estimated", wall_ms=2000.0)

    recost_fabric_windows(results_db)

    rows = _fabric_rows(results_db)
    assert rows["q_f2"][0] == pytest.approx(0.36 / 3600.0, rel=1e-6)
    assert rows["q_f128"][0] == pytest.approx(23.04 / 3600.0, rel=1e-6)
    assert rows["q_est"][0] == pytest.approx(2.0 * 5.76 / 3600.0, rel=1e-6)
    assert rows["q_est"][1] == "estimated", "no engine time: the tier stays"
    assert rows["q_f2"][2] == "query"


def test_fabric_warm_statement_bursting_past_its_sku_pays_the_cpu(results_db):
    """One second on F2 holds 2 CU-s; a statement that used 10 CPU-s burst past it."""
    from benchmark.analysis.recost import recost_fabric_windows

    _fabric_row(results_db, qid="q", label="F2", server_ms=1000.0, cpu_ms=10_000.0)
    recost_fabric_windows(results_db)
    assert _fabric_rows(results_db)["q"][0] == pytest.approx(10 * 0.18 / 3600.0, rel=1e-6)


def test_fabric_burst_bills_the_capacity_once_split_by_cpu(results_db):
    """Three statements share one F16 for ten seconds (0-4, 2-8, 6-10 s) and use 6
    CPU-s, well inside its 160 CU-s: the window costs ten seconds of F16, split 1:2:3.
    Pricing each at its own elapsed time would have charged 14 seconds."""
    from benchmark.analysis.recost import recost_fabric_windows

    common = {"run_type": "concurrency", "level": 3, "server_ms": 4000.0}
    _fabric_row(results_db, qid="a", wall_ms=4000.0, end_s=4.0, cpu_ms=1000.0, **common)
    _fabric_row(results_db, qid="b", wall_ms=6000.0, end_s=8.0, cpu_ms=2000.0, **common)
    _fabric_row(results_db, qid="c", wall_ms=4000.0, end_s=10.0, cpu_ms=3000.0, **common)

    summary = recost_fabric_windows(results_db)

    window_usd = 10.0 * 2.88 / 3600.0
    rows = _fabric_rows(results_db)
    assert rows["a"][0] == pytest.approx(window_usd / 6, rel=1e-6)
    assert rows["b"][0] == pytest.approx(window_usd * 2 / 6, rel=1e-6)
    assert rows["c"][0] == pytest.approx(window_usd * 3 / 6, rel=1e-6)
    assert {r[2] for r in rows.values()} == {"window_cpu_share"}
    assert {r[1] for r in rows.values()} == {"proportional"}
    assert summary["total_after_usd"] == pytest.approx(window_usd, abs=1e-4)
    assert summary["cells"][0]["windows"] == 1


def test_fabric_burst_past_the_sku_pays_the_cpu_and_failures_count(results_db):
    """F2 for ten seconds holds 20 CU-s; the burst used 100 CPU-s, 40 of them in a
    statement that failed. The window pays for 100 CU-s, all of it on the successes."""
    from benchmark.analysis.recost import recost_fabric_windows

    common = {"label": "F2", "run_type": "concurrency", "level": 3}
    _fabric_row(results_db, qid="ok1", wall_ms=10_000.0, end_s=10.0, cpu_ms=20_000.0, **common)
    _fabric_row(results_db, qid="ok2", wall_ms=5_000.0, end_s=6.0, cpu_ms=40_000.0, **common)
    _fabric_row(
        results_db, qid="bad", wall_ms=5_000.0, end_s=9.0, cpu_ms=40_000.0, err="timeout", **common
    )

    recost_fabric_windows(results_db)

    window_usd = 100 * 0.18 / 3600.0
    rows = _fabric_rows(results_db)
    assert rows["bad"][0] == 0.0
    assert rows["ok1"][0] == pytest.approx(window_usd / 3, rel=1e-6)
    assert rows["ok2"][0] == pytest.approx(window_usd * 2 / 3, rel=1e-6)


def test_fabric_burst_without_cpu_splits_sku_time_by_elapsed(results_db):
    """History aged out: one statement lacks CPU time, so the window is SKU
    time only, split by engine time where recorded and wall clock where not. The
    window runs while either statement holds the capacity: a's 3 s of engine time
    (1-4 s) covers b's 1-2 s, so it is 3 s, not the 4 s of a's wall clock."""
    from benchmark.analysis.recost import recost_fabric_windows

    common = {"run_type": "concurrency", "level": 2}
    _fabric_row(results_db, qid="a", server_ms=3000.0, wall_ms=4000.0, end_s=4.0, **common)
    _fabric_row(
        results_db, qid="b", conf="estimated", wall_ms=1000.0, end_s=2.0, cpu_ms=5.0, **common
    )

    recost_fabric_windows(results_db)

    window_usd = 3.0 * 2.88 / 3600.0
    rows = _fabric_rows(results_db)
    assert rows["a"][0] == pytest.approx(window_usd * 3 / 4, rel=1e-6)
    assert rows["b"][0] == pytest.approx(window_usd / 4, rel=1e-6)
    assert rows["a"][1:] == ("proportional", "window_time_share")
    assert rows["b"][1] == "estimated"


def test_fabric_windows_are_per_invocation_and_level(results_db):
    """Two invocations, or two levels of one, are separate windows: the idle gap
    between them is not charged to either."""
    from benchmark.analysis.recost import recost_fabric_windows

    common = {"run_type": "concurrency", "wall_ms": 1000.0, "cpu_ms": 100.0}
    _fabric_row(results_db, qid="p1", provenance=PROV_1, level=5, end_s=1.0, **common)
    _fabric_row(results_db, qid="p2", provenance=PROV_2, level=5, end_s=100.0, **common)
    _fabric_row(results_db, qid="l10", provenance=PROV_1, level=10, end_s=50.0, **common)

    summary = recost_fabric_windows(results_db)

    rows = _fabric_rows(results_db)
    for qid in ("p1", "p2", "l10"):
        assert rows[qid][0] == pytest.approx(2.88 / 3600.0, rel=1e-6)
    assert summary["cells"][0]["windows"] == 3


def test_fabric_idle_gap_splits_a_pass_into_windows(results_db):
    """A level-1 pass bills 1+1+1 seconds of F16: neither the half-second client
    turnaround after the first statement nor the ten minutes of reconnects before
    the third, since no statement was running in either."""
    from benchmark.analysis.recost import recost_fabric_windows

    common = {"run_type": "concurrency", "level": 1, "wall_ms": 1000.0, "cpu_ms": 10.0}
    _fabric_row(results_db, qid="a", end_s=1.0, **common)
    _fabric_row(results_db, qid="b", end_s=2.5, **common)  # 0.5 s turnaround
    _fabric_row(results_db, qid="c", end_s=603.0, **common)

    summary = recost_fabric_windows(results_db)

    rows = _fabric_rows(results_db)
    assert rows["a"][0] + rows["b"][0] == pytest.approx(2 * 2.88 / 3600.0, rel=1e-6)
    assert rows["c"][0] == pytest.approx(2.88 / 3600.0, rel=1e-6)
    assert summary["cells"][0]["windows"] == 3


def test_fabric_window_recost_is_idempotent(results_db):
    from benchmark.analysis.recost import recost_fabric_windows

    common = {"run_type": "sustained", "level": 2, "wall_ms": 3000.0}
    _fabric_row(results_db, qid="a", end_s=3.0, cpu_ms=900.0, **common)
    _fabric_row(results_db, qid="b", end_s=5.0, cpu_ms=300.0, **common)

    first = recost_fabric_windows(results_db)
    rows = _fabric_rows(results_db)
    second = recost_fabric_windows(results_db)
    assert _fabric_rows(results_db) == rows
    assert second["total_after_usd"] == first["total_after_usd"] == second["total_before_usd"]


def test_fabric_window_dry_run_writes_nothing(results_db):
    from benchmark.analysis.recost import recost_fabric_windows

    _fabric_row(results_db, qid="a", server_ms=1000.0, cost=9.99)
    summary = recost_fabric_windows(results_db, dry_run=True)
    assert summary["total_after_usd"] == pytest.approx(2.88 / 3600.0, abs=1e-4)
    assert _fabric_rows(results_db)["a"][0] == 9.99


def test_fabric_recost_voids_unpriceable_unlabelled_cost(results_db):
    """A cost with no compute size behind it is a guess wearing a confidence tier;
    void it (preserved in metadata) rather than repricing it at a guessed rate."""
    from benchmark.analysis.recost import recost_fabric_windows

    _fabric_row(results_db, qid="q_nolabel", label=None, conf="proportional", server_ms=1000.0)

    recost_fabric_windows(results_db)

    with results_db.connect() as con:
        cost, conf, meta = con.execute(
            "SELECT cost_usd, cost_confidence, metadata FROM sql_benchmark_results "
            "WHERE query_id = 'q_nolabel'"
        ).fetchone()
    assert cost is None and conf is None
    assert "cost_usd_voided" in (meta or "")


# ── rate-modelled engines under shared load ─────────────────────────────────
#
# Pricing ClickHouse and MotherDuck rows at wall clock x the size's rate charges the
# whole service to every query in flight: against list rate x each window's span,
# that overstates concurrency cost 5.4x on ClickHouse and 23x on MotherDuck.


def _shared_row(
    db, *, platform, qid, label, wall_ms, end_s, level=3, err=None, cost=1.0, conf="estimated"
):
    """A concurrency row ending ``end_s`` seconds after :data:`T0`."""
    db.record_sql_result(
        platform=platform,
        query_id=qid,
        block="tpcds",
        scale_factor=100,
        run_type="concurrency",
        run_number=1,
        wall_clock_ms=wall_ms,
        server_time_ms=None,
        row_count=1,
        cost_usd=cost,
        cost_confidence=conf,
        error_message=err,
        metadata={},
        concurrency=level,
        provenance_id=PROV_1,
        compute_label=label,
    )
    with db.connect() as con:
        con.execute(
            "UPDATE sql_benchmark_results SET created_at = ?::TIMESTAMP + to_microseconds(?)"
            " WHERE query_id = ?",
            [T0, int(end_s * 1e6), qid],
        )


def _costs(db) -> dict[str, float]:
    with db.connect() as con:
        return dict(con.execute("SELECT query_id, cost_usd FROM sql_benchmark_results").fetchall())


def test_clickhouse_burst_costs_the_service_once(results_db):
    """Three queries share a 3x8GB service for ten seconds (0-4, 2-8, 6-10 s): the
    burst costs ten seconds of the service, split 4:6:4, not 14 seconds of it; the
    failed fourth query costs nothing."""
    from benchmark.analysis.recost import recost_capacity_windows
    from benchmark.report.compute_rates import effective_usd_per_hour

    ch = {"platform": "clickhouse", "label": "3x8GB"}
    _shared_row(results_db, qid="a", wall_ms=4000.0, end_s=4.0, **ch)
    _shared_row(results_db, qid="b", wall_ms=6000.0, end_s=8.0, **ch)
    _shared_row(results_db, qid="c", wall_ms=4000.0, end_s=10.0, **ch)
    _shared_row(results_db, qid="bad", wall_ms=2000.0, end_s=9.0, err="OOM", **ch)

    summary = recost_capacity_windows(results_db, RATE_CONFIG)

    window_usd = 10.0 * effective_usd_per_hour("clickhouse", "3x8GB") / 3600.0
    costs = _costs(results_db)
    assert costs["a"] == pytest.approx(window_usd * 4 / 14, rel=1e-6)
    assert costs["b"] == pytest.approx(window_usd * 6 / 14, rel=1e-6)
    assert costs["c"] == pytest.approx(window_usd * 4 / 14, rel=1e-6)
    assert costs["bad"] == 0.0
    assert summary["cells"][0]["windows"] == 1


def test_motherduck_pulse_keeps_its_per_query_price(results_db):
    """Pulse bills compute units per query, so only per-second Ducklings are
    window-priced."""
    from benchmark.analysis.recost import recost_capacity_windows

    for tier in ("Pulse", "Giga"):
        _shared_row(
            results_db,
            platform="motherduck",
            qid=f"{tier}-a",
            label=tier,
            wall_ms=4000.0,
            end_s=4.0,
        )
        _shared_row(
            results_db,
            platform="motherduck",
            qid=f"{tier}-b",
            label=tier,
            wall_ms=4000.0,
            end_s=4.0,
        )

    recost_capacity_windows(results_db, RATE_CONFIG)

    costs = _costs(results_db)
    assert costs["Pulse-a"] == costs["Pulse-b"] == 1.0
    # Giga $24.00/hr: two queries sharing four seconds cost four seconds between them.
    assert costs["Giga-a"] + costs["Giga-b"] == pytest.approx(4.0 * 24.0 / 3600.0, rel=1e-6)


def test_rate_repair_leaves_shared_load_to_windows(results_db):
    """The per-query repair must not re-price a burst at wall clock x rate each."""
    from benchmark.analysis.recost import recost_rate_priced_rows

    ch = {"platform": "clickhouse", "label": "3x8GB"}
    _shared_row(results_db, qid="a", wall_ms=10_000.0, end_s=10.0, **ch)
    _shared_row(results_db, qid="b", wall_ms=10_000.0, end_s=10.0, **ch)

    recost_rate_priced_rows(results_db, RATE_CONFIG, platforms=("clickhouse",))

    from benchmark.report.compute_rates import effective_usd_per_hour

    costs = _costs(results_db)
    assert costs["a"] + costs["b"] == pytest.approx(
        10.0 * effective_usd_per_hour("clickhouse", "3x8GB") / 3600.0, rel=1e-6
    )


# ── Databricks: re-split each billed hour by window time ────────────────────


def _dbx_row(
    db, *, qid, level, wall_ms, end_s, billed, err=None, run_type="concurrency", label="X-Small"
):
    db.record_sql_result(
        platform="databricks",
        query_id=qid,
        block="tpcds",
        scale_factor=1,
        run_type=run_type,
        run_number=1,
        wall_clock_ms=wall_ms,
        server_time_ms=wall_ms,
        row_count=1,
        cost_usd=billed,
        cost_confidence="proportional",
        error_message=err,
        metadata={},
        concurrency=level,
        provenance_id=PROV_1,
        compute_label=label,
    )
    with db.connect() as con:
        con.execute(
            "UPDATE sql_benchmark_results SET created_at = ?::TIMESTAMP + to_microseconds(?)"
            " WHERE query_id = ?",
            [T0, int(end_s * 1e6), qid],
        )


def test_databricks_hour_is_split_by_window_time_then_execution(results_db):
    """One billed hour ($0.05 across our rows, under X-Small's list rate x the time
    queries ran): a one-session pass ran 60 s and got $0.005 of it; a ten-session
    burst ran 10 s but held 100 s of execution and got $0.045. By window time the
    pass ran 6/7 of the hour's measured time."""
    from benchmark.analysis.recost import resplit_databricks_hours

    _dbx_row(results_db, qid="solo", level=1, wall_ms=60_000.0, end_s=60.0, billed=0.005)
    for i in range(10):
        _dbx_row(results_db, qid=f"burst{i}", level=10, wall_ms=10_000.0, end_s=80.0, billed=0.0045)

    summary = resplit_databricks_hours(results_db)

    costs = _costs(results_db)
    assert costs["solo"] == pytest.approx(0.05 * 60 / 70, rel=1e-6)
    assert costs["burst0"] == pytest.approx(0.05 * 10 / 70 / 10, rel=1e-6)
    assert sum(costs.values()) == pytest.approx(0.05, rel=1e-9), "the hour's bill is kept"
    assert summary["total_after_usd"] == pytest.approx(summary["total_before_usd"], abs=1e-4)
    with results_db.connect() as con:
        billed = con.execute(
            "SELECT json_extract_string(metadata, '$.billed_share_usd')::DOUBLE"
            " FROM sql_benchmark_results WHERE query_id = 'solo'"
        ).fetchone()[0]
    assert billed == pytest.approx(0.005), "the billed share is kept for a re-run"

    resplit_databricks_hours(results_db)
    assert _costs(results_db) == pytest.approx(costs), "idempotent"


def test_databricks_sizes_sharing_an_hour_split_by_list_rate_times_time(results_db):
    """A resized warehouse ran a 2X-Small pass for 40 min, then a Small pass for 20 min,
    in one billed hour ($3.00 across our rows). Split by execution time alone, the
    bigger size comes out cheaper per second. By list rate x time the hour goes
    2.80 x 40 : 8.40 x 20 = 112 : 168."""
    from benchmark.analysis.recost import resplit_databricks_hours

    _dbx_row(
        results_db,
        qid="xxs",
        level=None,
        wall_ms=2_400_000.0,
        end_s=2400.0,
        billed=2.00,
        run_type="warm",
        label="2X-Small",
    )
    _dbx_row(
        results_db,
        qid="s",
        level=None,
        wall_ms=1_200_000.0,
        end_s=3600.0 - 1.0,
        billed=1.00,
        run_type="warm",
        label="Small",
    )

    resplit_databricks_hours(results_db)

    costs = _costs(results_db)
    assert costs["xxs"] == pytest.approx(3.00 * 112 / 280, rel=1e-6)
    assert costs["s"] == pytest.approx(3.00 * 168 / 280, rel=1e-6)
    assert costs["s"] / 20 == pytest.approx(3 * costs["xxs"] / 40, rel=1e-6), (
        "3x the rate per minute"
    )


def test_databricks_hours_stay_apart_and_failures_pass_their_share_on(results_db):
    from benchmark.analysis.recost import resplit_databricks_hours

    _dbx_row(results_db, qid="ok", level=5, wall_ms=5_000.0, end_s=5.0, billed=0.003)
    _dbx_row(results_db, qid="bad", level=5, wall_ms=5_000.0, end_s=5.0, billed=0.002, err="x")
    _dbx_row(results_db, qid="next_hour", level=5, wall_ms=5_000.0, end_s=3605.0, billed=0.004)

    resplit_databricks_hours(results_db)

    costs = _costs(results_db)
    assert costs["ok"] == pytest.approx(0.005) and costs["bad"] == 0.0
    assert costs["next_hour"] == pytest.approx(0.004)


def test_databricks_idle_in_a_billed_hour_is_not_query_cost(results_db):
    """A 10 s statement alone in an hour that billed $1.00 (the warehouse idled
    before auto-stop) costs X-Small's list rate x its 10 s of engine time."""
    from benchmark.analysis.recost import resplit_databricks_hours
    from benchmark.report.compute_rates import effective_usd_per_hour

    _dbx_row(
        results_db, qid="q", level=None, run_type="warm", wall_ms=10_000.0, end_s=10.0, billed=1.00
    )

    resplit_databricks_hours(results_db)

    rate = effective_usd_per_hour("databricks", "X-Small")
    assert _costs(results_db)["q"] == pytest.approx(rate * 10 / 3600, rel=1e-6)


def test_clickhouse_is_priced_from_its_label_not_the_captured_size_map(results_db) -> None:
    """A ClickHouse row's cost follows its ``<replicas>x<memory>GB`` label.

    A captured size map can be wrong (vCPU counts swapped between sizes, or 1x120GB
    mapped to 2 vCPUs: $0.30/h against $4.48 list). The repair prices from the
    label, so a wrong size map cannot mis-price a row.
    """
    from benchmark.analysis.recost import recost_rate_priced_rows
    from benchmark.report.compute_rates import effective_usd_per_hour

    results_db.record_sql_result(
        platform="clickhouse",
        query_id="tpcds_q01",
        block="tpcds",
        scale_factor=100,
        run_type="warm",
        run_number=1,
        wall_clock_ms=3_600_000.0,
        server_time_ms=3_500_000.0,
        row_count=1,
        cost_usd=0.2985,
        cost_confidence="estimated",
        error_message=None,
        compute_label="1x120GB",
    )
    wrong_map = {"platforms": {"clickhouse": {"sizes": {"1x120GB": {"vcpus": 2, "replicas": 1}}}}}
    recost_rate_priced_rows(results_db, wrong_map, platforms=("clickhouse",))
    with results_db.connect() as con:
        cost = con.execute("SELECT cost_usd FROM sql_benchmark_results").fetchone()[0]
    # Priced on its engine time (3,500 s), not the client's wall clock (3,600 s).
    assert cost == pytest.approx(effective_usd_per_hour("clickhouse", "1x120GB") * 3500 / 3600)


# --- Capacity-time passes: Redshift idle cap, BigQuery baseline held, Snowflake re-spread ---


def _cap_row(
    db,
    *,
    platform,
    qid,
    label,
    wall_ms,
    end_s,
    cost,
    conf,
    metadata=None,
    err=None,
    run_type="warm",
    level=None,
    server_ms=None,
    queue_ms=None,
    provenance=PROV_1,
):
    """One row ending ``end_s`` seconds after :data:`T0`."""
    db.record_sql_result(
        queue_ms=queue_ms,
        platform=platform,
        query_id=qid,
        block="tpcds",
        scale_factor=1,
        run_type=run_type,
        run_number=1,
        wall_clock_ms=wall_ms,
        server_time_ms=server_ms,
        row_count=1,
        cost_usd=cost,
        cost_confidence=conf,
        error_message=err,
        metadata=metadata or {},
        concurrency=level,
        provenance_id=provenance,
        compute_label=label,
    )
    with db.connect() as con:
        con.execute(
            "UPDATE sql_benchmark_results SET created_at = ?::TIMESTAMP + to_microseconds(?)"
            " WHERE query_id = ?",
            [T0, int(end_s * 1e6), qid],
        )


def _meta(db, qid, key):
    with db.connect() as con:
        return con.execute(
            "SELECT cost_confidence, json_extract_string(metadata, '$.' || ?)"
            " FROM sql_benchmark_results WHERE query_id = ?",
            [key, qid],
        ).fetchone()


def test_redshift_warm_statement_is_capped_at_rpu_rate_times_its_time(results_db):
    """8 RPU lists $3.00/h. A 1 s statement billed $0.10 (idle harness time spread
    onto it) caps at $3.00/3600; one billed under its cap keeps its billed share."""
    from benchmark.analysis.recost import cap_redshift_idle

    _cap_row(
        results_db,
        platform="redshift",
        qid="idle",
        label="8 RPU",
        wall_ms=1000.0,
        end_s=1.0,
        cost=0.10,
        conf="proportional",
        metadata={"billed_share_usd": 0.10},
    )
    _cap_row(
        results_db,
        platform="redshift",
        qid="busy",
        label="8 RPU",
        wall_ms=3600.0,
        end_s=10.0,
        cost=0.001,
        conf="proportional",
    )
    _cap_row(
        results_db,
        platform="redshift",
        qid="est",
        label="8 RPU",
        wall_ms=1000.0,
        end_s=20.0,
        cost=None,
        conf="estimated",
    )

    cap_redshift_idle(results_db)

    costs = _costs(results_db)
    assert costs["idle"] == pytest.approx(3.0 / 3600, rel=1e-9)
    assert costs["busy"] == pytest.approx(0.001, rel=1e-9), "under the cap: billed share kept"
    assert costs["est"] is None, "rows reconcile has not priced are left alone"
    assert _meta(results_db, "idle", "billed_share_usd") == ("proportional", "0.1")
    assert _meta(results_db, "busy", "cost_basis") == ("proportional", "capped_capacity_time")


def test_redshift_burst_caps_the_window_and_splits_it_by_execution(results_db):
    """Three sessions hold 8 RPU over 1-4, 3-8 and 6-10 s of engine time (the client
    saw each for 4-6 s) and were billed $0.05: the window caps at the 9 s a query was
    running x $3.00/h, split 3:5:4 by execution time; a failure costs 0."""
    from benchmark.analysis.recost import cap_redshift_idle

    common = {
        "platform": "redshift",
        "label": "8 RPU",
        "conf": "proportional",
        "run_type": "concurrency",
        "level": 5,
    }
    _cap_row(results_db, qid="a", wall_ms=4000.0, server_ms=3000.0, end_s=4.0, cost=0.02, **common)
    _cap_row(results_db, qid="b", wall_ms=6000.0, server_ms=5000.0, end_s=8.0, cost=0.02, **common)
    _cap_row(results_db, qid="c", wall_ms=5000.0, server_ms=4000.0, end_s=10.0, cost=0.01, **common)
    _cap_row(results_db, qid="x", wall_ms=1000.0, end_s=9.0, cost=0.00, err="boom", **common)

    summary = cap_redshift_idle(results_db)

    window = 9 * 3.0 / 3600
    costs = _costs(results_db)
    assert costs["a"] == pytest.approx(window * 3 / 12, rel=1e-9)
    assert costs["b"] == pytest.approx(window * 5 / 12, rel=1e-9)
    assert costs["x"] == 0.0
    assert summary["total_after_usd"] == pytest.approx(window, abs=1e-4)


def test_redshift_turnaround_between_queries_is_not_held(results_db):
    """A one-session pass: two 1 s queries with half a second of client turnaround
    between them hold the workgroup for 2 s, not 2.5 s."""
    from benchmark.analysis.recost import cap_redshift_idle

    common = {
        "platform": "redshift",
        "label": "8 RPU",
        "conf": "proportional",
        "run_type": "concurrency",
        "level": 1,
        "wall_ms": 1000.0,
        "server_ms": 1000.0,
        "cost": 0.01,
    }
    _cap_row(results_db, qid="a", end_s=1.0, **common)
    _cap_row(results_db, qid="b", end_s=2.5, **common)

    cap_redshift_idle(results_db)

    assert sum(_costs(results_db).values()) == pytest.approx(2 * 3.0 / 3600, rel=1e-9)


def test_queue_inside_the_engine_is_held_but_not_split_on(results_db):
    """Redshift reports execution apart from queue. Two sessions: a runs 0-4 s; b
    waits 4 s in the queue behind it, then runs 4-6 s. The workgroup is held for the
    6 s b's total elapsed spans (it was billed while b waited), and the window splits
    4:2 by execution."""
    from benchmark.analysis.recost import cap_redshift_idle

    common = {
        "platform": "redshift",
        "label": "8 RPU",
        "conf": "proportional",
        "run_type": "concurrency",
        "level": 2,
        "cost": 0.05,
    }
    _cap_row(results_db, qid="a", wall_ms=4000.0, server_ms=4000.0, end_s=4.0, **common)
    _cap_row(
        results_db, qid="b", wall_ms=6000.0, server_ms=2000.0, queue_ms=4000.0, end_s=6.0, **common
    )

    cap_redshift_idle(results_db)

    window = 6 * 3.0 / 3600
    costs = _costs(results_db)
    assert costs["a"] == pytest.approx(window * 4 / 6, rel=1e-9)
    assert costs["b"] == pytest.approx(window * 2 / 6, rel=1e-9)


def test_redshift_voided_rows_are_priced_on_rpu_time_and_marked_estimated(results_db):
    """Rows whose estimates were voided (no billing left) cost RPU rate x their
    engine time; in a burst they share the window's cap with billed rows. Their
    stand-in is never recorded as a billed share, and a second pass changes nothing."""
    from benchmark.analysis.recost import cap_redshift_idle

    voided = {"voided_cost_usd": 0.40}
    common = {"platform": "redshift", "label": "8 RPU"}
    _cap_row(
        results_db,
        qid="solo",
        wall_ms=2000.0,
        server_ms=1000.0,
        end_s=2.0,
        cost=None,
        conf=None,
        metadata=voided,
        **common,
    )
    burst = {"run_type": "concurrency", "level": 2, **common}
    _cap_row(
        results_db,
        qid="billed",
        wall_ms=4000.0,
        server_ms=4000.0,
        end_s=14.0,
        cost=0.01,
        conf="proportional",
        **burst,
    )
    _cap_row(
        results_db,
        qid="void",
        wall_ms=4000.0,
        server_ms=4000.0,
        end_s=14.0,
        cost=None,
        conf=None,
        metadata=voided,
        **burst,
    )

    cap_redshift_idle(results_db)
    first = _costs(results_db)
    cap_redshift_idle(results_db)

    assert first["solo"] == pytest.approx(3.0 * 1 / 3600, rel=1e-9)
    window = 4 * 3.0 / 3600  # billed 0.01 + stand-in 0.0033 > 4 s of 8 RPU: capped
    assert first["billed"] + first["void"] == pytest.approx(window, rel=1e-9)
    assert _costs(results_db) == pytest.approx(first), "idempotent"
    assert _meta(results_db, "solo", "billed_share_usd") == ("estimated", None)
    assert _meta(results_db, "void", "voided_cost_usd") == ("estimated", "0.4")
    assert _meta(results_db, "billed", "billed_share_usd") == ("proportional", "0.01")


def test_redshift_cap_is_idempotent(results_db):
    """The second pass starts from the kept billed shares, not the capped cost."""
    from benchmark.analysis.recost import cap_redshift_idle

    _cap_row(
        results_db,
        platform="redshift",
        qid="q",
        label="8 RPU",
        wall_ms=1000.0,
        end_s=1.0,
        cost=0.10,
        conf="proportional",
    )
    cap_redshift_idle(results_db)
    first = _costs(results_db)["q"]
    cap_redshift_idle(results_db)
    assert _costs(results_db)["q"] == pytest.approx(first, rel=1e-12)
    assert _meta(results_db, "q", "billed_share_usd")[1] == "0.1"


def test_bigquery_reservation_prices_the_baseline_for_the_time_held(results_db):
    """100 slots list $6.00/h: a 2 s statement costs 2 s of it whatever it consumed.
    The consumed price stays in metadata; the tier becomes proportional; on-demand
    rows keep their bytes price; a failure costs 0."""
    from benchmark.analysis.recost import recost_bigquery_capacity

    label = "ENTERPRISE / 100 slots"
    _cap_row(
        results_db,
        platform="bigquery",
        qid="res",
        label=label,
        wall_ms=2000.0,
        end_s=2.0,
        cost=0.0001,
        conf="direct",
        metadata={"slot_millis": 36_000},
    )
    _cap_row(
        results_db,
        platform="bigquery",
        qid="fail",
        label=label,
        wall_ms=500.0,
        end_s=5.0,
        cost=0.0,
        conf="direct",
        metadata={"slot_millis": 100},
        err="boom",
    )
    _cap_row(
        results_db,
        platform="bigquery",
        qid="od",
        label="on-demand",
        wall_ms=2000.0,
        end_s=9.0,
        cost=0.0123,
        conf="direct",
    )

    recost_bigquery_capacity(results_db)

    costs = _costs(results_db)
    assert costs["res"] == pytest.approx(6.0 * 2 / 3600, rel=1e-9)
    assert costs["fail"] == 0.0
    assert costs["od"] == pytest.approx(0.0123)
    conf, consumed = _meta(results_db, "res", "slot_ms_cost_usd")
    assert conf == "proportional"
    assert float(consumed) == pytest.approx(36_000 / 3_600_000 * RATE, rel=1e-9)
    assert float(_meta(results_db, "fail", "slot_ms_cost_usd")[1]) == 0.0
    assert _meta(results_db, "od", "cost_basis") == ("direct", None)


def test_bigquery_burst_pays_the_baseline_once(results_db):
    """Ten sessions over the same 10 s on 100 slots cost 10 s of the baseline, not 100 s."""
    from benchmark.analysis.recost import recost_bigquery_capacity

    for i in range(10):
        _cap_row(
            results_db,
            platform="bigquery",
            qid=f"q{i}",
            label="ENTERPRISE / 100 slots",
            wall_ms=10_000.0,
            end_s=10.0,
            cost=0.0,
            conf="direct",
            run_type="concurrency",
            level=10,
            metadata={"slot_millis": 1000},
        )

    summary = recost_bigquery_capacity(results_db)
    again = recost_bigquery_capacity(results_db)

    assert sum(_costs(results_db).values()) == pytest.approx(6.0 * 10 / 3600, rel=1e-9)
    assert again["total_after_usd"] == summary["total_after_usd"], "idempotent"


def test_bigquery_burst_is_split_by_slot_share_else_execution(results_db):
    """Two queries hold 100 slots over 10 s of engine time: the baseline's 10 s goes
    1:3 by the slot time each used. A window where a successful row lacks slot time
    splits by execution time instead (10:3)."""
    from benchmark.analysis.recost import recost_bigquery_capacity

    common = {
        "platform": "bigquery",
        "label": "ENTERPRISE / 100 slots",
        "conf": "direct",
        "cost": 0.0,
        "run_type": "concurrency",
        "wall_ms": 10_000.0,
        "end_s": 10.0,
    }
    _cap_row(
        results_db, qid="s1", level=2, server_ms=10_000.0, metadata={"slot_millis": 1000}, **common
    )
    _cap_row(
        results_db, qid="s3", level=2, server_ms=1000.0, metadata={"slot_millis": 3000}, **common
    )
    _cap_row(
        results_db, qid="e1", level=5, server_ms=10_000.0, metadata={"slot_millis": 5}, **common
    )
    _cap_row(results_db, qid="e3", level=5, server_ms=3000.0, **common)

    recost_bigquery_capacity(results_db)

    window = 6.0 * 10 / 3600
    costs = _costs(results_db)
    assert costs["s1"] == pytest.approx(window / 4, rel=1e-9)
    assert costs["s3"] == pytest.approx(window * 3 / 4, rel=1e-9)
    assert costs["e1"] == pytest.approx(window * 10 / 13, rel=1e-9), "falls back to execution"
    assert costs["e3"] == pytest.approx(window * 3 / 13, rel=1e-9)


#: X-Small Gen2 at Standard: 1 credit/h x 1.35 x $2.00.
SF_XS_USD_PER_H = 1 * 1.35 * 2.00


def test_snowflake_is_priced_on_engine_time_and_keeps_attribution(results_db):
    """Two warm X-Small queries priced by attribution at $3/credit ($0.01, and $0 for
    one that shared its second) become list rate x engine time at Standard; their
    credits stay as a cross-check. A never-reconciled row is priced too."""
    from benchmark.analysis.recost import price_snowflake_on_engine_time

    common = {"platform": "snowflake", "label": "X-Small (Gen2)", "wall_ms": 1200.0}
    _cap_row(
        results_db, qid="paid", server_ms=1000.0, end_s=1.2, cost=0.01, conf="direct", **common
    )
    _cap_row(results_db, qid="free", server_ms=400.0, end_s=2.0, cost=0.0, conf="direct", **common)
    _cap_row(
        results_db, qid="late", server_ms=1100.0, end_s=9.0, cost=0.0, conf="estimated", **common
    )

    price_snowflake_on_engine_time(results_db)

    costs = _costs(results_db)
    assert costs["paid"] == pytest.approx(SF_XS_USD_PER_H * 1.0 / 3600, rel=1e-9)
    assert costs["free"] == pytest.approx(SF_XS_USD_PER_H * 0.4 / 3600, rel=1e-9)
    assert costs["late"] == pytest.approx(SF_XS_USD_PER_H * 1.1 / 3600, rel=1e-9)
    conf, credits = _meta(results_db, "paid", "attributed_credits")
    assert conf == "estimated" and float(credits) == pytest.approx(0.01 / 3.0)
    assert _meta(results_db, "late", "attributed_credits")[1] is None, "reconcile records it"


def test_snowflake_burst_is_priced_as_a_window_and_is_idempotent(results_db):
    """Five sessions on X-Small over 0-10 s of engine time cost 10 s of the warehouse,
    split by engine time, whatever attribution gave them; a second pass changes
    nothing."""
    from benchmark.analysis.recost import price_snowflake_on_engine_time

    common = {
        "platform": "snowflake",
        "label": "X-Small (Gen2)",
        "conf": "direct",
        "run_type": "concurrency",
        "level": 5,
        "cost": 0.004,
    }
    for i, (server, end) in enumerate(((10_000.0, 10.0), (4000.0, 6.0), (6000.0, 9.0))):
        _cap_row(results_db, qid=f"q{i}", wall_ms=server, server_ms=server, end_s=end, **common)

    price_snowflake_on_engine_time(results_db)
    first = _costs(results_db)
    summary = price_snowflake_on_engine_time(results_db)

    assert sum(first.values()) == pytest.approx(SF_XS_USD_PER_H * 10 / 3600, rel=1e-9)
    assert first["q0"] == pytest.approx(SF_XS_USD_PER_H * 10 / 3600 * 10 / 20, rel=1e-9)
    assert _costs(results_db) == pytest.approx(first)
    assert summary["total_after_usd"] == pytest.approx(summary["total_before_usd"], abs=1e-6)


def test_snowflake_dry_run_writes_nothing(results_db):
    from benchmark.analysis.recost import price_snowflake_on_engine_time

    _cap_row(
        results_db,
        platform="snowflake",
        qid="q",
        label="X-Small (Gen2)",
        wall_ms=1000.0,
        server_ms=1000.0,
        end_s=1.0,
        cost=0.01,
        conf="direct",
    )
    summary = price_snowflake_on_engine_time(results_db, dry_run=True)

    assert summary["dry_run"] is True
    assert summary["total_after_usd"] == pytest.approx(SF_XS_USD_PER_H / 3600, abs=1e-4)
    assert _costs(results_db)["q"] == pytest.approx(0.01)
    assert _meta(results_db, "q", "attributed_credits") == ("direct", None)

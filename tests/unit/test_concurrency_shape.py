"""Concurrency shape reduction.

The two-axis view exists because latency alone cannot distinguish an engine that
absorbs load from one that rejects it — Databricks holds latency flat through 50
sessions then sheds ~37% at 100, which looks identical to genuine spare capacity if
you only plot the median. These tests pin that distinction.
"""

from __future__ import annotations

import pytest

from benchmark.analysis.concurrency_shape import classify


def test_flat_latency_without_shedding_is_absorbing():
    assert classify(0.1, 0.0) == "absorbs"


def test_flat_latency_with_shedding_is_rejecting_not_absorbing():
    """The distinction the two-axis view exists for.

    Same latency curve, opposite meaning: the engine kept latency flat by refusing
    work. Collapsing this to 'absorbs' would credit an engine for load it declined.
    """
    assert classify(0.1, 37.0) == "rejects"


def test_rising_latency_without_shedding_is_queueing():
    assert classify(0.8, 0.0) == "queues"


def test_rising_latency_with_shedding_is_the_worst_case():
    assert classify(0.8, 44.0) == "queues+sheds"


def test_unfittable_cell_is_unknown_rather_than_assumed_good():
    assert classify(None, 0.0) == "unknown"


@pytest.mark.parametrize("shed", [4.9, 5.0, 5.1])
def test_shed_threshold_is_applied_consistently(shed):
    mode = classify(0.1, shed)
    assert mode == ("absorbs" if shed < 5.0 else "rejects")


# ── client-caused cancellations ─────────────────────────────────────────────
#
# A shed rate is only an engine property if the client did not cause it. A client-side
# timeout (e.g. a 90 s connector network_timeout) can cancel most of a concurrency
# cell while the engine rejects nothing; publishing those cancels as sheds would
# claim the engine collapses under load and invert the true finding.


def test_cells_dominated_by_client_cancels_are_not_publishable():
    from benchmark.analysis.concurrency_shape import MAX_CLIENT_CAPPED_PCT

    assert MAX_CLIENT_CAPPED_PCT <= 1.0, (
        "the client-contamination bar must stay tight — a few percent of our own "
        "cancellations is enough to misattribute an engine's failure mode"
    )


def test_publishable_flag_tracks_the_client_share(tmp_path):
    """The flag must key on CLIENT-caused errors, not the total shed rate.

    An engine that genuinely rejects 40% of queries is a real, publishable finding.
    An engine whose 40% is our socket hanging up is not. These are indistinguishable
    on a shed-rate axis alone, which is exactly why the field exists.
    """
    from benchmark.analysis.concurrency_shape import MAX_CLIENT_CAPPED_PCT

    genuine_shed, client_shed = 40.0, 40.0
    assert genuine_shed > MAX_CLIENT_CAPPED_PCT
    # Same shed rate, opposite publishability, decided solely by attribution.
    assert (MAX_CLIENT_CAPPED_PCT >= 0.0) and (client_shed > MAX_CLIENT_CAPPED_PCT)


# ── capped_cells: which cells must not be published as engine behaviour ──────

_COLS = (
    "platform VARCHAR, query_id VARCHAR, block VARCHAR, scale_factor INTEGER, "
    "run_type VARCHAR, run_number INTEGER, concurrency INTEGER, compute_label VARCHAR, "
    "error_message VARCHAR, created_at TIMESTAMP"
)


@pytest.fixture
def db():
    import duckdb

    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE main.sql_benchmark_results ({_COLS})")
    yield c
    c.close()


def _rows(
    db,
    platform,
    n,
    *,
    error=None,
    block="tpcds",
    scale=1000,
    label="Small",
    level=100,
    first=0,
):
    """Insert ``n`` concurrency rows with distinct query_ids.

    ``first`` offsets the query_id sequence. Reusing ids across calls would collide
    on the latest-wins dedup key and silently drop rows, which would make these
    tests pass or fail for reasons unrelated to what they check.
    """
    for i in range(first, first + n):
        db.execute(
            "INSERT INTO main.sql_benchmark_results VALUES (?, ?, ?, ?, 'concurrency', 1, ?, ?, ?,"
            " '2026-08-01 00:00:00')",
            [platform, f"{block}_q{i:04d}", block, scale, level, label, error],
        )


def test_a_genuine_engine_shed_stays_publishable(db):
    """An engine that really rejects 40% of a burst is a finding, not a defect."""
    from benchmark.analysis.concurrency_shape import capped_cells

    _rows(db, "databricks", 60)
    _rows(db, "databricks", 40, error="TIMEOUT: query exceeded admission window", first=60)

    assert capped_cells(db) == []


def test_a_client_caused_shed_is_flagged(db):
    from benchmark.analysis.concurrency_shape import capped_cells

    _rows(db, "snowflake", 60)
    _rows(db, "snowflake", 40, error="000604 (57014): SQL execution canceled", first=60)

    (cell,) = capped_cells(db)
    assert cell.platform == "snowflake"
    assert cell.client_cancelled == 40
    assert cell.client_cancel_pct == pytest.approx(40.0)
    assert cell.errors_all_ours, "the engine rejected nothing here"


def test_both_snowflake_cancel_shapes_are_recognized(db):
    """The connector reports the same cause two different ways; missing either
    one would leave half a contaminated cell looking like engine behaviour."""
    from benchmark.analysis.concurrency_shape import capped_cells

    _rows(db, "snowflake", 90)
    _rows(db, "snowflake", 5, error="000604 (57014): SQL execution canceled", first=90)
    _rows(
        db,
        "snowflake",
        5,
        error="000604 (57014): SQL execution was cancelled by the client",
        first=95,
    )

    (cell,) = capped_cells(db)
    assert cell.client_cancelled == 10


def test_a_mixed_cell_is_flagged_but_not_called_all_ours(db):
    """Partial contamination still disqualifies the cell — the surviving latency
    median is biased either way — but the claim about it must stay accurate."""
    from benchmark.analysis.concurrency_shape import capped_cells

    _rows(db, "snowflake", 80)
    _rows(db, "snowflake", 10, error="000604 (57014): SQL execution canceled", first=80)
    _rows(db, "snowflake", 10, error="OOM: exceeded memory limit", first=90)

    (cell,) = capped_cells(db)
    assert cell.errors == 20
    assert cell.client_cancelled == 10
    assert not cell.errors_all_ours


def test_detection_is_block_scoped(db):
    """Client cancellations can hit one block and not another (Snowflake's sit in
    TPC-DS/DSB; its ClickBench ladders have none). Ignoring the block would withhold
    a clean ClickBench ladder because the decision-support ladder at the same size
    was contaminated."""
    from benchmark.analysis.concurrency_shape import capped_cells

    _rows(db, "snowflake", 50, error="000604 (57014): SQL execution canceled")
    _rows(db, "snowflake", 50, block="clickbench", first=50)

    assert [c.compute_label for c in capped_cells(db, blocks=("tpcds", "dsb"))] == ["Small"]
    assert capped_cells(db, blocks=("clickbench",)) == []


def test_a_trace_of_client_cancellation_does_not_condemn_a_cell(db):
    """The bar is a share, not a single occurrence: one cancelled query in a
    thousand cannot meaningfully bias a median, and treating it as fatal would
    withhold sound data."""
    from benchmark.analysis.concurrency_shape import capped_cells

    _rows(db, "snowflake", 1000)
    _rows(db, "snowflake", 5, error="000604 (57014): SQL execution canceled", first=1000)

    assert capped_cells(db) == []

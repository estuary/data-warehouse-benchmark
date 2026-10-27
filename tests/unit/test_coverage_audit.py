"""Run-plan completeness audit.

Coverage gaps (a missing sweep ladder, censored ceilings, unreconciled cells) sit in
the data unnoticed unless something asks for them. The audit asks; these tests pin
the asking.

The audit deliberately does NOT distinguish deliberate from overlooked: the remedy for
a deliberate omission is to record it in `scale_skips`, which both removes it here and
surfaces it in the report's "deliberately not measured" table. So a recorded skip must
suppress the gap, and an unrecorded one must not.
"""

from __future__ import annotations

import pytest

from benchmark.analysis.coverage import KINDS, STANDARD_LEVELS, _designated_label, audit


def _warm(db, platform, scale, label, qid="tpcds_q01"):
    db.record_sql_result(
        platform=platform,
        query_id=qid,
        block="tpcds",
        scale_factor=scale,
        run_type="warm",
        run_number=1,
        wall_clock_ms=10.0,
        server_time_ms=5.0,
        row_count=1,
        cost_usd=0.1,
        cost_confidence="direct",
        error_message=None,
        bytes_scanned=1,
        compute_label=label,
    )


def test_a_designated_cell_with_no_data_is_reported(results_db):
    """The strongest signal the audit has: the plan asserts a size for a scale and
    the database denies it."""
    with results_db.connect() as con:
        gaps = audit(con)

    missing = [g for g in gaps if g.kind == "designated-missing"]
    assert missing, "an empty DB should report every designated cell as missing"
    assert all(g.fill_command.startswith("uv run benchmark sql") for g in missing)


def test_a_recorded_skip_suppresses_its_gaps(results_db):
    """A deliberate omission recorded in scale_skips must stop being reported —
    otherwise the audit stays permanently noisy and nobody reads it."""
    with results_db.connect() as con:
        before = len([g for g in audit(con) if g.platform == "fabric"])
    for sf in (1, 10, 100, 1000):
        results_db.record_scale_skip(sf, "fabric", "not benchmarked", "test")
    with results_db.connect() as con:
        after = [g for g in audit(con) if g.platform == "fabric"]

    assert before > 0
    assert after == [], f"recorded skips should suppress gaps, still saw {after}"


def test_the_label_mapper_tolerates_both_ladder_conventions():
    """Sweep ladders are not written in one unit. Most name a run-plan target the
    runner translates; ClickHouse's names the label itself, because its knob is
    replica count while its ladder walks memory. A mismatch must degrade to the raw
    string, never raise — a coverage audit that crashes on a naming difference is
    worse than one that reports a gap that is not one."""
    assert _designated_label("snowflake", 1, "XSMALL") == "X-Small (Gen2)"
    assert _designated_label("redshift", 1, 8) == "8 RPU"
    # ClickHouse's ladder entries are already labels; the mapper wants an int.
    assert _designated_label("clickhouse", 100, "1x8GB") == "1x8GB"


def test_standard_levels_match_what_the_runners_default_to():
    """A level-gap is defined against this tuple, so it has to track what a pass runs."""
    import importlib.util
    import inspect
    import pathlib

    from benchmark.sql_benchmark.concurrency import run_concurrency_pass

    param = inspect.signature(run_concurrency_pass).parameters["concurrency_levels"]
    assert tuple(param.default) == STANDARD_LEVELS

    path = pathlib.Path("scripts", "sweeps", "redshift_sweep.py")
    spec = importlib.util.spec_from_file_location("redshift_sweep", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DEFAULT_LEVELS == STANDARD_LEVELS


@pytest.mark.parametrize("kind", KINDS)
def test_every_gap_kind_carries_a_runnable_fill_command(results_db, kind):
    """A gap a reader cannot act on is a complaint. The command is the deliverable."""
    _warm(results_db, "snowflake", 1, "X-Small (Gen2)")
    with results_db.connect() as con:
        gaps = [g for g in audit(con) if g.kind == kind]
    for g in gaps:
        assert g.fill_command.startswith("uv run benchmark"), g
        assert g.platform in g.fill_command

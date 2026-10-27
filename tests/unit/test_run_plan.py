"""Run-plan model + Snowflake per-scale warehouse resolution.

Covers: loading the repo run_plan.yaml, target() lookups (including missing
cells / platforms), scale validation, and the Snowflake connector deriving its
warehouse name from the run-plan (scale -> size -> BENCHMARK_WH_<SIZE>).
"""

from __future__ import annotations

import textwrap

import pytest

from benchmark.connectors.snowflake import SnowflakeConnector
from benchmark.exceptions import BenchmarkError
from benchmark.run_plan import RunPlan, get_run_plan
from benchmark.scales import VALID_SCALES, validate_scale


def test_repo_run_plan_loads_and_resolves():
    plan = get_run_plan()  # the real run_plan.yaml at repo root
    assert plan.target("snowflake", 1000) == "LARGE"
    assert plan.target("bigquery", 1000) == 500
    assert plan.target("motherduck", 1) == "standard"


def test_missing_cell_and_platform_return_none():
    plan = get_run_plan()
    assert plan.target("nonexistent", 1000) is None


def test_validate_scale():
    for s in VALID_SCALES:
        assert validate_scale(s) == s
    with pytest.raises(ValueError, match="scale must be one of"):
        validate_scale(7)


def test_load_rejects_bad_scale_key(tmp_path):
    bad = tmp_path / "run_plan.yaml"
    bad.write_text("snowflake: { warehouse: { 7: XSMALL } }\n", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="scale must be one of"):
        RunPlan.load(bad)


def test_load_rejects_multi_knob(tmp_path):
    bad = tmp_path / "run_plan.yaml"
    bad.write_text(
        textwrap.dedent("""
            snowflake:
              warehouse: { 1: XSMALL }
              extra: { 1: NOPE }
        """),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError, match="exactly one sizing knob"):
        RunPlan.load(bad)


def test_repo_run_plan_on_demand_policy():
    plan = get_run_plan()  # the real run_plan.yaml
    # allow at low scales, deny at SF1000, no opinion for engines without the policy.
    assert plan.on_demand_allowed("bigquery", 100) is True
    assert plan.on_demand_allowed("bigquery", 1000) is False
    assert plan.on_demand_allowed("snowflake", 1000) is None  # no on_demand policy
    assert plan.on_demand_allowed("nonexistent", 1000) is None
    # The sizing knob is unaffected by the extra policy key.
    assert plan.target("bigquery", 1000) == 500


def test_load_policy_key_alongside_one_knob(tmp_path):
    good = tmp_path / "run_plan.yaml"
    good.write_text(
        textwrap.dedent("""
            bigquery:
              reservation_slots: { 1: 100, 1000: 500 }
              on_demand: { 1: allow, 1000: deny }
        """),
        encoding="utf-8",
    )
    plan = RunPlan.load(good)
    assert plan.target("bigquery", 1000) == 500
    assert plan.on_demand_allowed("bigquery", 1) is True
    assert plan.on_demand_allowed("bigquery", 1000) is False


def test_load_rejects_bad_policy_value(tmp_path):
    bad = tmp_path / "run_plan.yaml"
    bad.write_text(
        textwrap.dedent("""
            bigquery:
              reservation_slots: { 1: 100 }
              on_demand: { 1: maybe }
        """),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError, match="must be one of"):
        RunPlan.load(bad)


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(BenchmarkError, match="run-plan not found"):
        RunPlan.load(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    "scale,expected",
    [
        (1, "BENCHMARK_WH_XSMALL"),
        (1000, "BENCHMARK_WH_LARGE"),
    ],
)
def test_snowflake_warehouse_for_scale(scale, expected):
    assert SnowflakeConnector._warehouse_for_scale(scale) == expected


def test_snowflake_warehouse_for_scale_non_int_returns_none():
    # Non-int (or unmapped) -> None so connect() falls back to the settings default.
    assert SnowflakeConnector._warehouse_for_scale(None) is None
    assert SnowflakeConnector._warehouse_for_scale("1000") is None


# ── sweep ladders ────────────────────────────────────────────────────────────
#
# Sizing comes from the vendor's own published ladder where one exists, and from this
# file's per-scale matrix where no guidance is given. Declaring the ladder makes a
# sweep's intended extent checkable: without it, the swept sizes exist only in
# whatever happened to run, and a deliberately short ladder looks identical to an
# unfinished sweep.


def test_every_engine_declares_a_sweep_ladder():
    from benchmark.run_plan import RunPlan

    plan = RunPlan.load()
    missing = [e for e in plan.engines if not plan.sweep_ladder(e)]

    assert not missing, f"engines with no declared sizing ladder: {missing}"


def test_the_designated_size_is_always_on_the_ladder():
    """A sweep that skips the size the plan designates for a scale would measure a
    curve that never passes through the configuration actually benchmarked."""
    from benchmark.run_plan import RunPlan

    plan = RunPlan.load()
    problems = []
    for engine, ep in plan.engines.items():
        ladder = {str(v).lower() for v in plan.sweep_ladder(engine)}
        if engine == "clickhouse":
            continue  # its knob is replica count; the ladder is expressed in memory
        for scale, target in ep.sizes.items():
            if str(target).lower() not in ladder:
                problems.append(
                    f"{engine} SF{scale} designates {target!r}, not on {sorted(ladder)}"
                )

    assert not problems, problems


def test_ladders_are_ordered_smallest_first():
    """Downstream readers (and the report's axes) assume ascending capacity."""
    from benchmark.run_plan import RunPlan

    plan = RunPlan.load()
    for engine in ("redshift", "bigquery"):
        numeric = [int(v) for v in plan.sweep_ladder(engine)]
        assert numeric == sorted(numeric), f"{engine} ladder is not ascending: {numeric}"


def test_a_non_list_sweep_is_rejected():
    import tempfile
    from pathlib import Path

    from benchmark.exceptions import BenchmarkError
    from benchmark.run_plan import RunPlan

    with tempfile.TemporaryDirectory() as d:
        bad = Path(d) / "run_plan.yaml"
        bad.write_text("snowflake:\n  warehouse: { 1: XSMALL }\n  sweep: XSMALL\n")
        with pytest.raises(BenchmarkError, match="sweep must be a list"):
            RunPlan.load(bad)

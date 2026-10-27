"""E2E orchestration plan: ordering, per-engine fan-out, step flags, filters.

``build_plan`` / ``filter_plan`` are pure, so the production sequence and the
per-engine fan-out are verified here without credentials — the real run just
shells each step to the corresponding ``benchmark`` subcommand.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from benchmark.cost.discovery import DEFAULT_COST_CONFIG_PATH
from benchmark.e2e import build_plan, filter_plan, run_e2e


def _keys(plan):
    return [s.key for s in plan]


def test_plan_is_ordered_generate_through_report():
    keys = _keys(build_plan(["snowflake"], scale=1))
    order = [
        "generate-tpcds",
        "stage",
        "capture",
        "materialize",
        "validate-loads",
        "cost-config",
        "sql",
        "analyze",
        "report",
    ]
    positions = [keys.index(k) for k in order]
    assert positions == sorted(positions)  # each phase precedes the next


def test_estuary_steps_publish_full_spec_unscoped():
    # Captures/materializations are define-once + stateful: e2e publishes the
    # FULL spec (no --scale/--blocks scoping) so it isn't destructively
    # re-published per scale. Data presence is checked in the target instead.
    plan = build_plan(["snowflake"], scale=1)
    capture = next(s for s in plan if s.key == "capture")
    materialize = next(s for s in plan if s.key == "materialize")
    assert capture.argv == ("flow-publish", "source-s3")
    assert materialize.argv == ("flow-publish", "estuary/materializations/snowflake.flow.yaml")
    # No scale/block scoping leaks into the stateful publish steps.
    assert "--scale" not in capture.argv and "--blocks" not in capture.argv
    assert "--scale" not in materialize.argv and "--blocks" not in materialize.argv


def test_per_engine_fanout_with_shared_steps_once():
    plan = build_plan(["snowflake", "bigquery"], scale=1)
    names = [s.name for s in plan]
    assert {"materialize:snowflake", "materialize:bigquery"} <= set(names)
    assert {"sql:snowflake", "sql:bigquery"} <= set(names)
    for shared in ("stage", "capture", "validate-loads", "analyze"):
        assert _keys(plan).count(shared) == 1


def test_validate_loads_passes_all_platforms_scale_and_suites():
    vl = next(
        s for s in build_plan(["snowflake", "bigquery"], scale=10) if s.key == "validate-loads"
    )
    assert "--platforms" in vl.argv and "snowflake,bigquery" in vl.argv
    assert "--scale" in vl.argv and "10" in vl.argv
    # Suites are scoped to the run's blocks so a tpcds,dsb run doesn't fail on
    # the (unloaded) ClickBench suite that --suites otherwise defaults to.
    assert "--suites" in vl.argv and "tpcds,dsb,clickbench" in vl.argv


def test_validate_loads_suites_follow_blocks():
    vl = next(
        s
        for s in build_plan(["snowflake"], scale=1, blocks=("tpcds", "dsb"))
        if s.key == "validate-loads"
    )
    assert "--suites" in vl.argv and "tpcds,dsb" in vl.argv
    assert "clickbench" not in vl.argv


def test_validate_queries_step_scoped_to_local_suites():
    # Present for tpcds/dsb, scoped to just those suites.
    plan = build_plan(["snowflake"], scale=1, blocks=("tpcds", "dsb"))
    vq = next(s for s in plan if s.key == "validate-queries")
    assert vq.argv == ("validate-queries", "--suites", "tpcds,dsb", "--scale", "1")
    # Drops to tpcds-only when that's all that's requested.
    only_tpcds = build_plan(["snowflake"], scale=1, blocks=("tpcds",))
    vq2 = next(s for s in only_tpcds if s.key == "validate-queries")
    assert vq2.argv == ("validate-queries", "--suites", "tpcds", "--scale", "1")


def test_stage_step_scoped_to_blocks():
    # stage-data must follow --blocks so a tpcds,dsb run doesn't upload
    # ClickBench's multi-GiB hits.parquet.
    stage = next(
        s for s in build_plan(["snowflake"], scale=1, blocks=("tpcds", "dsb")) if s.key == "stage"
    )
    assert stage.argv == ("stage-data", "--suites", "tpcds,dsb")


def test_validate_queries_absent_when_no_local_suite():
    # ClickBench ground truth is the separate 100 GB path; no validate-queries.
    plan = build_plan(["snowflake"], scale=1, blocks=("clickbench",))
    assert "validate-queries" not in _keys(plan)


def test_validate_queries_precedes_stage():
    keys = _keys(build_plan(["snowflake"], scale=1))
    assert keys.index("validate-queries") < keys.index("stage")


def test_clickbench_blocks_add_derive_and_ground_truth():
    keys = _keys(build_plan(["snowflake"], scale=1, blocks=("tpcds", "dsb", "clickbench")))
    # download -> derive-typed, ordered, and derive precedes staging (stage
    # uploads the typed hits.parquet the capture binds).
    assert keys.index("download-clickbench") < keys.index("derive-clickbench")
    assert keys.index("derive-clickbench") < keys.index("stage")
    # ClickBench ground truth is hashed for validate-sf1 (the correctness gate);
    # it is built before the SQL benchmark/validation steps run.
    assert "hash-clickbench-gt" in keys
    assert keys.index("hash-clickbench-gt") < keys.index("sql")


def test_no_clickbench_means_no_derive_or_gt():
    keys = _keys(build_plan(["snowflake"], scale=1, blocks=("tpcds", "dsb")))
    assert "download-clickbench" not in keys
    assert "derive-clickbench" not in keys
    assert "hash-clickbench-gt" not in keys


def test_blocks_filter_drops_unused_generation():
    keys = _keys(build_plan(["snowflake"], scale=1, blocks=("tpcds",)))
    assert "generate-tpcds" in keys
    assert "generate-dsb" not in keys
    assert "download-clickbench" not in keys


def test_sql_step_targets_one_platform():
    sql_snow = next(
        s for s in build_plan(["snowflake", "bigquery"], scale=1) if s.name == "sql:snowflake"
    )
    assert sql_snow.argv[0] == "sql"
    assert "--platforms" in sql_snow.argv and "snowflake" in sql_snow.argv


def test_filter_from_and_skip():
    plan = build_plan(["snowflake"], scale=1)
    from_mat = filter_plan(plan, from_key="materialize")
    assert _keys(from_mat)[0] == "materialize"
    assert "generate-tpcds" not in _keys(from_mat)
    assert "report" not in _keys(filter_plan(plan, skip=["report"]))
    with pytest.raises(ValueError, match="unknown --from"):
        filter_plan(plan, from_key="nope")


# ── cost config: `sql` exits 2 without results/cost_config.json ─────────────


def test_cost_config_is_captured_once_after_loads_and_before_sql():
    """capture-cost-config introspects the live engines, so it follows
    validate-loads (every engine has answered by then) and precedes the first sql."""
    plan = build_plan(["snowflake", "bigquery"], scale=1)
    keys = _keys(plan)
    assert keys.count("cost-config") == 1
    assert keys.index("validate-loads") < keys.index("cost-config") < keys.index("sql")
    step = plan[keys.index("cost-config")]
    assert step.argv == ("capture-cost-config", "--platforms", "snowflake,bigquery")
    assert step.scope == "shared"


def test_cost_config_step_is_skipped_when_the_file_exists_unless_recaptured():
    default = next(s for s in build_plan(["snowflake"], scale=1) if s.key == "cost-config")
    assert default.skip_if_exists == DEFAULT_COST_CONFIG_PATH
    forced = next(
        s
        for s in build_plan(["snowflake"], scale=1, recapture_cost_config=True)
        if s.key == "cost-config"
    )
    assert forced.skip_if_exists is None


class _Recorder:
    """subprocess.run double: records each step's argv, every step succeeds."""

    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.argvs.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0)

    def ran(self, subcommand: str) -> bool:
        return any(subcommand in argv for argv in self.argvs)


@pytest.mark.parametrize(
    ("file_exists", "recapture", "expect_run"),
    [(False, False, True), (True, False, False), (True, True, True)],
)
def test_run_e2e_captures_cost_config_only_when_missing_or_asked(
    file_exists: bool,
    recapture: bool,
    expect_run: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.chdir(tmp_path)  # DEFAULT_COST_CONFIG_PATH is relative to the cwd
    if file_exists:
        DEFAULT_COST_CONFIG_PATH.parent.mkdir(parents=True)
        DEFAULT_COST_CONFIG_PATH.write_text("{}", encoding="utf-8")
    recorder = _Recorder()
    monkeypatch.setattr("benchmark.e2e.subprocess.run", recorder)

    results = run_e2e(
        ["snowflake"],
        1,
        from_key="cost-config",
        skip=["analyze", "report"],
        recapture_cost_config=recapture,
        runner=("benchmark",),
    )

    assert dict(results)["capture-cost-config"] == ("ok" if expect_run else "skipped")
    assert recorder.ran("capture-cost-config") is expect_run
    # The sql step still runs: a skipped capture is not a failure.
    assert dict(results)["sql:snowflake"] == "ok"


def test_e2e_cli_passes_recapture_through(monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from benchmark import cli

    seen: dict[str, object] = {}

    def fake_run_e2e(platforms, scale, blocks, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(cli, "_configure", lambda verbose=False: None)
    monkeypatch.setattr("benchmark.e2e.run_e2e", fake_run_e2e)

    assert CliRunner().invoke(cli.app, ["e2e", "--recapture-cost-config"]).exit_code == 0
    assert seen["recapture_cost_config"] is True
    assert CliRunner().invoke(cli.app, ["e2e"]).exit_code == 0
    assert seen["recapture_cost_config"] is False

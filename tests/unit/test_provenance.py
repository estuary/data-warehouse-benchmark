"""Tier-3 run provenance: gather snapshot + record/link round-trip."""

from __future__ import annotations

import json

from benchmark import provenance as prov_mod
from benchmark.provenance import gather_provenance, optimization_provenance


def test_gather_provenance_has_expected_shape():
    prov = gather_provenance(
        command="sql",
        scale_factor=10,
        run_type="warm",
        platforms=["snowflake", "motherduck"],
        blocks=["tpcds", "dsb"],
    )
    # required keys present for record_run_provenance(**prov)
    for key in (
        "command",
        "scale_factor",
        "run_type",
        "platforms",
        "blocks",
        "git_sha",
        "git_dirty",
        "harness_version",
        "optimize_model",
        "query_files_hash",
        "tool_versions",
    ):
        assert key in prov
    assert prov["command"] == "sql"
    assert prov["scale_factor"] == 10
    assert prov["platforms"] == "snowflake,motherduck"
    assert prov["blocks"] == "tpcds,dsb"
    # tool_versions is a dict with at least python
    assert isinstance(prov["tool_versions"], dict)
    assert "python" in prov["tool_versions"]


def test_gather_provenance_empty_platforms_is_none():
    prov = gather_provenance(
        command="sql",
        scale_factor=None,
        run_type=None,
        platforms=[],
        blocks=[],
    )
    assert prov["platforms"] is None
    assert prov["blocks"] is None
    # no queries dir for empty platforms → no hash
    assert prov["query_files_hash"] is None


# ── optimize_model records reality, not configuration ───────────────────────
#
# Echoing `get_settings().OPTIMIZE_MODEL` would assert on every recorded run that
# every engine's SQL had been LLM-optimized, whatever actually ran. A benchmark that
# misstates which engines were tuned is easily discredited, so the value is derived
# from evidence on disk (the per-platform optimization logs).


def _fake_log(tmp_path, monkeypatch, platform, model="claude-opus-4-8"):
    results = tmp_path / "results"
    results.mkdir(exist_ok=True)
    (results / f"optimization_{platform}.json").write_text(
        json.dumps({"platform": platform, "model": model})
    )
    monkeypatch.setattr(prov_mod, "_REPO_ROOT", tmp_path)


def test_only_platforms_with_an_optimization_log_are_recorded(tmp_path, monkeypatch):
    _fake_log(tmp_path, monkeypatch, "snowflake")

    got = optimization_provenance(["snowflake", "redshift", "bigquery", "clickhouse"])

    assert got == "snowflake:claude-opus-4-8", (
        "an engine with no optimization log must not be recorded as optimized"
    )


def test_no_optimized_platform_records_none(tmp_path, monkeypatch):
    _fake_log(tmp_path, monkeypatch, "snowflake")

    assert optimization_provenance(["redshift", "databricks"]) is None


def test_the_model_comes_from_the_log_not_the_setting(tmp_path, monkeypatch):
    """The log records the model that actually ran. Reading the setting instead is
    how a later config change would silently rewrite history."""
    _fake_log(tmp_path, monkeypatch, "snowflake", model="some-other-model")
    monkeypatch.setenv("BENCHMARK_OPTIMIZE_MODEL", "claude-opus-4-8")

    assert optimization_provenance(["snowflake"]) == "snowflake:some-other-model"


def test_an_unreadable_log_still_reports_the_platform_as_optimized(tmp_path, monkeypatch):
    """A corrupt log means the model is unknown, not that the pass never ran —
    dropping the platform entirely would understate the tuning asymmetry."""
    results = tmp_path / "results"
    results.mkdir()
    (results / "optimization_snowflake.json").write_text("{not json")
    monkeypatch.setattr(prov_mod, "_REPO_ROOT", tmp_path)

    assert optimization_provenance(["snowflake"]) == "snowflake:unknown-model"


def test_multiple_optimized_platforms_are_sorted_for_stability(tmp_path, monkeypatch):
    _fake_log(tmp_path, monkeypatch, "snowflake")
    _fake_log(tmp_path, monkeypatch, "bigquery", model="m2")

    assert optimization_provenance(["snowflake", "bigquery"]) == (
        "bigquery:m2,snowflake:claude-opus-4-8"
    )


def test_record_run_provenance_links_results(results_db):
    pid = results_db.record_run_provenance(
        command="sql",
        scale_factor=1,
        run_type="warm",
        platforms="bigquery",
        blocks="clickbench",
        git_sha="abc1234",
        git_dirty=True,
        harness_version="0.1.0",
        optimize_model="claude-opus-4-8",
        query_files_hash="deadbeef",
        tool_versions={"python": "3.14.6", "duckdb": "1.5.3"},
    )
    assert pid  # a UUID string

    results_db.record_sql_result(
        platform="bigquery",
        query_id="clickbench_q01",
        block="clickbench",
        scale_factor=1,
        run_type="warm",
        run_number=1,
        wall_clock_ms=100.0,
        server_time_ms=80.0,
        row_count=1,
        cost_usd=0.01,
        cost_confidence="direct",
        error_message=None,
        bytes_scanned=1_000_000,
        cache_hit_pct=0.0,
        platform_query_id="job-1",
        provenance_id=pid,
    )

    with results_db.connect() as con:
        # the result row links back to the provenance record
        joined = con.execute(
            """
            SELECT r.git_sha, r.git_dirty, b.bytes_scanned, b.cache_hit_pct
            FROM sql_benchmark_results b
            JOIN benchmark_run r ON r.provenance_id = b.provenance_id
            WHERE b.query_id = 'clickbench_q01'
            """
        ).fetchone()
    assert joined == ("abc1234", True, 1_000_000, 0.0)

"""Unit tests for report rendering helpers.

The report renders markdown tables straight from DuckDB rows (native Python
types) via ``_render_table`` — no pandas — so NULL is ``None`` (not float NaN)
and integer columns stay integers (not coerced to ``0.0``/``1.0``). These guard
the null-branch order and type formatting, plus the SQL-driven ``_query_table``.
"""

from __future__ import annotations

import duckdb

from benchmark.connectors.base import compute_label_from_environment
from benchmark.report.generator import (
    _block_in,
    _compute_descriptor,
    _query_table,
    _render_table,
)


def test_render_table_header_and_separator():
    lines = _render_table(["platform", "n"], [("snowflake", 3)])
    assert lines[0] == "| platform | n |"
    assert lines[1] == "| --- | --- |"


def test_render_table_empty_rows():
    assert _render_table(["a", "b"], []) == ["_(no data)_", ""]


def test_render_table_formats_floats_with_fmt():
    assert "| 12.50 |" in _render_table(["median_ms"], [(12.5,)])
    assert "| 12.500 |" in _render_table(["median_ms"], [(12.5,)], float_fmt=".3f")


def test_render_table_renders_none_as_emdash():
    # DuckDB yields Python None for NULL (not float NaN), so the null branch is a
    # plain identity check — no "nan" ever reaches the output.
    rendered = "\n".join(_render_table(["median_ms"], [(1.0,), (None,), (None,)]))
    assert "nan" not in rendered.lower()
    assert rendered.count("—") == 2


def test_render_table_keeps_ints_and_strings_unchanged():
    # Rendering without pandas keeps an int column as "4", never "4.0".
    rendered = "\n".join(_render_table(["platform", "runs"], [("bigquery", 4)]))
    assert "| bigquery | 4 |" in rendered


def test_compute_descriptor_per_engine():
    # Snowflake: size + warehouse generation (the SF1 X-Small vs SF10 Small case).
    assert (
        _compute_descriptor(
            {"platform": "snowflake", "warehouse_generation": "2", "actual": {"size": "Small"}}
        )
        == "Small (Gen2)"
    )
    # BigQuery: the ACTUAL (live) reservation decides slots vs on-demand; a
    # provisioned/run-plan slot count is intent and must NOT relabel an on-demand run.
    assert (
        _compute_descriptor(
            {"platform": "bigquery", "actual": {"edition": "ENTERPRISE", "slot_capacity": 100}}
        )
        == "ENTERPRISE / 100 slots"
    )
    assert (
        _compute_descriptor({"platform": "bigquery", "provisioned": {"reservation_slots": 100}})
        == "on-demand"
    )
    # MotherDuck duckling tier.
    assert (
        _compute_descriptor({"platform": "motherduck", "actual": {"size": "Standard"}})
        == "Standard"
    )
    # Unknown / empty falls back gracefully, never raises.
    assert _compute_descriptor({"platform": "who", "actual": {}, "provisioned": {}}) == "?"


def test_block_in_builds_family_predicate():
    # ClickBench is reported separately, so sections filter by block family.
    assert _block_in(("tpcds", "dsb")) == "block IN ('tpcds', 'dsb')"
    assert _block_in(("clickbench",)) == "block IN ('clickbench')"


def test_efficiency_rows_keeps_multiple_compute_sizes(results_db):
    # Same scale/query/run at two compute sizes must NOT collide — the size-aware
    # dedup keeps one row per size so the efficiency section can plot the frontier.
    from benchmark.report.generator import _efficiency_rows

    for label, ms in (("X-Small", 100.0), ("Small", 40.0)):
        results_db.record_sql_result(
            platform="snowflake",
            query_id="tpcds_q01",
            block="tpcds",
            scale_factor=10,
            run_type="warm",
            run_number=1,
            wall_clock_ms=ms + 5,
            server_time_ms=ms,
            row_count=1,
            cost_usd=0.01,
            cost_confidence="direct",
            error_message=None,
            compute_label=label,
        )
    with results_db.connect() as con:
        rows = _efficiency_rows(con, ("tpcds", "dsb"))
    by_label = {r[3]: r[5] for r in rows}  # compute_label -> median_server_ms
    assert by_label == {"X-Small": 100.0, "Small": 40.0}


def test_compute_label_from_environment_matches_descriptor():
    # The stamped label (runner) and the displayed descriptor (report) must agree so
    # rows without a stamped label COALESCE cleanly to the same string.
    env = {"platform": "snowflake", "warehouse_generation": "2", "actual": {"size": "Small"}}
    assert compute_label_from_environment(env) == "Small (Gen2)"
    assert compute_label_from_environment(env) == _compute_descriptor(env)
    # BigQuery reservation from the actual reading (slot_capacity).
    assert (
        compute_label_from_environment(
            {"platform": "bigquery", "actual": {"slot_capacity": 100, "edition": "ENTERPRISE"}}
        )
        == "ENTERPRISE / 100 slots"
    )
    # Redshift: the live boto3 reading emits base_capacity; the terraform slice
    # emits baseline_rpu. Both must label (and agree with the descriptor) so a
    # RPU band sweep records one distinct series per band instead of colliding.
    rs_actual = {"platform": "redshift", "actual": {"base_capacity": 32, "max_capacity": 32}}
    assert compute_label_from_environment(rs_actual) == "32 RPU"
    assert compute_label_from_environment(rs_actual) == _compute_descriptor(rs_actual)
    rs_prov = {"platform": "redshift", "actual": {}, "provisioned": {"baseline_rpu": 8}}
    assert compute_label_from_environment(rs_prov) == "8 RPU"
    assert compute_label_from_environment(rs_prov) == _compute_descriptor(rs_prov)
    # Databricks: the live REST reading emits cluster_size (reading only `size`
    # would leave every row's label NULL and the report unable to distinguish
    # warehouse sizes).
    db_env = {"platform": "databricks", "actual": {"cluster_size": "Medium"}}
    assert compute_label_from_environment(db_env) == "Medium"
    assert compute_label_from_environment(db_env) == _compute_descriptor(db_env)
    # Fabric: the live ARM reading emits capacity_sku.
    fab_env = {"platform": "fabric", "actual": {"capacity_sku": "F8"}}
    assert compute_label_from_environment(fab_env) == "F8"
    assert compute_label_from_environment(fab_env) == _compute_descriptor(fab_env)
    # No discernible size → None (row treated as default size, not an error).
    assert compute_label_from_environment({"platform": "snowflake", "actual": {}}) is None


def test_query_table_runs_sql_and_preserves_types():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (platform VARCHAR, scale_factor INTEGER, ms DOUBLE)")
    con.execute("INSERT INTO t VALUES ('snowflake', 10, 12.5), ('motherduck', 1, NULL)")
    lines = _query_table(con, "SELECT platform, scale_factor, ms FROM t ORDER BY platform")
    rendered = "\n".join(lines)
    assert lines[0] == "| platform | scale_factor | ms |"
    # int scale_factor stays "10"/"1" (not "10.0"); NULL double renders "—"
    assert "| motherduck | 1 | — |" in rendered
    assert "| snowflake | 10 | 12.50 |" in rendered

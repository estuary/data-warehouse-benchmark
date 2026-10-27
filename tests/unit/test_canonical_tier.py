"""Canonical compute-tier resolution: run_plan targets -> compute_label strings."""

from __future__ import annotations

import duckdb
import pytest

from benchmark.report.canonical import create_canonical_table, resolve_canonical


@pytest.fixture
def con():
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE sql_benchmark_results (
            platform VARCHAR, scale_factor INTEGER, compute_label VARCHAR,
            run_type VARCHAR DEFAULT 'warm')
    """)
    rows = [
        # snowflake: full sweep present at SF100
        *[
            ("snowflake", 100, lbl)
            for lbl in (
                "X-Small (Gen2)",
                "Small (Gen2)",
                "Medium (Gen2)",
                "Large (Gen2)",
                "X-Large (Gen2)",
            )
        ],
        # bigquery: SF1000 sweep has no 500-slot run (run-plan target)
        *[("bigquery", 1000, f"ENTERPRISE / {n} slots") for n in (200, 400, 600, 800, 1000)],
        ("bigquery", 1, "on-demand"),
        # clickhouse pinned label + pre-labeling NULL rows. Both a 2x and the
        # designated 1x rung are present at 100 GB: resolution must pick the
        # designated one, not merely something that exists.
        ("clickhouse", 100, "1x32GB"),
        ("clickhouse", 100, "2x32GB"),
        ("clickhouse", 1, None),
        # motherduck tiers
        *[("motherduck", 100, t) for t in ("Standard", "Pulse", "Jumbo", "Mega", "Giga")],
        ("redshift", 100, "16 RPU"),
    ]
    rows = [(*r, "warm") for r in rows]
    rows.append(("clickhouse", 100, "3x32GB", "concurrency"))
    con.executemany("INSERT INTO sql_benchmark_results VALUES (?, ?, ?, ?)", rows)
    return con


def test_designated_tiers_resolve_to_present_labels(con):
    m = resolve_canonical(con)
    assert m[("snowflake", 100, "warm")] == "Medium (Gen2)"  # run_plan: MEDIUM @SF100
    assert m[("motherduck", 100, "warm")] == "Jumbo"  # run_plan: jumbo @SF100
    assert m[("redshift", 100, "warm")] == "16 RPU"  # run_plan: 16 @SF100
    assert m[("clickhouse", 100, "warm")] == "1x32GB"  # 1 replica x pinned memory map
    assert m[("bigquery", 1, "warm")] == "on-demand"  # on_demand allowed at low scales


def test_bigquery_nearest_slots_fallback(con):
    # run_plan says 500 slots @SF1000; sweep ran 200..1000 — nearest is 400.
    m = resolve_canonical(con)
    assert m[("bigquery", 1000, "warm")] == "ENTERPRISE / 400 slots"


def test_absent_data_yields_no_entry(con):
    # clickhouse SF1 has only NULL labels -> no enforceable canonical entry
    # (bench view keeps all rows for that cell; NULL rows pass anyway).
    m = resolve_canonical(con)
    assert ("clickhouse", 1, "warm") not in m


def test_create_canonical_table_materializes(con):
    m = create_canonical_table(con)
    n = con.execute("SELECT COUNT(*) FROM _canonical_tier").fetchone()[0]
    assert n == len(m) > 0


def test_clickhouse_concurrency_class_uses_three_replicas(con):
    m = resolve_canonical(con)
    assert m[("clickhouse", 100, "concurrency")] == "3x32GB"

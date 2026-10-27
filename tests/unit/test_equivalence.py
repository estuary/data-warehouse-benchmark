"""Equivalence guardrails: output-arity tripwire + local execute-and-compare."""

from __future__ import annotations

import duckdb
import pytest

from benchmark import equivalence as eq
from benchmark.datagen.tpcds import TPCDS_TABLES
from benchmark.results import ResultsDB
from benchmark.sql_benchmark.validator import serialize_rows

# --- output-arity tripwire (pure) --------------------------------------------


def test_output_arity_counts_projection():
    assert eq.output_arity("SELECT a, b, c FROM t", "duckdb") == 3
    assert eq.output_arity("SELECT a FROM t", "duckdb") == 1


def test_output_arity_uses_first_branch_for_set_ops():
    assert eq.output_arity("SELECT a, b FROM t UNION SELECT c, d FROM u", "duckdb") == 2


def test_output_arity_none_when_unparseable_or_no_dialect():
    assert eq.output_arity("definitely (( not sql", "duckdb") is None
    assert eq.output_arity("SELECT a FROM t", None) is None


def test_arity_mismatch_detects_dropped_column():
    msg = eq.arity_mismatch("SELECT a, b FROM t", "SELECT a FROM t", "duckdb")
    assert msg is not None and "2 -> 1" in msg


def test_arity_mismatch_none_when_equal():
    assert eq.arity_mismatch("SELECT a, b FROM t", "SELECT b, a FROM t", "duckdb") is None


def test_arity_mismatch_none_when_either_side_unparseable():
    # A dialect gap on one side must never manufacture a false mismatch.
    assert eq.arity_mismatch("SELECT a FROM t", "garble (( not sql", "duckdb") is None


# --- local execute-and-compare gate ------------------------------------------


def _make_dataset(data_root, rows, scale=1):
    """Write a tiny SF<scale> TPC-DS-shaped Parquet set; store_sales holds rows."""
    suite_dir = data_root / "tpcds" / f"sf{scale}"
    suite_dir.mkdir(parents=True)
    con = duckdb.connect()
    try:
        values = ", ".join(f"({a}, '{b}')" for a, b in rows)
        for table in TPCDS_TABLES:
            out = suite_dir / f"{table}.parquet"
            con.execute(
                f"COPY (SELECT * FROM (VALUES {values}) AS v(a, b)) TO '{out}' (FORMAT PARQUET)"
            )
    finally:
        con.close()


def _seed_truth(db, query_id, rows, block="tpcds", scale=1, columns=None):
    import json as _json

    cols = _json.dumps(columns) if columns else None
    db.record_ground_truth(
        [
            (
                query_id,
                block,
                scale,
                "deadbeef",
                len(rows),
                1.0,
                None,
                "v1",
                serialize_rows(rows),
                cols,
            )
        ]
    )


@pytest.fixture
def db(tmp_path):
    return ResultsDB(tmp_path / "results.duckdb")


def test_check_equal(tmp_path, db):
    truth_rows = [(1, "x"), (2, "y"), (3, "z")]
    _make_dataset(tmp_path / "data", truth_rows)
    _seed_truth(db, "tpcds_q01", truth_rows)
    checker = eq.LocalEquivalenceChecker(db, data_root=tmp_path / "data")
    try:
        ev = checker.check(
            "tpcds_q01", "tpcds", "SELECT a, b FROM store_sales ORDER BY a", "duckdb"
        )
    finally:
        checker.close()
    assert ev.status == eq.EQUIV_EQUAL


def test_check_differ(tmp_path, db):
    _make_dataset(tmp_path / "data", [(1, "x"), (2, "y"), (3, "z")])
    _seed_truth(db, "tpcds_q01", [(1, "x"), (2, "DIFFERENT"), (3, "z")])
    checker = eq.LocalEquivalenceChecker(db, data_root=tmp_path / "data")
    try:
        ev = checker.check(
            "tpcds_q01", "tpcds", "SELECT a, b FROM store_sales ORDER BY a", "duckdb"
        )
    finally:
        checker.close()
    assert ev.status == eq.EQUIV_DIFFER


def test_check_defers_clickbench_when_no_data_or_gt(tmp_path, db):
    # No local hits.parquet and no ground truth -> defer to the live gate.
    checker = eq.LocalEquivalenceChecker(db, data_root=tmp_path / "data")
    ev = checker.check("clickbench_q01", "clickbench", "SELECT 1", "clickhouse")
    assert ev.status == eq.EQUIV_DEFERRED


def _make_clickbench(data_root, rows):
    """Write a tiny typed hits.parquet (EventTime as ISO VARCHAR + a value col)."""
    (data_root / "clickbench").mkdir(parents=True)
    out = data_root / "clickbench" / "hits.parquet"
    con = duckdb.connect()
    try:
        values = ", ".join(f"('{ts}', {x})" for ts, x in rows)
        con.execute(
            f"COPY (SELECT * FROM (VALUES {values}) AS v(EventTime, CounterID)) "
            f"TO '{out}' (FORMAT PARQUET)"
        )
    finally:
        con.close()


def test_check_clickbench_equal_uses_sf100_view(tmp_path, db):
    rows = [("2013-07-14T22:20:56Z", 5), ("2013-07-14T22:21:10Z", 9)]
    _make_clickbench(tmp_path / "data", rows)
    # Ground truth keyed at SF100 (the ClickBench scale), built the same way.
    _seed_truth(db, "clickbench_q01", [[5], [9]], block="clickbench", scale=100)
    checker = eq.LocalEquivalenceChecker(db, data_root=tmp_path / "data")
    try:
        ev = checker.check(
            "clickbench_q01",
            "clickbench",
            "SELECT CounterID FROM hits ORDER BY CounterID",
            "duckdb",
        )
    finally:
        checker.close()
    assert ev.status == eq.EQUIV_EQUAL


def test_check_clickbench_differ(tmp_path, db):
    _make_clickbench(tmp_path / "data", [("2013-07-14T22:20:56Z", 5)])
    _seed_truth(db, "clickbench_q01", [[999]], block="clickbench", scale=100)
    checker = eq.LocalEquivalenceChecker(db, data_root=tmp_path / "data")
    try:
        ev = checker.check("clickbench_q01", "clickbench", "SELECT CounterID FROM hits", "duckdb")
    finally:
        checker.close()
    assert ev.status == eq.EQUIV_DIFFER


def test_check_defers_when_no_ground_truth_rows(tmp_path, db):
    _make_dataset(tmp_path / "data", [(1, "x")])
    # No ground truth seeded for this id.
    checker = eq.LocalEquivalenceChecker(db, data_root=tmp_path / "data")
    try:
        ev = checker.check("tpcds_q02", "tpcds", "SELECT a, b FROM store_sales", "duckdb")
    finally:
        checker.close()
    assert ev.status == eq.EQUIV_DEFERRED


def test_check_defers_when_data_missing(tmp_path, db):
    _seed_truth(db, "tpcds_q01", [(1, "x")])
    checker = eq.LocalEquivalenceChecker(db, data_root=tmp_path / "empty")
    ev = checker.check("tpcds_q01", "tpcds", "SELECT a, b FROM store_sales", "duckdb")
    assert ev.status == eq.EQUIV_DEFERRED


def test_check_defers_when_execution_fails(tmp_path, db):
    _make_dataset(tmp_path / "data", [(1, "x")])
    _seed_truth(db, "tpcds_q01", [(1, "x")])
    checker = eq.LocalEquivalenceChecker(db, data_root=tmp_path / "data")
    try:
        # References a column that doesn't exist -> DuckDB raises -> deferred,
        # never a false "differ".
        ev = checker.check(
            "tpcds_q01", "tpcds", "SELECT nonexistent_col FROM store_sales", "duckdb"
        )
    finally:
        checker.close()
    assert ev.status == eq.EQUIV_DEFERRED

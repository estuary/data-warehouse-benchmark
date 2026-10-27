"""Order-aware, relative-tolerance result comparison."""

from __future__ import annotations

import decimal

from benchmark.sql_benchmark.validator import (
    NONDETERMINISTIC_QUERY_IDS,
    _clickbench_trusted,
    _has_order_by,
    _round_sig,
    compare_result_sets,
    deserialize_rows,
    hash_query_result,
    serialize_rows,
)


def test_clickbench_trusted_for_all_engines_except_fabric():
    # ClickBench ships vendor-approved per-engine queries for these — trusted,
    # not result-compared.
    for eng in ["snowflake", "redshift", "databricks", "bigquery", "clickhouse", "motherduck"]:
        assert _clickbench_trusted(eng, "clickbench") is True
    # Fabric has no ClickBench variant → we translate it → we DO validate it.
    assert _clickbench_trusted("fabric", "clickbench") is False
    # Non-ClickBench blocks are always result-validated, never "trusted".
    assert _clickbench_trusted("snowflake", "tpcds") is False
    assert _clickbench_trusted("snowflake", "dsb") is False


def test_nondeterministic_set_contents():
    # 8 ClickBench top-N (no total order), q59 both suites (ORDER BY ties cut
    # by LIMIT), q67 both suites (RANK over float SUMs; ULP summation-order
    # ties/splits — see NONDETERMINISTIC_QUERY_IDS comments).
    expected = frozenset(
        {f"clickbench_q{n}" for n in (18, 25, 32, 33, 39, 40, 41, 42)}
        | {"tpcds_q59", "dsb_q59", "tpcds_q67", "dsb_q67"}
    )
    assert expected == NONDETERMINISTIC_QUERY_IDS


def test_compare_aligns_columns_by_name():
    # Engine returns columns in a different order + an extra _meta column;
    # alignment by name should reorder/drop so it matches the reference.
    truth_cols = ["WatchID", "Title", "EventTime"]
    truth_rows = [[1, "a", "2013-07-01T00:00:00"], [2, "b", "2013-07-02T00:00:00"]]
    # Engine: alphabetical-ish order, extra _meta/file, uppercased names.
    engine_cols = ["EVENTTIME", "_meta/file", "TITLE", "WATCHID"]
    engine_rows = [
        ["2013-07-01T00:00:00", "s3://x", "a", 1],
        ["2013-07-02T00:00:00", "s3://y", "b", 2],
    ]
    passed, reason = compare_result_sets(
        engine_rows,
        truth_rows,
        "SELECT * FROM hits",
        engine_columns=engine_cols,
        truth_columns=truth_cols,
    )
    assert passed, reason


def test_compare_column_alignment_detects_real_value_mismatch():
    truth_cols = ["a", "b"]
    truth_rows = [[1, "x"]]
    engine_cols = ["b", "a"]
    engine_rows = [["WRONG", 1]]  # b should be 'x'
    passed, _ = compare_result_sets(
        engine_rows,
        truth_rows,
        "SELECT * FROM t",
        engine_columns=engine_cols,
        truth_columns=truth_cols,
    )
    assert not passed


def test_compare_falls_back_to_positional_without_columns():
    # No column names -> positional comparison (ground truth stored without them).
    assert compare_result_sets([[1, 2]], [[1, 2]], "q")[0]
    assert not compare_result_sets([[2, 1]], [[1, 2]], "q")[0]


def test_round_sig_is_relative():
    assert _round_sig(0.0) == "0"
    assert _round_sig(12345678.90) == "12345700"  # 6 significant figures
    assert _round_sig(1.23456789) == "1.23457"


def test_has_order_by():
    assert _has_order_by("SELECT * FROM t ORDER BY x")
    assert not _has_order_by("SELECT x FROM t")


def test_no_order_by_compares_as_multiset():
    a = [(2, "b"), (1, "a")]
    b = [(1, "a"), (2, "b")]
    assert hash_query_result(a, "SELECT x, y FROM t") == hash_query_result(b, "SELECT x, y FROM t")


def test_order_by_is_order_significant():
    a = [(2,), (1,)]
    b = [(1,), (2,)]
    sql = "SELECT x FROM t ORDER BY x"
    assert hash_query_result(a, sql) != hash_query_result(b, sql)


def test_relative_float_tolerance():
    sql = "SELECT s FROM t"
    v1 = [(decimal.Decimal("12345678.90"),)]
    v2 = [(decimal.Decimal("12345678.91"),)]  # sub-tolerance drift -> equal
    assert hash_query_result(v1, sql) == hash_query_result(v2, sql)
    v3 = [(decimal.Decimal("12463135.0"),)]  # ~1% difference -> not equal
    assert hash_query_result(v1, sql) != hash_query_result(v3, sql)


# --- tolerance-aware element-wise comparison (compare_result_sets) ----------


def test_serialize_round_trip_preserves_comparison():
    rows = [(decimal.Decimal("421.9975"), "x", None)]
    blob = serialize_rows(rows)
    truth = deserialize_rows(blob)
    passed, reason = compare_result_sets(rows, truth, "SELECT a, b, c FROM t")
    assert passed and reason == "match"


def test_boundary_rounding_no_longer_false_positive():
    # The real dsb_q13 case: DuckDB's exact decimal vs the engine's float repr.
    # These straddle a 6-sig-fig rounding boundary (hash differs) but are equal
    # within 1e-4, so element-wise comparison must accept them.
    truth = deserialize_rows(serialize_rows([(decimal.Decimal("421.9975"),)]))
    engine = [(421.99749999999995,)]
    sql = "SELECT avg(x) FROM t"
    # hash over-reports...
    assert hash_query_result(engine, sql) != hash_query_result(
        [(decimal.Decimal("421.9975"),)], sql
    )
    # ...but the tolerance comparison passes.
    passed, reason = compare_result_sets(engine, truth, sql)
    assert passed and reason == "match"


def test_value_difference_beyond_tolerance_fails():
    truth = deserialize_rows(serialize_rows([(100.0,)]))
    engine = [(101.0,)]  # 1% > 1e-4
    passed, reason = compare_result_sets(engine, truth, "SELECT avg(x) FROM t")
    assert not passed and reason == "value mismatch"


def test_row_count_mismatch_fails():
    truth = deserialize_rows(serialize_rows([(1,), (2,)]))
    passed, reason = compare_result_sets([(1,)], truth, "SELECT x FROM t")
    assert not passed and reason == "row count mismatch"


def test_unordered_compares_as_multiset():
    truth = deserialize_rows(serialize_rows([(2, "b"), (1, "a")]))
    engine = [(1, "a"), (2, "b")]
    passed, _ = compare_result_sets(engine, truth, "SELECT x, y FROM t")
    assert passed


def test_set_difference_fails_regardless_of_limit():
    # A genuinely different selected row is a mismatch — there is NO LIMIT
    # special-casing (an ORDER BY … LIMIT query that picks different rows at the
    # cutoff is a real difference, and masking it would also hide real defects).
    truth_rows = [(i, "k") for i in range(100)]
    engine_rows = [(i, "k") for i in range(98)] + [(200, "k"), (201, "k")]
    truth = deserialize_rows(serialize_rows(truth_rows))
    for sql in ("SELECT x, y FROM t ORDER BY y LIMIT 100", "SELECT x, y FROM t"):
        passed, reason = compare_result_sets(engine_rows, truth, sql)
        assert not passed and reason == "value mismatch"


def test_beyond_tolerance_on_limit_query_still_fails():
    # The real dsb_q83 shape: a LIMIT query, but a numeric column differs beyond
    # 1e-4. Must fail — the "contains LIMIT" string must not excuse it.
    truth = deserialize_rows(serialize_rows([("item", 0.0365764)]))
    engine = [("item", 0.0365667)]  # ~2.7e-4 relative -> beyond tolerance
    passed, reason = compare_result_sets(
        engine, truth, "SELECT id, dev FROM t ORDER BY id LIMIT 100"
    )
    assert not passed and reason == "value mismatch"

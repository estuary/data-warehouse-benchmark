"""The report must be reproducible from a fixed database.

Two hazards can make published figures change between `benchmark report` runs on
identical data. Neither is visible by inspection, since each value is individually
plausible, yet a reader recomputing our numbers would find different ones. These
tests pin both.

1. **A NULL `compute_label` must not borrow a real compute size.** Rendering a
   row's label as ``label or layout.get((platform, scale))`` puts unlabelled rows
   on the same pivot key as a genuine size, and the last writer wins. Which one
   that is depends on row order, which the SQL does not constrain: a Redshift
   128 RPU concurrency cell at 100 GB can alternate between a real 402-row
   measurement (519 ms at 10 sessions) and a 102-row unlabelled sample of unknown
   compute size (1,658 ms).

2. **DOUBLE aggregation is not associative.** DuckDB parallelizes SUM/AVG, so a
   201-term float sum varies in its low bits run to run, enough to move a
   displayed value across a rounding boundary ($0.73 vs $0.74).
"""

from __future__ import annotations

import duckdb
import pytest

from benchmark.report import generator

_COLS = (
    "platform VARCHAR, query_id VARCHAR, block VARCHAR, scale_factor INTEGER, "
    "run_type VARCHAR, run_number INTEGER, concurrency INTEGER, compute_label VARCHAR, "
    "wall_clock_ms DOUBLE, server_time_ms DOUBLE, cost_usd DOUBLE, "
    "cost_confidence VARCHAR, row_count BIGINT, bytes_scanned BIGINT, "
    "error_message VARCHAR, created_at TIMESTAMP"
)


def test_a_null_compute_label_never_borrows_a_real_size():
    """Hazard 1. A row with no captured size must not be attributed to one,
    because that silently changes the real size's published figures."""
    assert generator._compute_row(None, "128 RPU") == generator.UNLABELLED_COMPUTE
    assert generator._compute_row("", "128 RPU") == generator.UNLABELLED_COMPUTE
    assert generator._compute_row("64 RPU", "128 RPU") == "64 RPU"


def test_unlabelled_rows_are_accounted_for_rather_than_dropped():
    """Withholding is not the same as hiding: the reader is told how much
    measurement sits outside the size-aware comparison."""
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE main.sql_benchmark_results ({_COLS})")
    for i in range(5):
        con.execute(
            "INSERT INTO main.sql_benchmark_results VALUES ('redshift', ?, 'tpcds', 100,"
            " 'warm', 1, NULL, NULL, 100.0, 90.0, 0.01, 'proportional', 1, 1000, NULL,"
            " '2026-08-01 00:00:00')",
            [f"tpcds_q{i:02d}"],
        )

    note = generator._unlabelled_note(con, ("tpcds", "dsb"))

    assert note, "unlabelled rows must be disclosed, not silently excluded"
    assert "Redshift SF100 (5)" in note[0], "the note names engines in vendor casing"
    con.close()


def test_no_unlabelled_rows_means_no_note():
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE main.sql_benchmark_results ({_COLS})")
    con.execute(
        "INSERT INTO main.sql_benchmark_results VALUES ('redshift', 'tpcds_q01', 'tpcds',"
        " 100, 'warm', 1, NULL, '128 RPU', 100.0, 90.0, 0.01, 'proportional', 1, 1000,"
        " NULL, '2026-08-01 00:00:00')"
    )

    assert generator._unlabelled_note(con, ("tpcds", "dsb")) == []
    con.close()


def test_group_by_binds_the_base_column_not_an_alias():
    """Why the concurrency query aliases to `scale_key` rather than `scale_factor`.

    SQL resolves GROUP BY against the FROM clause first, so aliasing an expression
    to an existing column name leaves the grouping on the *base* column while the
    projection shows the expression. For ClickBench that yields three groups (scale
    tags 1/10/100) all projecting 100, which collide on one pivot key with an
    arbitrary winner. This pins the engine behaviour the distinct alias relies on.
    """
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (sf INTEGER, v INTEGER)")
    con.execute("INSERT INTO t VALUES (1, 10), (10, 20), (100, 30)")

    shadowed = con.execute("SELECT 100 AS sf, median(v) FROM t GROUP BY sf").fetchall()
    distinct = con.execute("SELECT 100 AS sf_key, median(v) FROM t GROUP BY sf_key").fetchall()

    assert len(shadowed) == 3, "aliasing to an existing name does NOT change the grouping"
    assert len(distinct) == 1, "a distinct alias groups by the expression, as intended"
    con.close()


@pytest.mark.parametrize("column", ["cost_usd", "server_time_ms"])
def test_decimal_aggregation_is_reproducible_where_double_is_not(column):
    """Why displayed money/duration aggregates cast to DECIMAL first.

    Float addition is not associative, so a parallel DOUBLE SUM can differ in its
    low bits between runs. The DECIMAL sum is exact and therefore stable — the
    property the report's reproducibility rests on.
    """
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE t ({column} DOUBLE)")
    # Values chosen so the exact sum lands on a 2-decimal rounding boundary.
    con.executemany("INSERT INTO t VALUES (?)", [(0.007350123456789,)] * 100)

    exact = {
        con.execute(f"SELECT SUM(CAST({column} AS DECIMAL(28,12))) FROM t").fetchone()[0]
        for _ in range(20)
    }

    assert len(exact) == 1, "DECIMAL aggregation must be bit-stable across evaluations"
    con.close()


def _sql_lines(module) -> list[str]:
    """Source lines excluding comments.

    The guards below scan for SQL shapes that must not appear. Comments *describe*
    those shapes on purpose, so scanning them too makes the guard fail on its own
    documentation.
    """
    with open(module.__file__, encoding="utf-8") as fh:
        return [line for line in fh.read().splitlines() if not line.lstrip().startswith("#")]


def test_the_decimal_cast_is_declared_once():
    """A second precision spelling would bring the drift back in one table only,
    which is harder to notice than drift everywhere."""
    assert generator._DECIMAL == "DECIMAL(28,12)"
    lines = _sql_lines(generator)
    assert any("AS DECIMAL(" in line for line in lines)
    other = [line for line in lines if "AS DECIMAL(" in line and generator._DECIMAL not in line]
    assert not other, f"inconsistent DECIMAL precision in: {other}"


def test_avg_over_decimal_is_not_exact_so_the_mean_must_be_spelled_out():
    """The subtle half of the DECIMAL rule.

    DuckDB's AVG returns DOUBLE even for a DECIMAL input, so wrapping the input in a
    cast does nothing — the accumulation is order-dependent again. A mean has to be
    written SUM(CAST(x AS DECIMAL)) / NULLIF(COUNT(x), 0), and where that mean feeds
    an outer SUM it must be cast back, or the outer sum re-enters float accumulation.
    """
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (v DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?)", [(0.1,), (0.2,), (0.3,)])

    def result_type(expr: str) -> str:
        # description carries a duckdb type object; str() gives its SQL name.
        return str(con.execute(f"SELECT {expr} FROM t").description[0][1])

    assert result_type("AVG(CAST(v AS DECIMAL(28,12)))") == "DOUBLE", (
        "if this ever returns DECIMAL, the workaround can be simplified"
    )
    assert result_type("SUM(CAST(v AS DECIMAL(28,12)))").startswith("DECIMAL")
    con.close()


def test_no_avg_over_decimal_survives_in_the_report_sql():
    """Guards against the ineffective cast anywhere in the report path."""
    from benchmark.report import verifiers

    for module in (generator, verifiers):
        offenders = [line.strip() for line in _sql_lines(module) if "AVG(CAST(" in line]
        assert not offenders, (
            f"{module.__name__} averages over a DECIMAL cast, which is not exact — "
            f"spell the mean as SUM(CAST(...)) / NULLIF(COUNT(...), 0): {offenders}"
        )

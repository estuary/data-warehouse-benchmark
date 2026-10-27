"""Full-pipeline E2E smoke: generation → load → benchmark → analyze → report.

Runs the entire SQL path for ONE warehouse at small scale against a local DuckDB
stand-in — no cloud credentials. It mirrors the operator sequence for a real
engine (generate-tpcds → stage/load → sql → analyze → report) using the *real*
harness entry points at each stage, so a break anywhere in the chain fails here.

Scale via ``BENCHMARK_TEST_SF`` (default 0.01 for speed; set 1 for true SF1).
Opt-in: ``uv run pytest --run-integration tests/integration/test_e2e_pipeline.py``.

To smoke a *real* warehouse instead, the same sequence runs via the CLI:
``generate-tpcds`` → ``stage-data`` + Estuary load → ``sql`` → ``analyze`` →
``report``; this test pins the warehouse to local DuckDB so it needs no creds.
"""

from __future__ import annotations

import os

import duckdb
import pytest

from benchmark.analysis.curves import emit_cost_curves
from benchmark.analysis.patterns import emit_pattern_summary
from benchmark.analysis.rankings import emit_rankings
from benchmark.analysis.skew import emit_skew_impact
from benchmark.analysis.skew_load import emit_skew_load
from benchmark.queries.registry import BenchmarkQuery
from benchmark.report.generator import generate_report
from benchmark.results import ResultsDB
from benchmark.sql_benchmark import runner as runner_mod
from benchmark.sql_benchmark.runner import run_sql_benchmark
from benchmark.sql_benchmark.validator import hash_query_result, serialize_rows

pytestmark = pytest.mark.integration

TEST_SF = float(os.environ.get("BENCHMARK_TEST_SF", "0.01"))
PLATFORM = "local_duckdb"

# Representative queries over the loaded warehouse tables (store_sales + item):
# a scan, an aggregation, and a join — enough to give the analysis stage signal.
QUERIES = [
    BenchmarkQuery(
        id="tpcds_q01",
        block="tpcds",
        number=1,
        sql="SELECT COUNT(*) FROM store_sales",
        patterns=("scan",),
    ),
    BenchmarkQuery(
        id="tpcds_q02",
        block="tpcds",
        number=2,
        sql="SELECT ss_store_sk, COUNT(*) AS n FROM store_sales "
        "GROUP BY ss_store_sk ORDER BY ss_store_sk",
        patterns=("aggregation",),
    ),
    BenchmarkQuery(
        id="tpcds_q03",
        block="tpcds",
        number=3,
        sql="SELECT i_category, COUNT(*) AS n FROM store_sales "
        "JOIN item ON ss_item_sk = i_item_sk GROUP BY i_category ORDER BY i_category",
        patterns=("join",),
    ),
]


class _StubRegistry:
    def __init__(self, queries):
        self._queries = queries

    def all(self):
        return self._queries


def _generate_parquet(sf: float, data_dir):
    """Generate TPC-DS at small scale and export Parquet."""
    data_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute("INSTALL tpcds; LOAD tpcds;")
    con.execute(f"CALL dsdgen(sf={sf})")
    counts = {}
    for table in ("store_sales", "item"):
        path = data_dir / f"{table}.parquet"
        con.execute(f"COPY {table} TO '{path.as_posix()}' (FORMAT PARQUET)")
        counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.close()
    return counts


def _load_warehouse(data_dir, warehouse_path):
    """Load analog: load the staged Parquet into the (local DuckDB) warehouse."""
    con = duckdb.connect(str(warehouse_path))
    loaded = {}
    for parquet in sorted(data_dir.glob("*.parquet")):
        table = parquet.stem
        con.execute(
            f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_parquet('{parquet.as_posix()}')"
        )
        loaded[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.close()
    return loaded


def _seed_ground_truth(warehouse_path, results_db):
    con = duckdb.connect(str(warehouse_path), read_only=True)
    rows = []
    for q in QUERIES:
        result = [tuple(r) for r in con.execute(q.sql).fetchall()]
        rows.append(
            (
                q.id,
                q.block,
                1,
                hash_query_result(result, q.sql),
                len(result),
                0.0,
                None,
                "e2e",
                serialize_rows(result),
                None,
            )
        )
    con.close()
    results_db.record_ground_truth(rows)


def test_e2e_generation_to_analysis(tmp_path, fake_cost, local_connector, monkeypatch):
    monkeypatch.setenv("MPLBACKEND", "Agg")  # headless chart rendering
    data_dir = tmp_path / "data" / "tpcds"
    warehouse = tmp_path / "warehouse.duckdb"
    db_path = tmp_path / "benchmark.duckdb"
    results = ResultsDB(db_path)

    # 1. GENERATE -------------------------------------------------------------
    gen_counts = _generate_parquet(TEST_SF, data_dir)
    assert gen_counts["store_sales"] > 0 and gen_counts["item"] > 0
    assert (data_dir / "store_sales.parquet").exists()

    # 2. LOAD (Parquet -> warehouse) -----------------------------------------
    loaded = _load_warehouse(data_dir, warehouse)
    assert loaded == gen_counts  # row counts preserved generation -> load

    # 3. GROUND TRUTH ---------------------------------------------------------
    _seed_ground_truth(warehouse, results)

    # 4. BENCHMARK (the real runner against the warehouse) --------------------
    monkeypatch.setattr(runner_mod, "QueryRegistry", lambda dialect=None: _StubRegistry(QUERIES))
    run_sql_benchmark(
        PLATFORM,
        local_connector(warehouse),
        fake_cost(cost=0.01),
        results,
        scale_factor=1,
        blocks=["tpcds"],
        num_warmups=1,
        num_measured=2,
    )
    with results.connect() as con:
        (recorded,) = con.execute(
            "SELECT COUNT(*) "
            "FROM sql_benchmark_results WHERE platform = ? AND error_message IS NULL",
            [PLATFORM],
        ).fetchone()
    assert recorded == len(QUERIES) * 2  # 2 measured runs per query
    # Correctness is verified by validate-sf1, not the runner.

    # 5. ANALYZE (the real export functions) ---------------------------------
    exports = tmp_path / "exports"
    produced = [
        emit_cost_curves(db_path, exports / "cost_curves.parquet"),
        emit_pattern_summary(db_path, exports / "pattern_summary.parquet"),
        emit_rankings(db_path, exports / "rankings.parquet"),
        emit_skew_impact(db_path, exports / "skew_impact.parquet"),
        emit_skew_load(db_path, exports / "skew_load.parquet"),
    ]
    assert all(p.exists() for p in produced)

    # 6. REPORT ---------------------------------------------------------------
    # Generated body only: the hand-authored narrative's {{fact:...}} placeholders
    # are editorial claims about the real results DB and (correctly) fail loud on a
    # synthetic one — this smoke validates the machinery, not the prose.
    draft = generate_report(db_path, tmp_path / "report", compose_narrative=False)
    assert draft.exists()
    report_text = draft.read_text(encoding="utf-8")
    assert "Coverage" in report_text and PLATFORM in report_text

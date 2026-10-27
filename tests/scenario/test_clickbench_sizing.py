"""ClickBench is a single ~100 GB dataset, but the benchmark records it under
each *run tier* so the same workload exercises every engine's per-scale compute
size (warehouse/RPU/slots) — the engine-sizing curve. The runner records
latency/cost only; correctness is verified separately by validate-sf1.
"""

from __future__ import annotations

from benchmark.cli import _block_specs
from benchmark.queries.registry import BenchmarkQuery
from benchmark.sql_benchmark import runner as runner_mod
from benchmark.sql_benchmark.runner import run_sql_benchmark


def test_block_specs_clickbench_follows_run_tier_with_fixed_schema():
    # TPC-DS / DSB: both schema and scale_factor track the tier.
    assert _block_specs(1000)["tpcds"] == ("tpcds_sf1000", 1000)
    assert _block_specs(1000)["dsb"] == ("dsb_sf1000", 1000)
    # ClickBench: schema fixed (single 100 GB load), scale_factor tracks the
    # tier so it's recorded per tier.
    assert _block_specs(1)["clickbench"] == ("clickbench", 1)
    assert _block_specs(100)["clickbench"] == ("clickbench", 100)
    assert _block_specs(1000)["clickbench"] == ("clickbench", 1000)


class _StubRegistry:
    def __init__(self, queries: list[BenchmarkQuery]) -> None:
        self._queries = queries

    def all(self) -> list[BenchmarkQuery]:
        return self._queries


def test_clickbench_records_at_run_tier(monkeypatch, fake_connector, fake_cost, results_db):
    sql, rows = "SELECT 1", [(1,)]
    q = BenchmarkQuery(id="clickbench_q01", block="clickbench", number=1, sql=sql, patterns=())
    monkeypatch.setattr(runner_mod, "QueryRegistry", lambda dialect=None: _StubRegistry([q]))

    # Run at the SF1000 tier (an engine-sizing pass) — the single 100 GB
    # ClickBench dataset is recorded under whatever tier exercises it.
    run_sql_benchmark(
        "fake",
        fake_connector(rows=rows),
        fake_cost(),
        results_db,
        scale_factor=1000,
        blocks=["clickbench"],
        num_warmups=1,
        num_measured=1,
    )

    with results_db.connect() as con:
        recorded = con.execute(
            "SELECT scale_factor FROM sql_benchmark_results WHERE query_id = 'clickbench_q01'"
        ).fetchall()
    # Recorded under the run tier (1000). Correctness is validate-sf1's job.
    assert recorded == [(1000,)]


def test_engine_environment_recorded_once_per_run(
    monkeypatch, fake_connector, fake_cost, results_db
):
    q = BenchmarkQuery(id="tpcds_q01", block="tpcds", number=1, sql="SELECT 1", patterns=())
    monkeypatch.setattr(runner_mod, "QueryRegistry", lambda dialect=None: _StubRegistry([q]))

    run_sql_benchmark(
        "fake",
        fake_connector(rows=[(1,)], environment={"platform": "fake", "warehouse": "WH_LARGE"}),
        fake_cost(),
        results_db,
        scale_factor=1000,
        blocks=["tpcds"],
        num_warmups=1,
        num_measured=1,
    )

    # Captured once, keyed by (platform, scale_factor, run_type), with the
    # connector's resolved compute identity. `actual` is always present on a
    # successful run: the runner refuses to record a cell whose capture yields
    # no compute-size label.
    assert results_db.get_engine_environment("fake", 1000, "warm") == {
        "platform": "fake",
        "warehouse": "WH_LARGE",
        "actual": {"size": "fake-size"},
    }


def test_provisioned_compute_merged_into_environment(
    monkeypatch, fake_connector, fake_cost, results_db
):
    q = BenchmarkQuery(id="tpcds_q01", block="tpcds", number=1, sql="SELECT 1", patterns=())
    monkeypatch.setattr(runner_mod, "QueryRegistry", lambda dialect=None: _StubRegistry([q]))
    # terraform-provisioned compute for this scale is merged under "provisioned".
    monkeypatch.setattr(
        runner_mod, "provisioned_compute", lambda platform, scale: {"warehouse_size": "LARGE"}
    )

    run_sql_benchmark(
        "fake",
        fake_connector(rows=[(1,)], environment={"platform": "fake"}),
        fake_cost(),
        results_db,
        scale_factor=1000,
        blocks=["tpcds"],
        num_warmups=1,
        num_measured=1,
    )

    assert results_db.get_engine_environment("fake", 1000, "warm") == {
        "platform": "fake",
        "actual": {"size": "fake-size"},
        "provisioned": {"warehouse_size": "LARGE"},
    }


def test_compute_matches_per_engine():
    from benchmark.sql_benchmark.runner import _compute_matches

    # Label comparison (normalized) — snowflake / databricks / motherduck.
    assert _compute_matches("snowflake", {"warehouse_size": "LARGE"}, {"size": "Large"}) is True
    assert _compute_matches("snowflake", {"warehouse_size": "XSMALL"}, {"size": "X-Small"}) is True
    assert _compute_matches("snowflake", {"warehouse_size": "MEDIUM"}, {"size": "Large"}) is False
    assert (
        _compute_matches("databricks", {"warehouse_size": "Small"}, {"cluster_size": "Small"})
        is True
    )
    assert _compute_matches("motherduck", {"tier": "jumbo"}, {"size": "Jumbo"}) is True
    # Integer comparison — bigquery / redshift.
    assert _compute_matches("bigquery", {"reservation_slots": 100}, {"slot_capacity": 100}) is True
    assert _compute_matches("bigquery", {"reservation_slots": 100}, {"slot_capacity": 200}) is False
    assert _compute_matches("redshift", {"baseline_rpu": 8}, {"base_capacity": 8}) is True
    # Cloud-API engines: Fabric SKU (label) + ClickHouse replicas (int).
    assert _compute_matches("fabric", {"capacity_sku": "f64"}, {"capacity_sku": "F64"}) is True
    assert _compute_matches("fabric", {"capacity_sku": "f64"}, {"capacity_sku": "F32"}) is False
    assert _compute_matches("clickhouse", {"num_replicas": 3}, {"num_replicas": 3}) is True
    assert _compute_matches("clickhouse", {"num_replicas": 3}, {"num_replicas": 2}) is False
    # Missing either side -> None (no expectation declared / query failed).
    assert _compute_matches("clickhouse", {}, {"num_replicas": 3}) is None
    assert _compute_matches("snowflake", {}, {"size": "Large"}) is None
    assert _compute_matches("snowflake", {"warehouse_size": "LARGE"}, {}) is None


def test_runtime_compute_validated_against_provisioned(
    monkeypatch, fake_connector, fake_cost, results_db
):
    q = BenchmarkQuery(id="tpcds_q01", block="tpcds", number=1, sql="SELECT 1", patterns=())
    monkeypatch.setattr(runner_mod, "QueryRegistry", lambda dialect=None: _StubRegistry([q]))
    monkeypatch.setattr(
        runner_mod, "provisioned_compute", lambda platform, scale: {"warehouse_size": "LARGE"}
    )

    run_sql_benchmark(
        "snowflake",
        fake_connector(environment={"platform": "snowflake"}, compute={"size": "Large"}),
        fake_cost(),
        results_db,
        scale_factor=1000,
        blocks=["tpcds"],
        num_warmups=1,
        num_measured=1,
    )

    env = results_db.get_engine_environment("snowflake", 1000, "warm")
    assert env["actual"] == {"size": "Large"}
    assert env["provisioned"] == {"warehouse_size": "LARGE"}
    assert env["compute_validated"] is True


def test_runtime_compute_mismatch_is_flagged(monkeypatch, fake_connector, fake_cost, results_db):
    q = BenchmarkQuery(id="tpcds_q01", block="tpcds", number=1, sql="SELECT 1", patterns=())
    monkeypatch.setattr(runner_mod, "QueryRegistry", lambda dialect=None: _StubRegistry([q]))
    monkeypatch.setattr(
        runner_mod, "provisioned_compute", lambda platform, scale: {"warehouse_size": "MEDIUM"}
    )

    run_sql_benchmark(
        "snowflake",
        fake_connector(environment={"platform": "snowflake"}, compute={"size": "Large"}),
        fake_cost(),
        results_db,
        scale_factor=1000,
        blocks=["tpcds"],
        num_warmups=1,
        num_measured=1,
    )

    assert (
        results_db.get_engine_environment("snowflake", 1000, "warm")["compute_validated"] is False
    )

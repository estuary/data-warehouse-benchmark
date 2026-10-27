"""Base ``runtime_environment()`` — the portable fields every connector reports.

Per-connector overrides add compute identity (Snowflake warehouse, BigQuery
region, Fabric component, ...); the capture round-trip + wiring is covered in
tests/scenario/test_clickbench_sizing.py.
"""

from __future__ import annotations

from benchmark.connectors.base import QueryResult, WarehouseConnector


class _MiniConn(WarehouseConnector):
    """Minimal concrete connector to exercise the base default."""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def execute_query(self, sql: str) -> QueryResult:  # pragma: no cover - unused
        return QueryResult(rows=[], columns=[], row_count=0, wall_clock_ms=0.0)

    def qualify_table(self, schema: str, table: str) -> str:
        return f"{schema}.{table}"


def test_base_runtime_environment_returns_portable_fields():
    conn = _MiniConn("mini", {"scale_factor": 100, "schema": "tpcds_sf100", "suite": "tpcds"})
    assert conn.runtime_environment() == {
        "platform": "mini",
        "scale_factor": 100,
        "schema": "tpcds_sf100",
        "suite": "tpcds",
    }


def test_base_runtime_environment_omits_absent_config():
    # Only platform is guaranteed; absent/None config keys are skipped.
    assert _MiniConn("mini", {}).runtime_environment() == {"platform": "mini"}
    assert _MiniConn("mini", {"scale_factor": None}).runtime_environment() == {"platform": "mini"}


def test_query_scalar_is_resilient_to_failures():
    """Version-enrichment queries must degrade to None, never raise — a flaky
    version probe can't be allowed to abort a benchmark pass."""

    class _Boom(_MiniConn):
        def execute_query(self, sql: str) -> QueryResult:
            raise RuntimeError("boom")

    class _Err(_MiniConn):
        def execute_query(self, sql: str) -> QueryResult:
            return QueryResult(
                rows=[], columns=[], row_count=0, wall_clock_ms=0.0, error_message="nope"
            )

    class _Ok(_MiniConn):
        def execute_query(self, sql: str) -> QueryResult:
            return QueryResult(rows=[("9.1.0",)], columns=["v"], row_count=1, wall_clock_ms=0.0)

    assert _Boom("x", {})._query_scalar("SELECT version()") is None  # raised -> None
    assert _Err("x", {})._query_scalar("SELECT version()") is None  # error_message -> None
    assert _MiniConn("x", {})._query_scalar("SELECT version()") is None  # empty rows -> None
    assert _Ok("x", {})._query_scalar("SELECT version()") == "9.1.0"


def test_base_runtime_compute_is_empty_by_default():
    # Engines without a SQL-queryable actual size return {} (treated as "not
    # validatable" by the runtime-vs-provisioned check).
    assert _MiniConn("mini", {"scale_factor": 100}).runtime_compute() == {}


def test_snowflake_runtime_compute_parses_show_warehouses(monkeypatch):
    from benchmark.connectors.snowflake import SnowflakeConnector

    conn = SnowflakeConnector(config={"scale_factor": 1000})
    conn.warehouse = "BENCHMARK_WH_LARGE"

    def _fake_show(sql: str) -> QueryResult:
        assert "SHOW WAREHOUSES" in sql
        return QueryResult(
            rows=[("BENCHMARK_WH_LARGE", "STARTED", "Large", 1, 2, "STANDARD")],
            columns=["name", "state", "size", "min_cluster_count", "max_cluster_count", "type"],
            row_count=1,
            wall_clock_ms=1.0,
        )

    monkeypatch.setattr(conn, "execute_query", _fake_show)
    assert conn.runtime_compute() == {
        "size": "Large",
        "min_cluster_count": 1,
        "max_cluster_count": 2,
        "type": "STANDARD",
    }


def test_snowflake_runtime_compute_empty_without_warehouse():
    from benchmark.connectors.snowflake import SnowflakeConnector

    # Pre-connect: no resolved warehouse -> nothing to query, no false validation.
    assert SnowflakeConnector(config={}).runtime_compute() == {}


def test_bigquery_runtime_compute_parses_reservation(monkeypatch):
    from benchmark.connectors.bigquery import BigQueryConnector

    conn = BigQueryConnector(config={})
    conn.region = "US"

    def _fake(sql: str) -> QueryResult:
        assert "INFORMATION_SCHEMA.RESERVATIONS" in sql
        return QueryResult(
            rows=[(100, "ENTERPRISE")],
            columns=["slot_capacity", "edition"],
            row_count=1,
            wall_clock_ms=1.0,
        )

    monkeypatch.setattr(conn, "execute_query", _fake)
    assert conn.runtime_compute() == {"slot_capacity": 100, "edition": "ENTERPRISE"}


def test_motherduck_runtime_compute_parses_duckling_size(monkeypatch):
    from benchmark.connectors.motherduck import MotherDuckConnector

    conn = MotherDuckConnector(config={})

    def _fake(sql: str) -> QueryResult:
        assert "md_live_duckling_size" in sql
        return QueryResult(
            rows=[("jumbo",)], columns=["duckling_size"], row_count=1, wall_clock_ms=1.0
        )

    monkeypatch.setattr(conn, "execute_query", _fake)
    assert conn.runtime_compute() == {"size": "jumbo"}


def test_databricks_runtime_compute_via_rest(monkeypatch):
    import urllib.request

    from benchmark.connectors.databricks import DatabricksConnector

    conn = DatabricksConnector(
        config={"host": "https://x.databricks.com", "token": "t", "warehouse_id": "wid"}
    )

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"cluster_size": "Medium", "min_num_clusters": 1, "max_num_clusters": 3}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=15: _Resp())
    assert conn.runtime_compute() == {
        "cluster_size": "Medium",
        "min_num_clusters": 1,
        "max_num_clusters": 3,
    }


def test_redshift_runtime_compute_via_boto3(monkeypatch):
    import boto3

    from benchmark.connectors.redshift import RedshiftConnector

    conn = RedshiftConnector(config={"workgroup": "wg", "region": "us-east-1"})

    class _Client:
        def get_workgroup(self, workgroupName: str) -> dict:  # noqa: N803 (boto3 kwarg)
            return {"workgroup": {"baseCapacity": 8, "maxCapacity": 512}}

    monkeypatch.setattr(boto3, "client", lambda service, region_name=None: _Client())
    assert conn.runtime_compute() == {"base_capacity": 8, "max_capacity": 512}


def test_fabric_runtime_compute_via_arm(monkeypatch):
    import urllib.request

    import azure.identity

    from benchmark.connectors import fabric as fabric_mod
    from benchmark.connectors.fabric import FabricConnector

    conn = FabricConnector(config={"component": "dw"})

    class _Secret:
        @staticmethod
        def get_secret_value() -> str:
            return "s"

    class _Fab:
        SUBSCRIPTION_ID = "sub"
        RESOURCE_GROUP = "rg"
        CAPACITY_NAME = "cap"
        TENANT_ID = "t"
        CLIENT_ID = "c"
        CLIENT_SECRET = _Secret

    monkeypatch.setattr(fabric_mod, "get_settings", lambda: type("S", (), {"FABRIC": _Fab}))

    class _Cred:
        def __init__(self, *a: object, **k: object) -> None: ...

        def get_token(self, scope: str) -> object:
            return type("T", (), {"token": "tok"})

    monkeypatch.setattr(azure.identity, "ClientSecretCredential", _Cred)

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"sku": {"name": "F64"}, "properties": {"state": "Active"}}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=15: _Resp())
    assert conn.runtime_compute() == {"capacity_sku": "F64", "state": "Active"}


def test_clickhouse_runtime_compute_via_cloud_api(monkeypatch):
    import urllib.request

    from benchmark.connectors import clickhouse as ch_mod
    from benchmark.connectors.clickhouse import ClickHouseConnector

    conn = ClickHouseConnector(config={})

    class _Secret:
        @staticmethod
        def get_secret_value() -> str:
            return "sec"

    class _Ch:
        ORG_ID = "o"
        SERVICE_ID = "s"
        API_KEY_ID = "k"
        API_KEY_SECRET = _Secret

    monkeypatch.setattr(ch_mod, "get_settings", lambda: type("S", (), {"CLICKHOUSE": _Ch}))

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def read(self) -> bytes:
            return (
                b'{"result": {"numReplicas": 3, "minReplicaMemoryGb": 24, '
                b'"maxReplicaMemoryGb": 48, "tier": "production", "state": "running"}}'
            )

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=15: _Resp())
    assert conn.runtime_compute() == {
        "num_replicas": 3,
        "min_replica_memory_gb": 24,
        "max_replica_memory_gb": 48,
        "tier": "production",
        "state": "running",
    }


# ── the drift check has to survive a size sweep ───────────────────────────────
#
# `engine_environment` keys on compute_label as well as (platform, scale_factor,
# run_type). Keyed per cell alone, a sweep would overwrite one row per cell and only
# the last size would survive; the drift verdict would then compare that arbitrary
# survivor against the designated tier (e.g. `compute_validated: false` with
# actual=Pulse against provisioned=standard, purely because Pulse ran last). A false
# alarm, and a hard failure if a mismatch were ever made fatal.


def test_a_sweep_keeps_one_environment_row_per_size(tmp_path):
    from benchmark.results import ResultsDB

    db = ResultsDB(tmp_path / "r.duckdb")
    for label in ("Pulse", "Standard", "Giga"):
        db.record_engine_environment(
            "motherduck", 1, "warm", {"actual": {"size": label}}, compute_label=label
        )

    with db.connect() as con:
        rows = con.execute(
            "SELECT compute_label FROM engine_environment ORDER BY compute_label"
        ).fetchall()
    assert [r[0] for r in rows] == ["Giga", "Pulse", "Standard"]


def test_re_recording_one_size_updates_only_that_size(tmp_path):
    from benchmark.results import ResultsDB

    db = ResultsDB(tmp_path / "r.duckdb")
    db.record_engine_environment("motherduck", 1, "warm", {"v": 1}, compute_label="Pulse")
    db.record_engine_environment("motherduck", 1, "warm", {"v": 1}, compute_label="Giga")
    db.record_engine_environment("motherduck", 1, "warm", {"v": 2}, compute_label="Pulse")

    with db.connect() as con:
        rows = dict(
            con.execute(
                "SELECT compute_label, json_extract_string(environment, '$.v')"
                " FROM engine_environment"
            ).fetchall()
        )
    assert rows == {"Pulse": "2", "Giga": "1"}


def test_environment_migration_preserves_rows_and_is_idempotent(tmp_path):
    import duckdb

    from benchmark.results import ResultsDB

    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE engine_environment (
            platform VARCHAR NOT NULL, scale_factor INTEGER NOT NULL,
            run_type VARCHAR NOT NULL, environment JSON NOT NULL,
            captured_at TIMESTAMP DEFAULT now(),
            PRIMARY KEY (platform, scale_factor, run_type))
    """)
    con.execute("INSERT INTO engine_environment VALUES ('motherduck', 1, 'warm', '{}', now())")
    con.close()

    db = ResultsDB(path)

    with db.connect() as c:
        cols = {r[0] for r in c.execute("DESCRIBE engine_environment").fetchall()}
        rows = c.execute("SELECT platform, compute_label FROM engine_environment").fetchall()
    assert "compute_label" in cols
    assert rows == [("motherduck", "-")], "pre-existing rows are one unknown survivor"
    ResultsDB(path)
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM engine_environment").fetchone() == (1,)


# ── the pin must be asserted, not assumed ────────────────────────────────────


def test_an_unpinned_clickhouse_service_is_flagged():
    """The Cloud API reports the memory bounds, and they are checked: comparing
    num_replicas alone would pass a service free to resize itself mid-pass, the
    autoscaling failure mode that leaves a cell's rows unattributable to one size."""
    from benchmark.sql_benchmark.runner import _pin_violation

    unpinned = {"min_replica_memory_gb": 32, "max_replica_memory_gb": 120}
    assert "not pinned" in (_pin_violation("clickhouse", unpinned) or "")


def test_a_pinned_clickhouse_service_passes():
    from benchmark.sql_benchmark.runner import _pin_violation

    assert (
        _pin_violation("clickhouse", {"min_replica_memory_gb": 120, "max_replica_memory_gb": 120})
        is None
    )


def test_absent_api_credentials_are_unknowable_not_a_violation():
    """No creds means we cannot tell. Reporting that as a violation would cry wolf on
    every run without Cloud API access."""
    from benchmark.sql_benchmark.runner import _pin_violation

    assert _pin_violation("clickhouse", {"num_replicas": 3}) is None

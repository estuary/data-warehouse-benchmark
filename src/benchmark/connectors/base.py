"""Abstract warehouse connector base class.

CLAUDE.md §Code Standards mandates context-manager support so callers can use
`with connector(...) as conn:`. Concrete implementations live alongside this
file (motherduck.py, snowflake.py, etc.).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..exceptions import ConnectorError

logger = logging.getLogger("benchmark.connectors.base")


def compute_label_from_environment(env: dict[str, Any]) -> str | None:
    """A compact, engine-agnostic compute-size label from a captured
    ``engine_environment`` dict (``runtime_environment`` + ``actual`` +
    ``provisioned``).

    This is the size discriminator stamped on each ``sql_benchmark_results`` row so
    the same scale can be run at multiple compute sizes without the runs colliding
    (the report's efficiency section pivots on it). Best-effort per engine; returns
    ``None`` when no size is discernible (the row is then treated as the default
    size). Kept here so both the runner (stamping rows) and the report (rendering
    the layout) derive the label identically.
    """
    plat = env.get("platform")
    actual = env.get("actual") or {}
    prov = env.get("provisioned") or {}
    if plat == "snowflake":
        size = actual.get("size") or prov.get("warehouse_size")
        if not size:
            return None
        gen = env.get("warehouse_generation")
        return f"{size} (Gen{gen})" if gen else str(size)
    if plat == "bigquery":
        # Reflect what ACTUALLY ran: a live reservation (INFORMATION_SCHEMA.RESERVATIONS
        # → runtime_compute) means slots; empty means on-demand. Do NOT fall back to the
        # provisioned/run-plan slot count — that's intent, and would mislabel an on-demand
        # run as reserved.
        slots = actual.get("slot_capacity")
        edition = actual.get("edition")
        if slots:
            return f"{edition or 'reservation'} / {slots} slots"
        return "on-demand"
    if plat == "motherduck":
        return actual.get("size") or prov.get("tier")
    if plat == "databricks":
        # runtime_compute() emits the REST API's `cluster_size`; captures recorded
        # before that key carry `size`.
        return actual.get("cluster_size") or actual.get("size") or prov.get("warehouse_size")
    if plat == "redshift":
        # runtime_compute() emits base_capacity (boto3 get_workgroup); the
        # terraform-provisioned slice emits baseline_rpu (module output).
        rpu = actual.get("base_capacity") or prov.get("baseline_rpu")
        return f"{rpu} RPU" if rpu else None
    if plat == "fabric":
        # runtime_compute() emits the ARM resource's `capacity_sku`.
        return actual.get("capacity_sku") or actual.get("sku") or prov.get("capacity")
    if plat == "clickhouse":
        # ClickHouse Cloud sizing = replica count × per-replica memory (the Cloud
        # API's num_replicas + min/max_replica_memory_gb; vCPU follows memory at
        # 1 vCPU : 4 GB). Pinned runs (min == max, vertical autoscaling disabled)
        # label as e.g. '3x120GB'; an unpinned range labels honestly as
        # '1x16-120GB' so an autoscaling run is never mistaken for a fixed size.
        replicas = actual.get("num_replicas") or actual.get("replicas")
        mem_max = actual.get("max_replica_memory_gb")
        if replicas and mem_max:
            mem_min = actual.get("min_replica_memory_gb")
            mem = f"{mem_max:g}GB" if mem_min in (None, mem_max) else f"{mem_min:g}-{mem_max:g}GB"
            return f"{int(replicas)}x{mem}"
        return actual.get("size") or prov.get("tier")
    return actual.get("size") or prov.get("tier")


@dataclass
class QueryResult:
    """Result of a single query execution.

    `rows` is the actual result data (used for hashing); `wall_clock_ms` is
    measured by the connector (timing the SDK call). `server_time_ms` is
    pulled from the engine's query history later, where available.
    """

    rows: list[tuple]
    columns: list[str]
    row_count: int
    wall_clock_ms: float
    server_time_ms: float | None = None
    bytes_scanned: int | None = None
    # Best-effort engine profiling (NULL where the engine doesn't expose it):
    bytes_spilled: int | None = None  # bytes spilled to local/remote/disk
    compile_ms: float | None = None  # planning/compilation time
    queue_ms: float | None = None  # time queued/waiting before execution
    cache_hit_pct: float | None = None  # % of scan served from cache (0-100)
    platform_query_id: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class WarehouseConnector(ABC):
    """Abstract base for all engine connectors."""

    def __init__(self, platform: str, config: dict[str, Any] | None = None) -> None:
        self.platform = platform
        self.config = config or {}
        #: Set when a reconnect after a timeout failed: the session is gone, and a
        #: query sent now would fail instantly with "not connected" rather than
        #: measure anything. See ``connection.reconnect_in_place``.
        self.connection_lost = False

    @abstractmethod
    def connect(self) -> None:
        """Establish a connection to the engine."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection."""

    @abstractmethod
    def execute_query(self, sql: str) -> QueryResult:
        """Execute a SQL statement and return a `QueryResult`."""

    @abstractmethod
    def qualify_table(self, schema: str, table: str) -> str:
        """Return the engine-specific fully-qualified table reference.

        e.g. MotherDuck: ``benchmark_db.tpcds_sf1.store_sales``; BigQuery:
        ``` `proj.dataset.store_sales` ```; Redshift: ``public.store_sales``.
        Concrete connectors encapsulate their own qualification rules so the
        rest of the harness can stay engine-agnostic.
        """

    def count_table_rows(self, schema: str, table: str) -> int:
        """Concrete helper used by load-validation. Defaults to a portable
        ``SELECT COUNT(*)`` against ``qualify_table()``; engines free to
        override if a metadata table is cheaper.
        """
        qualified = self.qualify_table(schema, table)
        result = self.execute_query(f"SELECT COUNT(*) FROM {qualified}")
        if result.error_message:
            raise ConnectorError(
                f"count_table_rows failed for {self.platform}.{qualified}: {result.error_message}"
            )
        if not result.rows or not result.rows[0]:
            raise ConnectorError(f"count_table_rows returned no rows for {qualified}")
        return int(result.rows[0][0])

    def disable_cache(self) -> None:  # noqa: B027
        """Disable result caching where the engine supports it.

        Intentionally concrete with a no-op default: most engines either have
        no togglable result cache or honor it via session-level settings the
        connector applies in `connect()`. Engines like Snowflake
        (`USE_CACHED_RESULT`) and BigQuery (`useQueryCache`) override.
        """

    #: Whether ``execute_query`` may read server time from the engine's query
    #: history INLINE, on the measurement session, immediately after each query.
    #:
    #: False where the engine's history table lags completion by minutes: the
    #: lookup then costs real time and returns nothing useful, and — worse — it
    #: injects an extra statement into the measured session, which is a load the
    #: engines that do not do it never see. Those engines get their server time
    #: from ``benchmark reconcile`` post-run instead.
    #:
    #: Measured on Databricks: the inline lookup costs 15.3 s per query
    #: against a 1.4 s query and returns None every time, because
    #: ``system.query.history`` has not yet materialized the row. That is ~90% of
    #: the wall-clock of a Databricks benchmark pass, spent on nothing.
    INLINE_SERVER_TIME: ClassVar[bool] = True

    def get_server_timing(self, platform_query_id: str) -> float | None:
        """Pull server-side execution time for a specific query from history.

        Default returns None; engines with a query-history table override. Callers
        outside ``execute_query`` (reconcile, diagnostics) may always use this —
        ``INLINE_SERVER_TIME`` governs only whether the measurement path does.
        """
        return None

    def cancel_running(self) -> None:
        """Ask the driver to stop the statement ``execute_query`` is running now.

        Called from another thread when the harness's timeout fires, before the
        session is closed, so the abandoned call can return instead of staying blocked
        in the driver. Only for drivers whose cancel is safe to call from another
        thread (pyodbc's ``Cursor.cancel``). Default: nothing.
        """
        return None

    def abandon_session(self) -> None:
        """Drop the current session after a timeout whose query has not returned.

        The abandoned call may still be inside the driver on this session. Python
        drivers raise in that thread when the session is closed under it, so the
        default closes it (:meth:`disconnect`). A C driver can instead read freed
        memory and take the process down (pyodbc segfaults); such connectors
        override this to keep the abandoned handles alive and unclosed while a new
        session is opened.
        """
        self.disconnect()

    def inflight_query_id(self) -> str | None:
        """The engine's id for the statement ``execute_query`` is running now.

        Called from another thread when the harness's timeout fires, before the
        session is closed, so the timed-out row carries an id ``reconcile`` can
        look up. None when the connector cannot tell, or when the engine had not
        yet acknowledged the statement (it never reached the engine, or the
        client was still retrying the submit). Default: None.
        """
        return None

    def runtime_environment(self) -> dict[str, Any]:
        """Engine compute/runtime configuration in effect for this connection.

        Captured once per (platform, scale_factor, run_type) so results — the
        ClickBench engine-sizing curve in particular, where one 100 GB dataset
        runs across each engine's per-scale compute — record which compute
        produced them. The base returns the portable fields; connectors override
        to add their compute identity (warehouse size, RPUs, region, capacity,
        ...). Call after ``connect()`` so resolved attributes are populated.
        """
        env: dict[str, Any] = {"platform": self.platform}
        for key in ("scale_factor", "schema", "suite"):
            value = self.config.get(key)
            if value is not None:
                env[key] = value
        return env

    def _query_scalar(self, sql: str) -> Any | None:
        """Best-effort single-value query for runtime-environment enrichment
        (engine version, etc.). Resilient: returns None on any error — a missing
        version string must never break instrumentation or the run.
        """
        try:
            result = self.execute_query(sql)
        except Exception:
            logger.debug("runtime-env scalar query raised: %s", sql, exc_info=True)
            return None
        if result.error_message or not result.rows or not result.rows[0]:
            return None
        return result.rows[0][0]

    def runtime_compute(self) -> dict[str, Any]:
        """Query the engine for the compute size/cluster ACTUALLY in effect, as a
        runtime cross-check against what terraform provisioned for the scale.

        Default: ``{}`` (no SQL-queryable actual size). Engines that expose it —
        e.g. Snowflake via ``SHOW WAREHOUSES`` — override. Must be resilient and
        never raise; the caller treats ``{}`` as "not validatable".
        """
        return {}

    @staticmethod
    def cost_kwargs_from_compute(compute: dict[str, Any]) -> dict[str, Any] | None:
        """Translate ``runtime_compute()`` output into cost-collector kwargs.

        This is a PURE function — no engine round-trip. It consumes the exact
        dict that the drift check already read from the live engine via
        :meth:`runtime_compute`, so the priced compute can never diverge from the
        ran compute. ``benchmark capture-cost-config`` calls
        ``runtime_compute()`` once, then feeds the result here.

        Returns cost-collector constructor kwargs (e.g. ``{"tier": "standard"}``
        for MotherDuck, ``{"vcpus": 4, "replicas": 3}`` for ClickHouse), or
        ``None`` if this engine reports no cost driver (or ``compute`` was empty
        because the runtime query failed) — in which case the platform is
        recorded under ``errors`` in the cost-config file rather than aborting
        the capture.

        Default is None so an engine without an override simply doesn't
        contribute; engines that have a cost driver override this.
        """
        return None

    def __enter__(self) -> WarehouseConnector:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()

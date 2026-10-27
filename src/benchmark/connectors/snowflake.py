"""Snowflake connector — `snowflake-connector-python`.

Cost-telemetry tier: ESTIMATED, list rate x engine time (see
``benchmark.cost.snowflake``). The connector captures ``sfqid`` on each
execution so ``reconcile`` can look the query up in
``SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY`` after the 6-8 hour
attribution latency and keep its credits as a cross-check. Result-cache is disabled at session level.

Auth: JWT key-pair against BENCHMARK_USER (TYPE = SERVICE). The harness
points at the PKCS#8 PEM via ``SNOWFLAKE_BENCHMARK_PRIVATE_KEY_FILE``;
authenticator is ``SNOWFLAKE_JWT``. Service users on Snowflake can't use
password auth.

Schema convention: harness passes ``"tpcds_sf1"`` etc. as the ``schema``
config key; the connector uppercases it and applies ``USE SCHEMA
BENCHMARK_DB.TPCDS_SF1`` per the three-block layout (TPCDS_SF{N},
DSB_SF{N}, CLICKBENCH).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import snowflake.connector

from ..exceptions import BenchmarkError, ConnectorError
from ..run_plan import get_run_plan
from ..settings import get_settings
from .base import QueryResult, WarehouseConnector

logger = logging.getLogger("benchmark.connectors.snowflake")

# Ceiling for ALL Snowflake network operations, query execution included. Set above
# the harness's own longest query timeout (``SF1000_QUERY_TIMEOUT_S`` = 1800 s) with
# margin, so the client can never truncate a measurement the harness intended to
# allow. The harness owns query timeouts via ``resolve_query_timeout_s``; this is
# only a backstop against a genuinely wedged socket.
_NETWORK_TIMEOUT_S = 2100


class SnowflakeConnector(WarehouseConnector):
    """Snowflake connector with cost telemetry hooks (ESTIMATED tier; attribution cross-check).

    Config keys:
        account, user: fall back to SNOWFLAKE_ACCOUNT / SNOWFLAKE_BENCHMARK_USER.
        private_key_file: path to the PKCS#8 PEM file for BENCHMARK_USER.
            Falls back to SNOWFLAKE_BENCHMARK_PRIVATE_KEY_FILE. The connector
            authenticates via JWT (snowflake_jwt) using this key — service
            users in Snowflake (TYPE = SERVICE) require key-pair or OAuth.
        scale_factor: optional int (1 / 10 / 100 / 1000); the
            connector picks the right warehouse from the run-plan
            (``run_plan.yaml``) when no explicit ``warehouse`` is passed.
        warehouse: explicit override; takes precedence over the scale
            mapping. Default resolution order: ``config["warehouse"]`` →
            run-plan ``snowflake.warehouse[scale]`` (size tier →
            ``BENCHMARK_WH_<SIZE>``) → run-plan SF1 warehouse (ad-hoc/no-scale)
            → ``DEFAULT_WAREHOUSE`` if the run-plan is unavailable.
        role: default ``BENCHMARK_ROLE``
        database: default ``BENCHMARK_DB``
        schema: e.g. ``"tpcds_sf1"``; uppercased and applied via ``USE SCHEMA``
        region: optional; defaults to SNOWFLAKE_REGION / us-east-1
        disable_cache: default True; sets ``USE_CACHED_RESULT = FALSE``
    """

    # Last-resort warehouse when the run-plan can't be loaded at all (e.g. the
    # file is missing). The normal SF1 fallback is run-plan-derived; this only
    # guards a totally-unavailable plan.
    DEFAULT_WAREHOUSE = "BENCHMARK_WH_XSMALL"

    @staticmethod
    def _warehouse_for_scale(scale: Any) -> str | None:
        """Resolve the per-scale warehouse name from the run-plan.

        The run-plan gives scale → size tier (e.g. ``1000 → LARGE``); terraform
        names each pre-created warehouse ``BENCHMARK_WH_<SIZE>`` (snowflake module
        ``warehouse_sizes``), so the name is that convention applied to the tier.
        Returns ``None`` when ``scale`` isn't an int, has no run-plan entry, or
        the run-plan is unavailable — caller falls back to the settings default.
        """
        if not isinstance(scale, int):
            return None
        try:
            size = get_run_plan().target("snowflake", scale)
        except BenchmarkError:
            logger.debug("run-plan unavailable for warehouse resolution", exc_info=True)
            return None
        if size is None:
            return None
        return f"BENCHMARK_WH_{str(size).upper()}"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__("snowflake", config or {})
        self.conn: snowflake.connector.SnowflakeConnection | None = None
        self.cursor: Any = None
        self._last_sfqid: str | None = None
        # Resolved warehouse name (set in connect()); used by the cold pass to
        # suspend the warehouse actually in use rather than a hardcoded one.
        self.warehouse: str | None = None

    def connect(self) -> None:
        sf = get_settings().SNOWFLAKE
        account = self.config.get("account") or sf.ACCOUNT
        user = self.config.get("user") or sf.BENCHMARK_USER
        # JWT key-pair auth: snowflake_service_user (TYPE = SERVICE) doesn't
        # support password auth; the private key file at this path must
        # match the RSA public key terraform set on the Snowflake user.
        private_key_file = self.config.get("private_key_file") or (
            str(sf.BENCHMARK_PRIVATE_KEY_FILE) if sf.BENCHMARK_PRIVATE_KEY_FILE else None
        )
        if not (account and user and private_key_file):
            raise ConnectorError(
                "SNOWFLAKE_ACCOUNT / SNOWFLAKE_BENCHMARK_USER / "
                "SNOWFLAKE_BENCHMARK_PRIVATE_KEY_FILE missing"
            )

        # Warehouse resolution order:
        #   1. explicit config["warehouse"] (caller override)
        #   2. run-plan snowflake.warehouse[scale_factor] (auto-ramped per scale)
        #   3. run-plan SF1 warehouse (ad-hoc calls with no/unknown scale)
        #   4. DEFAULT_WAREHOUSE last resort if the run-plan is unavailable
        warehouse = (
            self.config.get("warehouse")
            or self._warehouse_for_scale(self.config.get("scale_factor"))
            or self._warehouse_for_scale(1)
            or self.DEFAULT_WAREHOUSE
        )
        role = self.config.get("role", sf.ROLE)
        database = self.config.get("database", sf.DATABASE)
        region = self.config.get("region") or sf.REGION

        connect_kwargs: dict[str, Any] = {
            "account": account,
            "user": user,
            "private_key_file": private_key_file,
            "authenticator": "SNOWFLAKE_JWT",
            "warehouse": warehouse,
            "database": database,
            "role": role,
            "application": "benchmark-2026",
            # Bound connection establishment so a failed/slow login surfaces as
            # an error instead of hanging the caller. Critical under the
            # concurrency pass, where many connections open at once and the
            # SDK's default retry/backoff could otherwise block for minutes.
            "login_timeout": self.config.get("login_timeout", 60),
            # `network_timeout` bounds EVERY network operation, not just login,
            # including query execution: a 90s value would silently cap Snowflake
            # queries at ~90s while every other engine runs to the harness's 600s
            # policy. Login bounding belongs to `login_timeout`; this must never be
            # tighter than the harness's own query timeout or it truncates the
            # measurement instead of protecting it.
            "network_timeout": self.config.get("network_timeout", _NETWORK_TIMEOUT_S),
        }
        # Only append a region for legacy account locators. Modern org-account
        # identifiers (ORGNAME-ACCOUNTNAME, contain a hyphen) and fully-qualified
        # hosts (contain a dot) already encode the region; passing region= makes
        # the connector build <account>.<region>.snowflakecomputing.com, a host
        # that does not exist and 404s on the login request.
        if region and "-" not in account and "." not in account:
            connect_kwargs["region"] = region

        try:
            self.conn = snowflake.connector.connect(**connect_kwargs)
            self.cursor = self.conn.cursor()
            self.warehouse = warehouse
        except Exception as e:
            logger.error("Snowflake connect failed", exc_info=True)
            raise ConnectorError(f"Snowflake connection failed: {e}") from e

        if self.config.get("disable_cache", True):
            try:
                self.cursor.execute("ALTER SESSION SET USE_CACHED_RESULT = FALSE")
            except Exception:
                logger.warning("failed to disable Snowflake result cache", exc_info=True)

        schema = self.config.get("schema")
        if schema:
            self.cursor.execute(f"USE SCHEMA {database}.{schema.upper()}")

        logger.info(
            "connected to Snowflake",
            extra={"platform": "snowflake", "warehouse": warehouse, "role": role},
        )

    def disconnect(self) -> None:
        if self.cursor is not None:
            try:
                self.cursor.close()
            except Exception:
                logger.debug("Snowflake cursor close failed", exc_info=True)
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None
                self.cursor = None

    def qualify_table(self, schema: str, table: str) -> str:
        """``BENCHMARK_DB.SF1.STORE_SALES``-style three-part name."""
        database = self.config.get("database", get_settings().SNOWFLAKE.DATABASE)
        return f"{database}.{schema.upper()}.{table.upper()}"

    def runtime_environment(self) -> dict[str, Any]:
        env = super().runtime_environment()
        env.update(
            {
                "warehouse": self.warehouse,  # encodes the size tier (…_MEDIUM, …_LARGE)
                "warehouse_generation": "2",  # Gen2 Standard (terraform generation="2")
                "database": self.config.get("database", get_settings().SNOWFLAKE.DATABASE),
            }
        )
        version = self._query_scalar("SELECT CURRENT_VERSION()")
        if version:
            env["version"] = str(version)
        return env

    def runtime_compute(self) -> dict[str, Any]:
        """Actual warehouse size + cluster bounds via ``SHOW WAREHOUSES`` — used
        to validate the running compute matches the per-scale size terraform
        provisioned. Resilient: ``{}`` on any error.
        """
        warehouse = self.warehouse
        if not warehouse:
            return {}
        try:
            result = self.execute_query(f"SHOW WAREHOUSES LIKE '{warehouse}'")
        except Exception:
            logger.debug("SHOW WAREHOUSES failed", exc_info=True)
            return {}
        if result.error_message or not result.rows:
            return {}
        cols = {c.lower(): i for i, c in enumerate(result.columns)}
        row = result.rows[0]
        out: dict[str, Any] = {}
        for key in ("size", "min_cluster_count", "max_cluster_count", "type"):
            idx = cols.get(key)
            if idx is not None and idx < len(row) and row[idx] is not None:
                out[key] = row[idx]
        return out

    def execute_query(self, sql: str) -> QueryResult:
        if self.cursor is None:
            raise ConnectorError("Snowflake connector not connected")

        start = time.perf_counter()
        try:
            self.cursor.execute(sql)
            rows = self.cursor.fetchall()
            wall_clock_ms = (time.perf_counter() - start) * 1000
            columns = [d[0] for d in (self.cursor.description or [])]
            sfqid = getattr(self.cursor, "sfqid", None)
            self._last_sfqid = sfqid
            # Capture server-side execution time + inline profiling — the primary
            # metric is server time (golden rule #8). wall_clock_ms is already
            # measured above, so the extra history lookup doesn't inflate it.
            # Inline gives server time + bytes + compile + queue; spill / cache% /
            # partitions are ACCOUNT_USAGE-only and not captured.
            profile = self.get_server_profile(sfqid) if sfqid else {}
            metadata: dict[str, Any] = {
                "sfqid": sfqid,
                "warehouse": self.warehouse,
                "database": self.config.get("database", get_settings().SNOWFLAKE.DATABASE),
            }

            return QueryResult(
                rows=rows,
                columns=columns,
                row_count=len(rows),
                wall_clock_ms=wall_clock_ms,
                server_time_ms=profile.get("server_time_ms"),
                bytes_scanned=profile.get("bytes_scanned"),
                compile_ms=profile.get("compile_ms"),
                queue_ms=profile.get("queue_ms"),
                platform_query_id=sfqid,
                metadata=metadata,
            )
        except Exception as e:
            wall_clock_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "Snowflake query failed",
                extra={"platform": "snowflake", "wall_clock_ms": wall_clock_ms},
                exc_info=True,
            )
            return QueryResult(
                rows=[],
                columns=[],
                row_count=0,
                wall_clock_ms=wall_clock_ms,
                error_message=str(e),
                metadata={"error_type": type(e).__name__},
            )

    def get_server_timing(
        self, platform_query_id: str, *, retries: int = 3, wait_s: float = 0.4
    ) -> float | None:
        """Server-side execution time (ms). Thin wrapper over get_server_profile
        for the base-class contract; ``total_elapsed_time`` is already in ms."""
        return self.get_server_profile(platform_query_id, retries=retries, wait_s=wait_s).get(
            "server_time_ms"
        )

    def get_server_profile(
        self, platform_query_id: str, *, retries: int = 3, wait_s: float = 0.4
    ) -> dict[str, Any]:
        """Pull the inline server-side profile from query_history_by_session().

        One lookup returns the signals the INFORMATION_SCHEMA session-history
        function actually exposes: server time (``total_elapsed_time``), bytes
        scanned, compile time, and queue time (overload + provisioning). Retried
        briefly — the just-executed query can take a beat to land in the session
        history view. All ``*_TIME`` columns are already in milliseconds. Returns
        ``{}`` on miss.

        NOTE: spill, ``percentage_scanned_from_cache`` and partition pruning are
        NOT in this table function; they live only in ``ACCOUNT_USAGE.QUERY_HISTORY``
        (higher latency), so they are not captured for Snowflake.
        """
        if self.cursor is None or not platform_query_id:
            return {}
        for attempt in range(retries):
            try:
                row = self.cursor.execute(
                    "SELECT total_elapsed_time, bytes_scanned, compilation_time,"
                    " queued_overload_time, queued_provisioning_time"
                    " FROM TABLE(information_schema.query_history_by_session())"
                    " WHERE query_id = %s LIMIT 1",
                    (platform_query_id,),
                ).fetchone()
                if row and row[0] is not None:
                    queue = (row[3] or 0) + (row[4] or 0)
                    return {
                        "server_time_ms": float(row[0]),
                        "bytes_scanned": int(row[1]) if row[1] is not None else None,
                        "compile_ms": float(row[2]) if row[2] is not None else None,
                        "queue_ms": float(queue),
                    }
            except Exception:
                logger.debug("get_server_profile attempt %d failed", attempt, exc_info=True)
            if attempt < retries - 1:
                time.sleep(wait_s)
        return {}

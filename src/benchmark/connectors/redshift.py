"""Amazon Redshift Serverless connector — `redshift-connector`.

Cost-telemetry tier: PROPORTIONAL (RPU-seconds via ``SYS_SERVERLESS_USAGE``;
7-day retention). The connector captures the Redshift ``query_id`` for the
cost collector.

Schema convention: a single Redshift database (``REDSHIFT_DATABASE``, default
``benchmark_db``) holds one schema per suite and scale (``tpcds_sf1``,
``dsb_sf1``, ... ``tpcds_sf1000``, ``dsb_sf1000``) plus ``clickbench``. The
connector sets ``search_path`` to the run's schema after connect and qualifies
tables as ``<schema>.<table>``, because Redshift queries one database per
session and schemas are namespaced beneath it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import redshift_connector

from ..exceptions import ConnectorError
from ..settings import get_settings
from .base import QueryResult, WarehouseConnector

logger = logging.getLogger("benchmark.connectors.redshift")


class RedshiftConnector(WarehouseConnector):
    """Redshift Serverless connector.

    Config keys:
        workgroup: Serverless workgroup (REDSHIFT_WORKGROUP)
        database: default ``benchmark_db`` (REDSHIFT_DATABASE)
        region: AWS region (AWS_REGION)
        host: optional explicit endpoint (overrides workgroup lookup)
        user, password: optional; default to IAM if absent
        schema: e.g. ``"tpcds_sf1"``; ``SET search_path`` after connect
        disable_cache: default True; sets ``enable_result_cache_for_session=off``
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__("redshift", config or {})
        self.conn: Any = None
        self.cursor: Any = None
        self._last_query_id: int | None = None

    def connect(self) -> None:
        settings = get_settings()
        rs = settings.REDSHIFT
        workgroup = self.config.get("workgroup") or rs.WORKGROUP
        database = self.config.get("database") or rs.DATABASE
        region = self.config.get("region") or settings.AWS.REGION
        host = self.config.get("host") or rs.ENDPOINT
        user = self.config.get("user") or rs.USER
        password = self.config.get("password") or (
            rs.PASSWORD.get_secret_value() if rs.PASSWORD else None
        )

        if not workgroup and not host:
            raise ConnectorError("REDSHIFT_WORKGROUP or explicit host config missing")

        try:
            connect_kwargs: dict[str, Any] = {
                "database": database,
                "region": region,
                # Aggressive TCP keepalive: the concurrency pass pre-warms up to
                # 100 sessions that sit idle at a barrier for minutes; anything
                # on the path (VPN, NAT, LB) that reaps idle TCP kills them,
                # surfacing as BrokenPipe at fire time.
                # macOS's default first probe is 2h; probe after 60s idle.
                "tcp_keepalive": True,
                "tcp_keepalive_idle": 60,
                "tcp_keepalive_interval": 30,
                "tcp_keepalive_count": 4,
            }
            if host:
                # REDSHIFT_ENDPOINT from terraform output is a flat 'host:port'
                # string. redshift_connector wants host and port separately —
                # passing host:port as `host` causes a getaddrinfo failure.
                if ":" in host:
                    host_only, port_str = host.rsplit(":", 1)
                    connect_kwargs["host"] = host_only
                    try:
                        connect_kwargs["port"] = int(port_str)
                    except ValueError:
                        connect_kwargs["host"] = host  # let driver surface the error
                else:
                    connect_kwargs["host"] = host
            else:
                # IAM serverless connect by workgroup name (uses boto3 chain).
                connect_kwargs.update(
                    {
                        "iam": True,
                        "is_serverless": True,
                        "serverless_work_group": workgroup,
                    }
                )
            if user:
                connect_kwargs["user"] = user
            if password:
                connect_kwargs["password"] = password

            self.conn = redshift_connector.connect(**connect_kwargs)
            self.conn.autocommit = True
            self.cursor = self.conn.cursor()
        except Exception as e:
            logger.error("Redshift connect failed", exc_info=True)
            raise ConnectorError(f"Redshift connection failed: {e}") from e

        if self.config.get("disable_cache", True):
            try:
                self.cursor.execute("SET enable_result_cache_for_session = off")
            except Exception:
                logger.warning("failed to disable Redshift result cache", exc_info=True)

        schema = self.config.get("schema")
        if schema:
            self.cursor.execute(f"SET search_path TO {schema}, public")

        logger.info(
            "connected to Redshift Serverless",
            extra={
                "platform": "redshift",
                "workgroup": workgroup,
                "database": database,
                "schema": schema,
            },
        )

    def disconnect(self) -> None:
        if self.cursor is not None:
            try:
                self.cursor.close()
            except Exception:
                logger.debug("Redshift cursor close failed", exc_info=True)
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None
                self.cursor = None

    def qualify_table(self, schema: str, table: str) -> str:
        return f"{schema}.{table}"

    def runtime_environment(self) -> dict[str, Any]:
        env = super().runtime_environment()
        settings = get_settings()
        env.update(
            {
                "workgroup": self.config.get("workgroup") or settings.REDSHIFT.WORKGROUP,
                "database": self.config.get("database") or settings.REDSHIFT.DATABASE,
                "region": self.config.get("region") or settings.AWS.REGION,
            }
        )
        version = self._query_scalar("SELECT version()")
        if version:
            env["version"] = str(version)
        return env

    def runtime_compute(self) -> dict[str, Any]:
        """Actual serverless base/max RPU via boto3 (redshift-serverless
        get_workgroup) — base capacity isn't SQL-exposed — to validate the
        running workgroup matches the provisioned RPU. Resilient: {} on error.
        """
        import boto3

        settings = get_settings()
        workgroup = self.config.get("workgroup") or settings.REDSHIFT.WORKGROUP
        region = self.config.get("region") or settings.AWS.REGION
        if not workgroup:
            return {}
        try:
            client = boto3.client("redshift-serverless", region_name=region)
            wg = client.get_workgroup(workgroupName=workgroup)["workgroup"]
        except Exception:
            logger.debug("Redshift get_workgroup failed", exc_info=True)
            return {}
        out: dict[str, Any] = {}
        if wg.get("baseCapacity") is not None:
            out["base_capacity"] = wg["baseCapacity"]
        if wg.get("maxCapacity") is not None:
            out["max_capacity"] = wg["maxCapacity"]
        return out

    @staticmethod
    def cost_kwargs_from_compute(compute: dict[str, Any]) -> dict[str, Any] | None:
        """Map live sizing → ``RedshiftCostCollector(baseline_rpu=)``.

        The inline estimate prices ``baseline_rpu`` (the workgroup's base
        capacity); ``benchmark reconcile`` later replaces it with actual
        RPU-seconds from ``SYS_SERVERLESS_USAGE``. Returns None if the boto3
        lookup didn't surface ``base_capacity``.
        """
        base = compute.get("base_capacity")
        if not base:
            return None
        return {"baseline_rpu": int(base)}

    def execute_query(self, sql: str) -> QueryResult:
        if self.cursor is None:
            raise ConnectorError("Redshift connector not connected")

        start = time.perf_counter()
        try:
            self.cursor.execute(sql)
            rows = self.cursor.fetchall() if self.cursor.description is not None else []
            wall_clock_ms = (time.perf_counter() - start) * 1000
            columns = [d[0] for d in (self.cursor.description or [])]

            # Redshift exposes the current query id via pg_last_query_id().
            qid: int | None = None
            try:
                qid_row = self.cursor.execute("SELECT pg_last_query_id()").fetchone()
                qid = int(qid_row[0]) if qid_row and qid_row[0] is not None else None
                self._last_query_id = qid
            except Exception:
                logger.debug("pg_last_query_id() failed", exc_info=True)

            # Capture server-side execution time inline (primary metric). SYS_QUERY_HISTORY
            # is populated shortly after the query finishes, so get_server_timing retries.
            server_time_ms = self.get_server_timing(str(qid)) if qid is not None else None

            return QueryResult(
                rows=[tuple(r) for r in rows],
                columns=columns,
                row_count=len(rows),
                wall_clock_ms=wall_clock_ms,
                server_time_ms=server_time_ms,
                platform_query_id=str(qid) if qid is not None else None,
                metadata={"query_id": qid, "database": self.config.get("database")},
            )
        except Exception as e:
            wall_clock_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "Redshift query failed",
                extra={"platform": "redshift", "wall_clock_ms": wall_clock_ms},
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
        self, platform_query_id: str, *, retries: int = 4, wait_s: float = 0.5
    ) -> float | None:
        """Server-side execution time (ms) from ``SYS_QUERY_HISTORY``.

        ``execution_time`` is in microseconds, so we divide by 1000. The row
        lands shortly after the query completes, so we retry briefly.
        """
        if self.cursor is None or not platform_query_id:
            return None
        for attempt in range(retries):
            try:
                row = self.cursor.execute(
                    "SELECT execution_time FROM SYS_QUERY_HISTORY WHERE query_id = %s",
                    (int(platform_query_id),),
                ).fetchone()
                if row and row[0] is not None:
                    return float(row[0]) / 1000.0
            except Exception:
                logger.debug("Redshift server timing lookup failed", exc_info=True)
            if attempt < retries - 1:
                time.sleep(wait_s)
        return None

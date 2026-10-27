"""Databricks SQL connector — `databricks-sql-connector`.

Cost-telemetry tier: PROPORTIONAL (DBU-based; allocated from
``system.billing.usage`` post-run). The connector captures the platform
query ID (statement ID) so the cost collector can correlate.

Schema convention: harness passes ``"tpcds_sf1"`` / ``"dsb_sf1"`` /
``"clickbench"``; the connector targets ``<catalog>.<schema>.<table>``
(catalog from ``DATABRICKS_CATALOG`` env / config).
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar

from databricks import sql as dbsql

from ..exceptions import ConnectorError
from ..settings import get_settings
from .base import QueryResult, WarehouseConnector

logger = logging.getLogger("benchmark.connectors.databricks")


class DatabricksConnector(WarehouseConnector):
    """Databricks SQL connector (serverless SQL warehouse).

    Config keys:
        host: server hostname (DATABRICKS_HOST, no scheme)
        http_path: SQL warehouse HTTP path (DATABRICKS_SQL_HTTP_PATH)
        token: PAT (DATABRICKS_TOKEN)
        catalog: default ``benchmark_catalog`` (DATABRICKS_CATALOG)
        schema: e.g. ``"tpcds_sf1"``
        disable_cache: default True; sets ``USE_CACHED_RESULT = FALSE``
    """

    #: `system.query.history` lags query completion by minutes, so reading it on the
    #: measurement session is both futile and expensive — a measured 15.3 s per query
    #: for a None. Server time is back-filled by `benchmark reconcile`.
    INLINE_SERVER_TIME: ClassVar[bool] = False

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__("databricks", config or {})
        self.conn: Any = None
        self.cursor: Any = None
        self._last_statement_id: str | None = None
        # The cursor's id before the current execute: the connector keeps the
        # previous statement's id until the server acknowledges the next one.
        self._id_before_execute: str | None = None

    @staticmethod
    def _warehouse_id_from_http_path(path: str) -> str:
        """Warehouse id = trailing segment of ``/sql/1.0/warehouses/<id>``."""
        return path.rstrip("/").split("/")[-1]

    def connect(self) -> None:
        db = get_settings().DATABRICKS
        host = self.config.get("host") or db.HOST
        path = self.config.get("http_path") or db.SQL_HTTP_PATH
        token = self.config.get("token") or (db.TOKEN.get_secret_value() if db.TOKEN else None)
        if not (host and path and token):
            raise ConnectorError(
                "DATABRICKS_HOST / DATABRICKS_SQL_HTTP_PATH / DATABRICKS_TOKEN missing"
            )

        # Capture the warehouse id (trailing segment of the HTTP path) for the
        # environment snapshot and the warehouse REST lookups. Reconcile does not
        # use it: it reads each statement's warehouse from system.query.history.
        if path and not self.config.get("warehouse_id"):
            self.config["warehouse_id"] = self._warehouse_id_from_http_path(path)

        catalog = self.config.get("catalog") or db.CATALOG
        schema = self.config.get("schema")

        try:
            self.conn = dbsql.connect(
                server_hostname=host.replace("https://", "").rstrip("/"),
                http_path=path,
                access_token=token,
                catalog=catalog,
                schema=schema,
            )
            self.cursor = self.conn.cursor()
        except Exception as e:
            logger.error("Databricks connect failed", exc_info=True)
            raise ConnectorError(f"Databricks connection failed: {e}") from e

        if self.config.get("disable_cache", True):
            try:
                self.cursor.execute("SET use_cached_result = false")
            except Exception:
                logger.warning("failed to disable Databricks result cache", exc_info=True)

        logger.info(
            "connected to Databricks SQL",
            extra={"platform": "databricks", "catalog": catalog, "schema": schema},
        )

    def disconnect(self) -> None:
        if self.cursor is not None:
            try:
                self.cursor.close()
            except Exception:
                logger.debug("Databricks cursor close failed", exc_info=True)
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None
                self.cursor = None

    def inflight_query_id(self) -> str | None:
        """Statement id of the execute in progress, once the warehouse acknowledged it.

        databricks-sql-connector sets ``cursor.active_command_id`` from the
        ExecuteStatement response and only then polls until the statement
        finishes, so a new id means the warehouse has the statement (running or
        queued). The same id as before the execute means it has not answered the
        submit yet: the connector retries a throttled submit (HTTP 429/503) for up
        to 900 s by default.
        """
        cursor = self.cursor
        qid = getattr(cursor, "query_id", None) if cursor is not None else None
        if qid is None or qid == self._id_before_execute:
            return None
        return str(qid)

    def qualify_table(self, schema: str, table: str) -> str:
        catalog = self.config.get("catalog", get_settings().DATABRICKS.CATALOG)
        return f"{catalog}.{schema}.{table}"

    def runtime_environment(self) -> dict[str, Any]:
        env = super().runtime_environment()
        db = get_settings().DATABRICKS
        env["catalog"] = self.config.get("catalog", db.CATALOG)
        http_path = self.config.get("http_path") or db.SQL_HTTP_PATH
        if http_path:
            env["http_path"] = http_path  # identifies the SQL warehouse
        warehouse_id = self.config.get("warehouse_id")  # derived from http_path in connect()
        if warehouse_id:
            env["warehouse_id"] = warehouse_id
        return env

    def runtime_compute(self) -> dict[str, Any]:
        """Actual SQL-warehouse cluster size via the Databricks REST API
        (GET /api/2.0/sql/warehouses/{id}) — the size isn't SQL-exposed — to
        validate the running warehouse matches the provisioned size. Resilient:
        {} on any error.
        """
        import json
        import urllib.request

        db = get_settings().DATABRICKS
        host = self.config.get("host") or db.HOST
        token = self.config.get("token") or (db.TOKEN.get_secret_value() if db.TOKEN else None)
        warehouse_id = self.config.get("warehouse_id")
        if not (host and token and warehouse_id):
            return {}
        if not host.startswith("http"):
            host = f"https://{host}"
        url = f"{host.rstrip('/')}/api/2.0/sql/warehouses/{warehouse_id}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception:
            logger.debug("Databricks warehouse REST lookup failed", exc_info=True)
            return {}
        out: dict[str, Any] = {}
        for key in ("cluster_size", "min_num_clusters", "max_num_clusters"):
            if data.get(key) is not None:
                out[key] = data[key]
        return out

    def execute_query(self, sql: str) -> QueryResult:
        if self.cursor is None:
            raise ConnectorError("Databricks connector not connected")

        self._id_before_execute = getattr(self.cursor, "query_id", None)
        start = time.perf_counter()
        try:
            self.cursor.execute(sql)
            rows = self.cursor.fetchall()
            wall_clock_ms = (time.perf_counter() - start) * 1000
            columns = [d[0] for d in (self.cursor.description or [])]
            # Prefer the statement GUID (cursor.query_id on recent connectors) —
            # that's what system.query.history keys on; fall back to the result
            # set's command_id on older versions.
            stmt_id = getattr(self.cursor, "query_id", None)
            if stmt_id is None:
                ars = getattr(self.cursor, "active_result_set", None)
                stmt_id = getattr(ars, "command_id", None) if ars is not None else None
            self._last_statement_id = str(stmt_id) if stmt_id is not None else None

            # NOT read inline — see INLINE_SERVER_TIME. `system.query.history` does
            # not have the row yet, so reading it here costs ~15 s per query and returns
            # None every time (measured against a 1.4 s query). Server time
            # comes from `benchmark reconcile`.
            server_time_ms = None

            return QueryResult(
                rows=[tuple(r) for r in rows],
                columns=columns,
                row_count=len(rows),
                wall_clock_ms=wall_clock_ms,
                server_time_ms=server_time_ms,
                platform_query_id=self._last_statement_id,
                metadata={
                    "statement_id": self._last_statement_id,
                    "catalog": self.config.get("catalog"),
                },
            )
        except Exception as e:
            wall_clock_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "Databricks query failed",
                extra={"platform": "databricks", "wall_clock_ms": wall_clock_ms},
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
        self, platform_query_id: str, *, retries: int = 3, wait_s: float = 0.5
    ) -> float | None:
        """Server-side execution time (ms) from ``system.query.history``.

        Keyed by ``statement_id``; ``execution_duration_ms`` is the execution
        portion (excludes compilation + result fetch). BEST-EFFORT: the
        ``system.query.history`` table can lag query completion by minutes, so
        the brief inline retry will often miss it on a live run — in that case
        this returns None and the value should be back-filled post-run (same
        pattern as the cost collector).
        """
        if self.cursor is None or not platform_query_id:
            return None
        # statement ids are GUIDs; safe to inline as a quoted literal.
        sid = platform_query_id.replace("'", "")
        for attempt in range(retries):
            try:
                self.cursor.execute(
                    "SELECT execution_duration_ms FROM system.query.history "
                    f"WHERE statement_id = '{sid}' ORDER BY start_time DESC LIMIT 1"
                )
                row = self.cursor.fetchone()
                if row and row[0] is not None:
                    return float(row[0])
            except Exception:
                logger.debug("Databricks server timing lookup failed", exc_info=True)
            if attempt < retries - 1:
                time.sleep(wait_s)
        return None

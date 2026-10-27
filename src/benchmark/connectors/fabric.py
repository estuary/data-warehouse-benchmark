"""Microsoft Fabric connector — T-SQL over `pyodbc` + ODBC Driver 18.

Runs the SQL benchmark on Fabric's Data Warehouse (DW) component
(columnar/lakehouse over OneLake). A single ``fabric`` platform ID is used in
the harness and the results DB; ``component`` (always ``dw``) is propagated
into ``QueryResult.metadata`` for provenance.

Cost-telemetry tier: PROPORTIONAL (Capacity Unit-time from Azure Cost Mgmt;
per-query attribution via ``queryinsights.exec_requests_history``).
"""

from __future__ import annotations

import logging
import struct
import time
import uuid
from typing import Any, ClassVar

import pyodbc
import sqlglot
from azure.identity import DefaultAzureCredential
from sqlglot import exp

from ..exceptions import ConnectorError
from ..settings import get_settings
from .base import QueryResult, WarehouseConnector

logger = logging.getLogger("benchmark.connectors.fabric")


# Token attribute id for SQL-side authentication via ODBC; documented under
# msodbcsql "SQL_COPT_SS_ACCESS_TOKEN".
_SQL_COPT_SS_ACCESS_TOKEN = 1256
_AAD_SCOPE_DW = "https://database.windows.net/.default"


#: Statements that accept a trailing ``OPTION (LABEL = ...)``. The hint must be the
#: final clause of a query, so it is appended only to reads — a SET or DDL statement
#: with an OPTION clause is a syntax error, and the connector issues a few of those
#: (session setup, warehouse metadata).
_LABELABLE_PREFIXES = ("select", "with")

#: ARM capacity lookups are retried: a transient failure here becomes a NULL
#: compute_label on every row of a block, which fractures the cell silently.
ARM_LOOKUP_ATTEMPTS = 4
ARM_LOOKUP_BACKOFF_S = 2.0

#: Fabric Warehouse's result-set cache is a warehouse-level setting with no session
#: switch (``SET RESULT_SET_CACHING`` is "not a recognized SET option"), so the
#: connector cannot turn it off per session the way the other engines do. It reads the
#: setting on connect and refuses while it is on: a repeat served from the cache
#: returns in ~0.7 s against ~16 s computed and would pass for engine time. A newly
#: created warehouse can have it on by default.
RESULT_CACHE_CHECK_SQL = "SELECT is_result_set_caching_on FROM sys.databases WHERE name = DB_NAME()"
RESULT_CACHE_OFF_SQL = "ALTER DATABASE CURRENT SET RESULT_SET_CACHING OFF"
#: Statement timeout for the check. Without one it can hang indefinitely on a reconnect
#: after a query timeout, queued behind the abandoned queries.
RESULT_CACHE_CHECK_TIMEOUT_S = 30
#: (endpoint, database) pairs already verified off in this process. The setting only
#: changes by an admin's ALTER DATABASE, so the check runs once per process, not on
#: every one of a burst's pre-warmed sessions or on a reconnect under load.
_RESULT_CACHE_VERIFIED_OFF: set[tuple[str, str]] = set()

#: Sessions abandoned after a timeout while their query was still inside the driver.
#: Kept referenced and never closed: closing or freeing a pyodbc connection under a
#: running ``Cursor.execute`` can segfault the process. They end with the process.
_ABANDONED_SESSIONS: list[tuple[Any, Any]] = []


def _is_labelable(sql: str) -> bool:
    """Whether ``OPTION (LABEL = ...)`` can be appended to this statement."""
    head = sql.lstrip()
    # Skip leading line/block comments so a commented query is still classified.
    while head.startswith(("--", "/*")):
        if head.startswith("--"):
            _, _, head = head.partition("\n")
        else:
            _, _, head = head.partition("*/")
        head = head.lstrip()
    return head[:6].lower().startswith(_LABELABLE_PREFIXES)


class FabricConnector(WarehouseConnector):
    """Microsoft Fabric Data Warehouse connector.

    Config keys:
        endpoint: optional explicit FQDN override (else ``FABRIC_DW_ENDPOINT``).
        database: optional override (else ``FABRIC_DW_DATABASE``).
        schema: e.g. ``"tpcds_sf1"``; bare table refs are qualified at execute
            time (Fabric has no per-session default-schema knob).
        driver: defaults to ``ODBC Driver 18 for SQL Server``.
        disable_cache: default True; ``connect()`` refuses while the warehouse's
            result-set cache is on (see ``RESULT_CACHE_CHECK_SQL``).
    """

    DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"

    #: `queryinsights.exec_requests_history` lags completion by minutes, and the
    #: lookup is a LIKE scan rather than a keyed read — so inline it would cost more
    #: than the Databricks equivalent (a measured 15.3 s/query) and return
    #: None just as reliably. Reconcile matches the same embedded label post-run.
    INLINE_SERVER_TIME: ClassVar[bool] = False

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__("fabric", config or {})
        # Recorded in QueryResult.metadata / engine_environment for provenance.
        self.component: str = "dw"
        self.conn: pyodbc.Connection | None = None
        self.cursor: Any = None
        # Schema for bare-table qualification (set here AND refreshed in
        # connect()); the benchmark passes one of tpcds_sf{N}/dsb_sf{N}/clickbench.
        self._schema: str | None = self.config.get("schema")
        # Capacity SKU cannot change mid-connection (sizing is out-of-band, between
        # passes), so the ARM lookup is cached rather than repeated per block.
        self._compute_cache: dict[str, Any] | None = None

    def _endpoint(self) -> str:
        if "endpoint" in self.config:
            return str(self.config["endpoint"])
        ep = get_settings().FABRIC.DW_ENDPOINT
        if not ep:
            raise ConnectorError("FABRIC_DW_ENDPOINT not set (or pass config['endpoint'])")
        return ep

    def _database(self) -> str:
        if "database" in self.config:
            return str(self.config["database"])
        return get_settings().FABRIC.DW_DATABASE

    def _token_struct(self) -> bytes:
        cred = DefaultAzureCredential()
        token = cred.get_token(_AAD_SCOPE_DW).token
        # SQL_COPT_SS_ACCESS_TOKEN expects a struct of (length, utf-16le bytes).
        token_bytes = token.encode("utf-16-le")
        return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)

    def connect(self) -> None:
        endpoint = self._endpoint()
        database = self._database()
        driver = self.config.get("driver", self.DEFAULT_DRIVER)
        conn_str = (
            f"Driver={{{driver}}};"
            f"Server={endpoint};"
            f"Database={database};"
            "Encrypt=yes;"
            "TrustServerCertificate=no;"
            "Connection Timeout=30;"
        )

        try:
            token_struct = self._token_struct()
            self.conn = pyodbc.connect(
                conn_str,
                attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: token_struct},
            )
            self.conn.autocommit = True
            self.cursor = self.conn.cursor()
        except Exception as e:
            logger.error("Fabric connect failed", exc_info=True)
            raise ConnectorError(f"Fabric ({self.component}) connection failed: {e}") from e

        if self.config.get("disable_cache", True):
            self._refuse_result_cache(endpoint, database)

        # Fabric T-SQL has no per-session "default schema" knob the harness
        # can set without ALTER USER (admin-only), so bare table names in a
        # query are schema-qualified at execute time (``_qualify_sql``); the
        # validator and load-check go through ``qualify_table()``.
        self._schema = self.config.get("schema")

        logger.info(
            "connected to Fabric",
            extra={
                "platform": "fabric",
                "component": self.component,
                "endpoint": endpoint,
                "database": database,
                "schema": self._schema,
            },
        )

    def _refuse_result_cache(self, endpoint: str, database: str) -> None:
        """Disconnect and raise if the warehouse's result-set cache is on.

        Runs once per (endpoint, database) per process, on its own cursor with a
        statement timeout. An endpoint without the ``is_result_set_caching_on``
        column has no such cache, so a ProgrammingError there is logged and the
        connection kept; any other failure to read the setting refuses, since it
        cannot be verified off.
        """
        key = (endpoint, database)
        if key in _RESULT_CACHE_VERIFIED_OFF:
            return
        try:
            # pyodbc applies Connection.timeout to cursors created after it is set.
            self.conn.timeout = RESULT_CACHE_CHECK_TIMEOUT_S
            check = self.conn.cursor()
            self.conn.timeout = 0
            try:
                check.execute(RESULT_CACHE_CHECK_SQL)
                row = check.fetchone()
            finally:
                check.close()
        except pyodbc.ProgrammingError:
            logger.warning(
                "Fabric endpoint has no result-set cache setting to check",
                exc_info=True,
                extra={"platform": "fabric", "database": database},
            )
            return
        except pyodbc.Error as e:
            self.disconnect()
            raise ConnectorError(
                f"could not read Fabric's result-set cache setting for {database}: {e}"
            ) from e
        if row and row[0]:
            self.disconnect()
            raise ConnectorError(
                f"Fabric warehouse {database} has its result-set cache on, so a repeated "
                "query would be served from it. Turn it off once, as a warehouse admin: "
                f"{RESULT_CACHE_OFF_SQL}"
            )
        _RESULT_CACHE_VERIFIED_OFF.add(key)

    def disconnect(self) -> None:
        if self.cursor is not None:
            try:
                self.cursor.close()
            except Exception:
                logger.debug("Fabric cursor close failed", exc_info=True)
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None
                self.cursor = None

    def cancel_running(self) -> None:
        """Cancel the running statement: pyodbc's ``Cursor.cancel`` (``SQLCancel``) is
        made to be called from another thread while ``execute`` is blocked."""
        cursor = self.cursor
        if cursor is not None:
            cursor.cancel()

    def abandon_session(self) -> None:
        """Set the session aside, unclosed, for a thread still inside ``execute`` on it
        (see :data:`_ABANDONED_SESSIONS`); :meth:`connect` then opens a new one."""
        if self.conn is not None or self.cursor is not None:
            _ABANDONED_SESSIONS.append((self.conn, self.cursor))
        self.conn = None
        self.cursor = None

    def qualify_table(self, schema: str, table: str) -> str:
        """T-SQL three-part name: ``database.schema.table``.

        DW lays out one schema per scale.
        """
        database = self._database()
        return f"{database}.{schema}.{table}"

    def count_table_rows(self, schema: str, table: str) -> int:
        """T-SQL COUNT(*) returns a 32-bit int and raises arithmetic overflow
        past 2^31-1 rows (store_sales at SF1000 has 2.88B), so use COUNT_BIG.
        Otherwise identical to the base implementation.
        """
        qualified = self.qualify_table(schema, table)
        result = self.execute_query(f"SELECT COUNT_BIG(*) FROM {qualified}")
        if result.error_message:
            raise ConnectorError(
                f"count_table_rows failed for {self.platform}.{qualified}: {result.error_message}"
            )
        if not result.rows or not result.rows[0]:
            raise ConnectorError(f"count_table_rows returned no rows for {qualified}")
        return int(result.rows[0][0])

    def _qualify_sql(self, sql: str) -> str:
        """Schema-qualify bare table references for Fabric DW.

        Fabric/T-SQL has no per-session default-schema switch (unlike
        MotherDuck's ``USE``, Redshift's ``search_path``, or BigQuery's default
        dataset), and the benchmark uses a different schema per block
        (``tpcds_sf{N}`` / ``dsb_sf{N}`` / ``clickbench``) over the same
        connection. So we rewrite each bare table reference
        ``store_sales`` → ``<schema>.store_sales`` at execute time via sqlglot,
        leaving CTE references and already-qualified names untouched. If parsing
        fails we return the SQL unchanged and let the engine surface the error.
        """
        if not self._schema:
            return sql
        try:
            expr = sqlglot.parse_one(sql, dialect="tsql")
        except Exception:
            logger.debug("sqlglot parse failed; executing unqualified", exc_info=True)
            return sql
        if expr is None:
            return sql
        cte_names = {c.alias_or_name.lower() for c in expr.find_all(exp.CTE) if c.alias_or_name}
        changed = False
        for table in expr.find_all(exp.Table):
            if table.db:  # already schema- (or db-) qualified
                continue
            if table.name.lower() in cte_names:  # CTE reference, not a base table
                continue
            table.set("db", exp.to_identifier(self._schema))
            changed = True
        return expr.sql(dialect="tsql") if changed else sql

    def runtime_environment(self) -> dict[str, Any]:
        env = super().runtime_environment()
        env["component"] = self.component  # always 'dw'
        env["capacity_sku"] = get_settings().FABRIC.CAPACITY_SKU  # f2/f4/.../f128
        version = self._query_scalar("SELECT @@VERSION")
        if version:
            env["version"] = str(version)
        return env

    def runtime_compute(self) -> dict[str, Any]:
        """Actual Fabric capacity SKU via the Azure ARM API (capacity isn't
        T-SQL queryable), to validate the running capacity matches CAPACITY_SKU.

        RETRIED, and the result CACHED for the connector's lifetime. Returning ``{}``
        on a transient failure is not harmless resilience: the runner turns a missing
        SKU into ``compute_label = NULL``, stamps it on every row of that block, and
        carries on — a fractured, silently-unlabelled cell. Building a fresh
        credential, fetching a token and calling ARM on EVERY capture makes that
        near-certain across a long run, because a handful of the dozens of calls
        will fail transiently.

        The capacity SKU cannot change mid-connection (sizing is out-of-band and this
        harness only resizes between passes), so caching is safe as well as cheap.
        ``{}`` is still returned when the settings are absent — that is a
        configuration state, not a transient one.
        """
        import json
        import time as _time
        import urllib.request

        from azure.identity import ClientSecretCredential

        if self._compute_cache is not None:
            return self._compute_cache

        fab = get_settings().FABRIC
        sub, rg, name = fab.SUBSCRIPTION_ID, fab.RESOURCE_GROUP, fab.CAPACITY_NAME
        if not (sub and rg and name):
            return {}

        last_error: Exception | None = None
        for attempt in range(ARM_LOOKUP_ATTEMPTS):
            if attempt:
                delay = ARM_LOOKUP_BACKOFF_S * (2 ** (attempt - 1))
                logger.warning(
                    "Fabric ARM capacity lookup failed (attempt %d/%d); retrying in %.0fs",
                    attempt,
                    ARM_LOOKUP_ATTEMPTS,
                    delay,
                    exc_info=last_error is not None,
                )
                _time.sleep(delay)
            try:
                if fab.TENANT_ID and fab.CLIENT_ID and fab.CLIENT_SECRET:
                    cred: Any = ClientSecretCredential(
                        fab.TENANT_ID, fab.CLIENT_ID, fab.CLIENT_SECRET.get_secret_value()
                    )
                else:
                    cred = DefaultAzureCredential()
                token = cred.get_token("https://management.azure.com/.default").token
                url = (
                    f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
                    f"/providers/Microsoft.Fabric/capacities/{name}?api-version=2023-11-01"
                )
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
            except Exception as exc:
                last_error = exc
                continue
            out: dict[str, Any] = {}
            sku = (data.get("sku") or {}).get("name")
            if sku is not None:
                out["capacity_sku"] = sku
            state = (data.get("properties") or {}).get("state")
            if state is not None:
                out["state"] = state
            if out:
                self._compute_cache = out
            return out

        logger.error(
            "Fabric ARM capacity lookup failed after %d attempts — the run will have NO "
            "compute label, which fractures the cell; fix credentials/ARM before rerunning",
            ARM_LOOKUP_ATTEMPTS,
            exc_info=last_error is not None,
        )
        return {}

    @staticmethod
    def cost_kwargs_from_compute(compute: dict[str, Any]) -> dict[str, Any] | None:
        """Map the live ARM capacity SKU → ``FabricCostCollector(capacity=...)``.

        ``runtime_compute()`` returns ``{"capacity_sku": "F8", "state": ...}``
        from the ARM capacity resource; the collector tier is that SKU
        lowercased. Needs FABRIC_SUBSCRIPTION_ID / RESOURCE_GROUP /
        CAPACITY_NAME set for the ARM lookup — without them
        ``runtime_compute()`` is empty and this returns None (recorded under
        cost-config ``errors``).
        """
        sku = compute.get("capacity_sku")
        if sku is None:
            return None
        return {"capacity": str(sku).lower()}

    def execute_query(self, sql: str) -> QueryResult:
        # Bound once: after a timeout the runner may set this session aside and open a
        # new one while this call is still running, and the call must not touch the
        # replacement cursor (it would read another query's results).
        cursor = self.cursor
        if cursor is None:
            raise ConnectorError("Fabric connector not connected")

        start = time.perf_counter()
        try:
            # Tag each statement with a unique label via the T-SQL OPTION (LABEL)
            # hint, which lands in `queryinsights.exec_requests_history.label` — a
            # dedicated indexed column, so reconcile is a keyed read.
            #
            # A leading `/* label */` block comment can NOT do this job: Fabric
            # strips leading comments before storing `command` (verified — 0 of
            # 37,169 history rows begin with a block comment), so a comment label
            # is never recorded and reconcile's LIKE-scan matches nothing.
            # Verified live: OPTION (LABEL) appears in the label column
            # within ~1 minute, carrying total_elapsed_time_ms + allocated_cpu_time_ms.
            #
            # Appended only to reads. OPTION must be the final clause of a SELECT;
            # attaching it to a SET/DDL statement is a syntax error, and the
            # connector runs a few of those. No benchmark query carries its own
            # OPTION clause or a trailing semicolon (checked across all 244).
            label = f"bmq_{uuid.uuid4().hex[:16]}"
            qualified = self._qualify_sql(sql)
            if _is_labelable(qualified):
                statement = f"{qualified.rstrip().rstrip(';')}\nOPTION (LABEL = '{label}')"
            else:
                statement = qualified
                label = None  # nothing to reconcile against
            cursor.execute(statement)
            rows = cursor.fetchall() if cursor.description is not None else []
            wall_clock_ms = (time.perf_counter() - start) * 1000
            columns = [d[0] for d in (cursor.description or [])]

            # NOT read inline — see INLINE_SERVER_TIME. Reconcile's lookup is a keyed
            # read on `label`, but `queryinsights` lags completion by ~1 minute, so an
            # inline read would return None for most queries however cheap it is.
            # Reconcile matches the label post-run.
            server_time_ms = None

            return QueryResult(
                rows=[tuple(r) for r in rows],
                columns=columns,
                row_count=len(rows),
                wall_clock_ms=wall_clock_ms,
                server_time_ms=server_time_ms,
                platform_query_id=label,
                metadata={
                    "component": self.component,
                    "database": self._database(),
                },
            )
        except Exception as e:
            wall_clock_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "Fabric query failed",
                extra={
                    "platform": "fabric",
                    "component": self.component,
                    "wall_clock_ms": wall_clock_ms,
                },
                exc_info=True,
            )
            return QueryResult(
                rows=[],
                columns=[],
                row_count=0,
                wall_clock_ms=wall_clock_ms,
                error_message=str(e),
                metadata={"component": self.component, "error_type": type(e).__name__},
            )

    def get_server_timing(
        self, platform_query_id: str | None = None, *, retries: int = 2, wait_s: float = 0.5
    ) -> float | None:
        """Server-side elapsed (ms) from ``queryinsights.exec_requests_history``.

        Matches the unique label the connector embeds in the statement text
        (``command``), which is our stable per-query identity. BEST-EFFORT:
        query-insights lags completion (often minutes), so the short inline retry
        frequently misses on a live run and returns None — the value is then
        back-filled post-run by ``benchmark reconcile`` (same label match).
        """
        cursor = self.cursor
        if cursor is None or not platform_query_id:
            return None
        label = platform_query_id.replace("'", "")
        for attempt in range(retries):
            try:
                cursor.execute(
                    "SELECT TOP 1 total_elapsed_time_ms "
                    "FROM queryinsights.exec_requests_history "
                    f"WHERE command LIKE '%{label}%' "
                    "AND command NOT LIKE '%exec_requests_history%' "
                    "ORDER BY submit_time DESC"
                )
                row = cursor.fetchone()
                if row and row[0] is not None:
                    return float(row[0])
            except Exception:
                logger.debug("Fabric server timing lookup failed", exc_info=True)
            if attempt < retries - 1:
                time.sleep(wait_s)
        return None

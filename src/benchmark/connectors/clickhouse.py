"""ClickHouse Cloud connector — `clickhouse-connect` HTTP driver.

Cost-telemetry tier: ESTIMATED. ClickHouse Cloud bills compute by the hour,
not per query. The connector takes server time (``elapsed_ns``), ``read_bytes``
and ``read_rows`` from the query summary clickhouse-connect returns with each
response, so nothing reads ``system.query_log``; the cost collector prices a
query at the service's hourly rate, and ``recost-capacity`` re-prices it on engine
time (docs/ARCHITECTURE.md §7).

Schema convention: each benchmark suite gets its own ClickHouse database
named after the schema (``tpcds_sf1``, ``dsb_sf1``, ``clickbench``); the
connector qualifies tables as ``<schema>.<table>``. This mirrors Redshift's
schema-per-suite layout — ClickHouse doesn't have a schema-inside-database
concept, so each suite is a full database here.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import clickhouse_connect

from ..exceptions import ConnectorError
from ..settings import get_settings
from .base import QueryResult, WarehouseConnector

logger = logging.getLogger("benchmark.connectors.clickhouse")


class ClickHouseConnector(WarehouseConnector):
    """ClickHouse Cloud HTTP connector.

    Config keys (all optional; fall back to CLICKHOUSE_* in settings):
        host: hostname override (defaults to CLICKHOUSE_HOST).
        user: user override (defaults to CLICKHOUSE_USER, then 'default').
        password: password override (defaults to CLICKHOUSE_PASSWORD).
        port: port override (defaults to CLICKHOUSE_PORT, then 8443 secure / 8123).
        secure: HTTPS (default True)
        schema: e.g. ``"tpcds_sf1"``; used directly as the ClickHouse database name.
        disable_cache: default True; sets ``use_query_cache = 0`` per session
        session_settings: dict merged over the per-session defaults (which
            include the correctness settings ``join_use_nulls=1`` and
            ``union_default_mode='DISTINCT'``); use to override per connection.

    Session defaults (see :meth:`_session_settings`) align ClickHouse outer-join
    NULL fill and bare-``UNION`` dedup with the DuckDB reference, so correctness
    holds for every query independent of the optional optimization pass.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__("clickhouse", config or {})
        self.client: Any = None
        self._last_query_id: str | None = None
        self._inflight_query_id: str | None = None

    def _database(self, schema: str) -> str:
        # ClickHouse has no schema-inside-database; the harness's "schema"
        # (tpcds_sf1, dsb_sf1, clickbench) maps 1:1 to a ClickHouse database
        # of the same name. Matches the Estuary materialization layout.
        return schema

    def _session_settings(self) -> dict[str, Any]:
        """Per-session ClickHouse settings applied to every query on this client.

        Two of these are correctness defaults that align ClickHouse with the
        DuckDB reference the benchmark grades against — set once here so
        correctness never depends on the optional per-query optimization pass
        (or the LLM remembering to emit a ``SETTINGS`` clause):

          * ``join_use_nulls = 1`` — fill unmatched OUTER-join columns with NULL
            (standard SQL / DuckDB), not the column type's default (0, '',
            epoch). Without it, ``IS NULL`` anti-joins and ``coalesce`` over
            right-side columns silently diverge — pervasive in TPC-DS/DSB.
          * ``union_default_mode = 'DISTINCT'`` — make a bare ``UNION``
            deduplicate like standard SQL/DuckDB instead of erroring (the empty
            default) or keeping duplicates.
          * ``group_by_use_nulls = 1`` — make ``ROLLUP``/``CUBE``/``GROUPING
            SETS`` super-aggregate rows carry NULL grouping keys (standard
            SQL / DuckDB) instead of the column type's default ('' for
            strings). Without it every ROLLUP total row diverges (e.g.
            TPC-DS q05/q14/q77/q80).

        Three more are **capability parity**, not tuning: every other engine in
        this benchmark spills to disk by default when memory runs out, while
        ClickHouse's defaults fail fast with a memory-limit error, so without
        these its DNF cells would partly measure a settings philosophy rather
        than the architecture. These let it degrade the way its peers' defaults
        do:

          * ``max_bytes_ratio_before_external_group_by = 0.7`` — spill GROUP BY
            state to disk at 70% of the memory limit (ratio form, so one value
            is right at every replica size in a sweep). Set high on purpose:
            spill is the safety valve, not an early mode switch — queries keep
            full in-memory speed until they are genuinely near the limit, and
            the remaining 30% is headroom for the spill-and-merge machinery
            itself.
          * ``max_bytes_ratio_before_external_sort = 0.7`` — same for sorts.
          * ``join_algorithm = 'direct,parallel_hash,hash,grace_hash'`` — the
            server default list plus ``grace_hash`` appended as the spillable
            fallback, so a join that cannot fit in memory partitions to disk
            instead of aborting. The default algorithms stay first, so
            in-memory plans are unchanged.

        ``use_query_cache`` is a benchmarking concern (measure cold compute, not
        cache). Any key can be overridden via ``config['session_settings']``.
        """
        settings: dict[str, Any] = {
            "use_query_cache": 0 if self.config.get("disable_cache", True) else 1,
            "join_use_nulls": 1,
            "union_default_mode": "DISTINCT",
            "group_by_use_nulls": 1,
            "max_bytes_ratio_before_external_group_by": 0.7,
            "max_bytes_ratio_before_external_sort": 0.7,
            "join_algorithm": "direct,parallel_hash,hash,grace_hash",
        }
        settings.update(self.config.get("session_settings") or {})
        return settings

    def connect(self) -> None:
        ch = get_settings().CLICKHOUSE
        host = self.config.get("host") or ch.HOST
        user = self.config.get("user") or ch.USER
        password = self.config.get("password") or (
            ch.PASSWORD.get_secret_value() if ch.PASSWORD else ""
        )
        if not host:
            raise ConnectorError("CLICKHOUSE_HOST missing (env or config['host'])")
        secure = bool(self.config.get("secure", True))
        port = int(self.config.get("port") or ch.PORT or (8443 if secure else 8123))

        # The CLI passes config['schema'] = 'tpcds_sf1' / 'dsb_sf1' /
        # 'clickbench' and expects unqualified table references in the
        # canonical queries to resolve against the matching ClickHouse
        # database. Set it as the client's default database; the runner can
        # still issue fully-qualified names where needed.
        schema = self.config.get("schema")
        database = self._database(schema) if schema else None

        try:
            self.client = clickhouse_connect.get_client(
                host=host,
                port=port,
                username=user,
                password=password,
                database=database,
                secure=secure,
                settings=self._session_settings(),
                # Sessionless on purpose. clickhouse-connect retries a dropped
                # request on the SAME auto-generated session id; if attempt 1 is
                # still executing server-side, attempt 2 hits SESSION_IS_LOCKED
                # (code 373), and a long-running zombie then poisons every
                # subsequent query on the client: one timeout can turn into a 373
                # cascade over the rest of a warm block. The harness needs no
                # session state (no temp tables / SET): the client-level settings
                # above are sent with every request regardless.
                autogenerate_session_id=False,
            )
        except Exception as e:
            logger.error("ClickHouse connect failed", exc_info=True)
            raise ConnectorError(f"ClickHouse connection failed: {e}") from e

        logger.info(
            "connected to ClickHouse Cloud",
            extra={
                "platform": "clickhouse",
                "host": host,
                "secure": secure,
                "database": database,
            },
        )

    def disconnect(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                logger.debug("ClickHouse close failed", exc_info=True)
            finally:
                self.client = None

    def qualify_table(self, schema: str, table: str) -> str:
        return f"{self._database(schema)}.{table}"

    def runtime_environment(self) -> dict[str, Any]:
        env = super().runtime_environment()
        env["host"] = self.config.get("host") or get_settings().CLICKHOUSE.HOST
        version = self._query_scalar("SELECT version()")
        if version:
            env["version"] = str(version)
        return env

    def runtime_compute(self) -> dict[str, Any]:
        """Actual ClickHouse Cloud service sizing, merged from two sources so a
        single call serves BOTH the drift check and cost pricing:

          * Cloud API (if creds present): ``num_replicas`` + memory + tier/state —
            the service is provisioned out-of-band, so replica count comes from
            here.
          * SQL: per-replica ``vcpus`` (``CGroupMaxCPU``) and a ``replicas`` count
            (``system.clusters``) — works with no Cloud-API creds, and ``vcpus``
            is the cost driver (see :meth:`cost_kwargs_from_compute`).

        Drift uses ``num_replicas``; cost uses ``vcpus`` × ``num_replicas``
        (falling back to the SQL ``replicas`` count). Resilient: returns whatever
        it could gather, ``{}`` if nothing.
        """
        out: dict[str, Any] = {}

        ch = get_settings().CLICKHOUSE
        org, service, key_id = ch.ORG_ID, ch.SERVICE_ID, ch.API_KEY_ID
        key_secret = ch.API_KEY_SECRET.get_secret_value() if ch.API_KEY_SECRET else None
        if org and service and key_id and key_secret:
            import base64
            import json
            import urllib.request

            creds = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
            url = f"https://api.clickhouse.cloud/v1/organizations/{org}/services/{service}"
            req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                svc = data.get("result", data)  # the API wraps the service under "result"
                if isinstance(svc, dict):
                    field_map = {
                        "numReplicas": "num_replicas",
                        "minReplicaMemoryGb": "min_replica_memory_gb",
                        "maxReplicaMemoryGb": "max_replica_memory_gb",
                        "tier": "tier",
                        "state": "state",
                    }
                    out.update(
                        {
                            dst: svc[src]
                            for src, dst in field_map.items()
                            if svc.get(src) is not None
                        }
                    )
            except Exception:
                logger.debug("ClickHouse Cloud API lookup failed", exc_info=True)

        # SQL sizing — per-replica vCPUs and a replica count. Resilient: skip on error.
        try:
            cpu = self.execute_query(
                "SELECT value FROM system.asynchronous_metrics WHERE metric = 'CGroupMaxCPU'"
            )
            if not cpu.error_message and cpu.rows and cpu.rows[0][0] is not None:
                vcpus = round(float(cpu.rows[0][0]))
                if vcpus > 0:
                    out["vcpus"] = vcpus
            rep = self.execute_query(
                "SELECT count() FROM system.clusters WHERE cluster = 'default'"
            )
            if not rep.error_message and rep.rows and rep.rows[0][0] is not None:
                replicas = int(rep.rows[0][0])
                if replicas > 0:
                    out["replicas"] = replicas
        except Exception:
            logger.debug("ClickHouse SQL sizing lookup failed", exc_info=True)

        return out

    @staticmethod
    def cost_kwargs_from_compute(compute: dict) -> dict | None:
        """Map live sizing → ``ClickHouseCostCollector(vcpus=, replicas=)``.

        Needs per-replica ``vcpus`` (SQL ``CGroupMaxCPU``) and a replica count —
        preferring the Cloud-API ``num_replicas`` and falling back to the SQL
        ``replicas`` count. Returns None if either is missing.
        """
        vcpus = compute.get("vcpus")
        replicas = compute.get("num_replicas") or compute.get("replicas")
        if not vcpus or not replicas:
            return None
        return {"vcpus": int(vcpus), "replicas": int(replicas)}

    def cancel_inflight(self) -> None:
        """Best-effort server-side KILL of the last submitted query.

        Called by ``execute_with_timeout`` after a client-side timeout: without
        it the abandoned query keeps executing server-side (there is no
        server-side timeout), burning CPU under later measurements. Uses a
        fresh short-lived client because the main client may be wedged.
        """
        qid = self._inflight_query_id
        if not qid:
            return
        ch = get_settings().CLICKHOUSE
        try:
            killer = clickhouse_connect.get_client(
                host=self.config.get("host") or ch.HOST,
                port=int(self.config.get("port") or ch.PORT or 8443),
                username=self.config.get("user") or ch.USER,
                password=self.config.get("password")
                or (ch.PASSWORD.get_secret_value() if ch.PASSWORD else ""),
                secure=bool(self.config.get("secure", True)),
                autogenerate_session_id=False,
            )
            try:
                killer.command(f"KILL QUERY WHERE query_id = '{qid}' ASYNC")
                logger.info(
                    "killed in-flight query", extra={"platform": "clickhouse", "query_id": qid}
                )
            finally:
                killer.close()
        except Exception:
            logger.warning("KILL QUERY failed for %s", qid, exc_info=True)

    def execute_query(self, sql: str) -> QueryResult:
        if self.client is None:
            raise ConnectorError("ClickHouse connector not connected")

        # ClickHouse can be told to use a default database via `database`
        # query parameter, but the harness fully-qualifies tables already.
        # The client-generated query_id makes the in-flight query killable
        # (see cancel_inflight) — the server-assigned id is only known on
        # completion, too late for a timeout kill.
        self._inflight_query_id = str(uuid.uuid4())
        start = time.perf_counter()
        try:
            # query_id rides in per-query settings (this clickhouse-connect
            # version has no query_id kwarg); client-level settings still apply.
            res = self.client.query(sql, settings={"query_id": self._inflight_query_id})
            wall_clock_ms = (time.perf_counter() - start) * 1000
            rows = [tuple(r) for r in res.result_rows]
            columns = list(res.column_names)
            query_id = getattr(res, "query_id", None) or self._inflight_query_id
            self._last_query_id = query_id
            self._inflight_query_id = None
            summary = getattr(res, "summary", {}) or {}
            metadata: dict[str, Any] = {
                "query_id": query_id,
                "read_bytes": int(summary.get("read_bytes", 0) or 0),
                "read_rows": int(summary.get("read_rows", 0) or 0),
                "elapsed_ns": int(summary.get("elapsed_ns", 0) or 0),
            }
            return QueryResult(
                rows=rows,
                columns=columns,
                row_count=len(rows),
                bytes_scanned=metadata["read_bytes"],
                wall_clock_ms=wall_clock_ms,
                server_time_ms=metadata["elapsed_ns"] / 1_000_000.0
                if metadata["elapsed_ns"]
                else None,
                platform_query_id=query_id,
                metadata=metadata,
            )
        except Exception as e:
            wall_clock_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "ClickHouse query failed",
                extra={"platform": "clickhouse", "wall_clock_ms": wall_clock_ms},
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

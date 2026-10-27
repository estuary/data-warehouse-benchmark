"""MotherDuck connector — DuckDB SDK against the `md:` remote protocol."""

from __future__ import annotations

import logging
import time
from typing import Any

import duckdb

from ..exceptions import ConnectorError
from ..settings import get_settings
from .base import QueryResult, WarehouseConnector

logger = logging.getLogger("benchmark.connectors.motherduck")


class MotherDuckConnector(WarehouseConnector):
    """MotherDuck connector.

    Config keys:
        token: MOTHERDUCK_TOKEN (falls back to env var)
        database: database name (default: 'benchmark_db')
        schema: schema to USE before each query (default: None)
    """

    def __init__(self, config: dict | None = None) -> None:
        super().__init__("motherduck", config or {})
        self.con: duckdb.DuckDBPyConnection | None = None

    def connect(self) -> None:
        md = get_settings().MOTHERDUCK
        token = self.config.get("token") or (md.TOKEN.get_secret_value() if md.TOKEN else None)
        if not token:
            raise ConnectorError("MOTHERDUCK_TOKEN missing (env or config)")
        database = self.config.get("database", "benchmark_db")

        try:
            self.con = duckdb.connect(f"md:{database}?motherduck_token={token}")
        except Exception as e:
            logger.error("MotherDuck connect failed", exc_info=True)
            raise ConnectorError(f"MotherDuck connection failed: {e}") from e

        schema = self.config.get("schema")
        if schema:
            self.con.execute(f"USE {database}.{schema}")

        logger.info(
            "connected to MotherDuck",
            extra={"platform": "motherduck"},
        )

    def disconnect(self) -> None:
        if self.con is not None:
            try:
                self.con.close()
            finally:
                self.con = None

    def qualify_table(self, schema: str, table: str) -> str:
        """MotherDuck three-part name: ``<database>.<schema>.<table>``."""
        database = self.config.get("database", "benchmark_db")
        return f"{database}.{schema}.{table}"

    def _last_query_id(self) -> str | None:
        """Server-side id of the most recent query (``MD_LAST_QUERY_ID``).

        Cheap and immediate, captured inline per query. Server-side *timing* is
        NOT fetched here — that's done post-run in one batch by
        :meth:`lookup_server_times` (driven by ``benchmark reconcile``) from the
        durable ``MD_QUERY_HISTORY()``, so a whole run back-fills at once.
        """
        try:
            row = self.con.execute("SELECT MD_LAST_QUERY_ID()").fetchone()
            return str(row[0]) if row and row[0] else None
        except Exception as e:
            logger.warning("Could not retrieve MD_LAST_QUERY_ID(): %s", e)
            return None

    def lookup_server_times(
        self, query_ids: list[str], *, settle_seconds: float = 10.0
    ) -> dict[str, dict[str, Any]]:
        """Batch server-side timing for a whole run from ``MD_QUERY_HISTORY()``.

        Sourced from ``MD_QUERY_HISTORY()`` — MotherDuck's **durable** account
        query history (retains 1y+, not the ~1-2 min rolling window of
        ``md_information_schema.recent_queries``). A whole run's ids can be
        back-filled long after it finishes, so post-run batch reconcile is
        reliable at any scale. Waits ``settle_seconds`` once for a just-finished
        query to land, then a single query fetches ``total_elapsed_time`` +
        authoritative ``start_time`` / ``end_time`` for every id at once — no
        per-query retry race. Returns
        ``{query_id: {"server_time_ms", "start_time", "end_time"}}`` (timestamps as
        ISO strings); ids with no landed row are omitted. Empty dict means the
        history function was inaccessible. Also returns profiling from the same
        row — ``bytes_scanned`` (bytes downloaded from cloud storage),
        ``bytes_spilled`` (spilled to disk) and ``queue_ms`` (wait_time). MotherDuck
        exposes no separate compile time. ``start``/``end`` feed the suite cost
        (``analysis/motherduck_cost.py``).
        """
        ids = [q for q in query_ids if q]
        if not ids:
            return {}
        if settle_seconds > 0:
            time.sleep(settle_seconds)
        placeholders = ", ".join("?" for _ in ids)
        try:
            rows = self.con.execute(
                f"""
                SELECT CAST(query_id AS VARCHAR) AS query_id,
                       EXTRACT(EPOCH FROM total_elapsed_time) * 1000 AS server_time_ms,
                       start_time, end_time,
                       bytes_downloaded, bytes_spilled_to_disk,
                       EXTRACT(EPOCH FROM wait_time) * 1000 AS queue_ms
                FROM MD_QUERY_HISTORY()
                WHERE CAST(query_id AS VARCHAR) IN ({placeholders})
                """,
                ids,
            ).fetchall()
        except Exception:
            logger.warning(
                "MotherDuck MD_QUERY_HISTORY() inaccessible — server_time_ms stays NULL",
                exc_info=True,
            )
            return {}
        out: dict[str, dict[str, Any]] = {}
        for qid, server_ms, start, end, bytes_dl, spilled, queue_ms in rows:
            if server_ms is None:
                continue
            out[str(qid)] = {
                "server_time_ms": float(server_ms),
                "start_time": start.isoformat() if start is not None else None,
                "end_time": end.isoformat() if end is not None else None,
                "bytes_scanned": int(bytes_dl) if bytes_dl is not None else None,
                "bytes_spilled": int(spilled) if spilled is not None else None,
                "queue_ms": float(queue_ms) if queue_ms is not None else None,
            }
        return out

    def runtime_environment(self) -> dict[str, Any]:
        env = super().runtime_environment()
        env["database"] = self.config.get("database", "benchmark_db")
        version = self._query_scalar("SELECT version()")  # DuckDB engine version
        if version:
            env["version"] = str(version)
        return env

    def runtime_compute(self) -> dict[str, Any]:
        """Actual MotherDuck instance ('duckling') size via md_live_duckling_size(),
        to validate the running instance matches the provisioned tier AND to price
        it (the Duckling tier is both the drift key and the cost driver — see
        :meth:`cost_kwargs_from_compute`). Resilient: {} on error.

        The tier name lives in the ``type`` column (e.g. 'standard'); size-named
        columns are a fallback. Returned under the ``size`` key, which the drift
        check compares (``actual['size']`` vs provisioned ``tier``).
        """
        try:
            result = self.execute_query("SELECT * FROM md_live_duckling_size()")
        except Exception:
            return {}
        if result.error_message or not result.rows or not result.rows[0]:
            return {}
        row = result.rows[0]
        cols = {c.lower(): i for i, c in enumerate(result.columns)}
        for key in ("type", "duckling_size", "instance_size", "size"):
            idx = cols.get(key)
            if idx is not None and idx < len(row) and row[idx] is not None:
                return {"size": row[idx]}
        return {"size": row[0]} if row[0] is not None else {}

    @staticmethod
    def cost_kwargs_from_compute(compute: dict) -> dict | None:
        """Map the live Duckling size → ``MotherDuckCostCollector(tier=...)``.

        ``runtime_compute()`` returns ``{"size": "standard"}``; the tier is that
        value lowercased. No env var, no UI, no second engine query.
        """
        size = compute.get("size")
        if size is None:
            return None
        return {"tier": str(size).lower()}

    def execute_query(self, sql: str) -> QueryResult:
        if self.con is None:
            raise ConnectorError("MotherDuck connector not connected")
        start = time.perf_counter()
        try:
            cursor = self.con.execute(sql)
            rows = cursor.fetchall()
            elapsed_ms = (time.perf_counter() - start) * 1000
            columns = [d[0] for d in (cursor.description or [])]
            # Capture only the (immediate) query id inline; server-side timing +
            # start/end are back-filled post-run in one batch by `benchmark reconcile`
            # (lookup_server_times) from the durable MD_QUERY_HISTORY().
            return QueryResult(
                rows=rows,
                columns=columns,
                row_count=len(rows),
                wall_clock_ms=elapsed_ms,
                server_time_ms=None,
                platform_query_id=self._last_query_id(),
            )
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "MotherDuck query failed",
                extra={"platform": "motherduck", "wall_clock_ms": elapsed_ms},
                exc_info=True,
            )
            return QueryResult(
                rows=[],
                columns=[],
                row_count=0,
                wall_clock_ms=elapsed_ms,
                server_time_ms=None,
                error_message=str(e),
                metadata={"error_type": type(e).__name__},
            )

"""BigQuery connector — `google-cloud-bigquery` SDK.

Cost telemetry tier: DIRECT on demand, PROPORTIONAL on a reservation (see
``benchmark.cost.bigquery``). Each query job exposes ``total_bytes_billed``,
``total_bytes_processed``, and ``slot_millis``; the cost collector reads them
from ``QueryResult.metadata`` (no separate billing query needed).

Schema convention: the harness passes the per-suite dataset name
(``tpcds_sf{N}``, ``dsb_sf{N}``, ``clickbench``) as ``config["schema"]``; the
layout has one dataset per suite and scale factor, as terraform/modules/bigquery
creates them.

Connection inputs (env or config):
    BIGQUERY_PROJECT (required) — GCP project ID
    BIGQUERY_GCP_SA_KEY — path to service account JSON (tilde-expanded).
        If set, credentials are loaded explicitly from this file. If unset,
        falls back to the default google-cloud-bigquery auth chain
        (GOOGLE_APPLICATION_CREDENTIALS / ADC / metadata server).
    BIGQUERY_REGION (optional, default us-central1) — only relevant for jobs
        that need an explicit location (e.g., loading from multi-region GCS).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from google.cloud import bigquery
from google.oauth2 import service_account

from ..exceptions import ConnectorError
from ..settings import get_settings
from .base import QueryResult, WarehouseConnector

logger = logging.getLogger("benchmark.connectors.bigquery")


class BigQueryConnector(WarehouseConnector):
    """BigQuery connector with DIRECT cost telemetry.

    Config keys:
        project_id: GCP project (falls back to BIGQUERY_PROJECT env).
        region: default us-central1 (falls back to BIGQUERY_REGION env).
        schema: dataset name ("tpcds_sf1", "dsb_sf1", "clickbench"), used as the
            default dataset for each query so unqualified table references resolve.
        suite: the block the dataset belongs to; recorded in the runtime
            environment only.
        run_id: optional label propagated to BigQuery query job labels for
            billing attribution / dashboard slicing.
    """

    DEFAULT_REGION = "us-central1"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__("bigquery", config or {})
        self.client: bigquery.Client | None = None
        self.project_id: str | None = None
        self.region: str = self.DEFAULT_REGION
        self.default_dataset: str | None = None

    def connect(self) -> None:
        bq = get_settings().BIGQUERY
        self.project_id = self.config.get("project_id") or bq.PROJECT
        if not self.project_id:
            raise ConnectorError("BIGQUERY_PROJECT missing (set env var or config['project_id'])")
        self.region = self.config.get("region") or bq.REGION or self.DEFAULT_REGION

        try:
            credentials = None
            sa_key_path = self.config.get("sa_key_path") or bq.GCP_SA_KEY
            if sa_key_path:
                credentials = service_account.Credentials.from_service_account_file(
                    os.path.expanduser(str(sa_key_path))
                )
            self.client = bigquery.Client(
                project=self.project_id, location=self.region, credentials=credentials
            )
        except Exception as e:
            logger.error("BigQuery connect failed", exc_info=True)
            raise ConnectorError(f"BigQuery connection failed: {e}") from e

        schema = self.config.get("schema")
        if schema:
            # The CLI passes the per-suite dataset name (tpcds_sf1 / dsb_sf1 /
            # clickbench), matching the datasets created by terraform/modules/bigquery.
            self.default_dataset = schema

        logger.info(
            "connected to BigQuery",
            extra={
                "platform": "bigquery",
                "project_id": self.project_id,
                "region": self.region,
                "default_dataset": self.default_dataset,
            },
        )

    def disconnect(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            finally:
                self.client = None

    def qualify_table(self, schema: str, table: str) -> str:
        """BigQuery three-part backtick-quoted reference.

        ``schema`` here is the BigQuery dataset name as the CLI prepares it
        per suite (e.g. ``tpcds_sf1``, ``dsb_sf1``, ``clickbench``) —
        matches the datasets provisioned by terraform/modules/bigquery.
        Backticks are mandatory for project IDs that contain hyphens.
        """
        if self.project_id is None:
            raise ConnectorError("BigQuery connector not connected")
        return f"`{self.project_id}.{schema}.{table}`"

    def runtime_environment(self) -> dict[str, Any]:
        env = super().runtime_environment()
        env.update({"project": self.project_id, "region": self.region})
        return env

    def runtime_compute(self) -> dict[str, Any]:
        """Actual reservation slots/edition via INFORMATION_SCHEMA.RESERVATIONS,
        to validate the assigned reservation matches the provisioned slots.
        Resilient: {} on any error (no reservation, no permission, etc.).
        """
        sql = (
            "SELECT slot_capacity, edition "
            f"FROM `region-{self.region.lower()}`.INFORMATION_SCHEMA.RESERVATIONS "
            "ORDER BY slot_capacity DESC LIMIT 1"
        )
        try:
            result = self.execute_query(sql)
        except Exception:
            return {}
        if result.error_message or not result.rows:
            return {}
        cols = {c.lower(): i for i, c in enumerate(result.columns)}
        row = result.rows[0]
        # Fall back to positional lookup when column names are absent: the SELECT has
        # a fixed (slot_capacity, edition) order, and returning {} here would label a
        # reservation run as on-demand (and price it on-demand).
        if not cols:
            cols = {"slot_capacity": 0, "edition": 1}
        out: dict[str, Any] = {}
        for key in ("slot_capacity", "edition"):
            idx = cols.get(key)
            if idx is not None and idx < len(row) and row[idx] is not None:
                out[key] = row[idx]
        return out

    @staticmethod
    def cost_kwargs_from_compute(compute: dict) -> dict | None:
        """Map the live reservation state → ``BigQueryCostCollector(tier=...)``.

        ``runtime_compute()`` returns ``{"slot_capacity", "edition"}`` when a
        reservation exists in the region (slot-based ``enterprise``) and ``{}``
        when none does (per-TiB ``on-demand``). Note: ``{}`` also results from a
        transient introspection error, which therefore reads as ``on-demand``; re-run
        ``capture-cost-config`` if a reservation is expected but absent here.

        ``slot_capacity`` is carried through for **provenance**: it records which
        reservation size a captured entry describes, so a per-size ``sizes`` entry
        is self-describing rather than an opaque ``{"tier": "enterprise"}``.
        ``collector_for`` filters it out before constructing the collector (it is
        not a ``BigQueryCostCollector`` kwarg), so it does not by itself select
        the committed pricing model — that would require passing
        ``reservation_slots``. The inline price is consumed slot-time;
        ``analysis.recost.recost_bigquery_capacity`` then re-prices reservation rows
        on the baseline held for the time the queries held it.
        """
        has_reservation = bool(compute.get("edition") or compute.get("slot_capacity"))
        if not has_reservation:
            return {"tier": "on-demand"}
        kwargs: dict = {"tier": "enterprise"}
        if compute.get("slot_capacity"):
            kwargs["slot_capacity"] = int(compute["slot_capacity"])
        return kwargs

    def execute_query(self, sql: str) -> QueryResult:
        if self.client is None:
            raise ConnectorError("BigQuery connector not connected")

        job_config = bigquery.QueryJobConfig(
            use_query_cache=False,
            use_legacy_sql=False,
            default_dataset=(
                f"{self.project_id}.{self.default_dataset}" if self.default_dataset else None
            ),
            labels={
                "benchmark": "2026",
                "run_id": str(self.config.get("run_id", "benchmark"))[:63],
            },
        )

        start = time.perf_counter()
        try:
            query_job = self.client.query(sql, job_config=job_config)
            iterator = query_job.result()
            rows = [tuple(row.values()) for row in iterator]
            wall_clock_ms = (time.perf_counter() - start) * 1000

            columns = [field.name for field in (query_job.schema or [])] if query_job.schema else []

            server_time_ms: float | None = None
            if query_job.ended and query_job.started:
                server_time_ms = (query_job.ended - query_job.started).total_seconds() * 1000
            # The job's wait in BigQuery's query queue (state PENDING) runs from its
            # creation to its start, outside ``server_time_ms``; Google documents
            # queue time as start_time minus creation_time.
            queue_ms: float | None = None
            if query_job.started and query_job.created:
                queue_ms = max((query_job.started - query_job.created).total_seconds() * 1000, 0.0)

            cache_hit = bool(query_job.cache_hit) if query_job.cache_hit is not None else False
            metadata: dict[str, Any] = {
                "job_id": query_job.job_id,
                "total_bytes_billed": int(query_job.total_bytes_billed or 0),
                "total_bytes_processed": int(query_job.total_bytes_processed or 0),
                "slot_millis": int(query_job.slot_millis or 0),
                "cache_hit": cache_hit,
                "statement_type": query_job.statement_type or "UNKNOWN",
            }

            return QueryResult(
                rows=rows,
                columns=columns,
                row_count=len(rows),
                wall_clock_ms=wall_clock_ms,
                server_time_ms=server_time_ms,
                queue_ms=queue_ms,
                bytes_scanned=int(query_job.total_bytes_billed or 0),
                # BigQuery cache_hit is all-or-nothing → 100% or 0% of scan cached.
                # Golden rule #9 expects this to be 0 (query cache disabled).
                cache_hit_pct=100.0 if cache_hit else 0.0,
                platform_query_id=query_job.job_id,
                metadata=metadata,
            )
        except Exception as e:
            wall_clock_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "BigQuery query failed",
                extra={"platform": "bigquery", "wall_clock_ms": wall_clock_ms},
                exc_info=True,
            )
            return QueryResult(
                rows=[],
                columns=[],
                row_count=0,
                wall_clock_ms=wall_clock_ms,
                server_time_ms=None,
                error_message=str(e),
                metadata={"error_type": type(e).__name__},
            )

    def dry_run_estimate_bytes(self, sql: str) -> int:
        """Submit a dry-run job and return ``total_bytes_processed``.

        Useful for cost projection without executing the query.
        Returns 0 if the dry-run itself fails.
        """
        if self.client is None:
            raise ConnectorError("BigQuery connector not connected")
        job_config = bigquery.QueryJobConfig(
            use_query_cache=False,
            use_legacy_sql=False,
            dry_run=True,
            default_dataset=(
                f"{self.project_id}.{self.default_dataset}" if self.default_dataset else None
            ),
        )
        try:
            job = self.client.query(sql, job_config=job_config)
            return int(job.total_bytes_processed or 0)
        except Exception:
            logger.warning("BigQuery dry-run failed", exc_info=True)
            return 0

    def get_server_timing(self, platform_query_id: str) -> float | None:
        """Pull (ended - started) for a completed job from the history."""
        if self.client is None or not platform_query_id:
            return None
        try:
            job = self.client.get_job(platform_query_id, location=self.region)
            if job.ended and job.started:
                return (job.ended - job.started).total_seconds() * 1000
        except Exception:
            logger.debug("get_server_timing failed", exc_info=True)
        return None

"""Post-run reconciliation: authoritative server time + reconciled cost.

Several engines record only a wall-clock cost *estimate* inline (and sometimes a
NULL server time) at run time, because their history/billing systems lag. This
module re-queries each engine after a run and back-fills the recorded rows,
matching on the captured ``platform_query_id``:

  - **Snowflake** — ``SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY``
    (``credits_attributed_compute`` + ``credits_used_query_acceleration``; this
    view has no per-query cloud-services credits). 6-8h attribution latency. The
    credits are recorded as ``metadata.attributed_credits``, a cross-check: the cost
    is list rate x engine time, like every engine's (``analysis.recost``), because
    attribution counts whole warehouse-seconds, 1.2-1.7x engine time on short
    queries. Server time is captured inline from
    QUERY_HISTORY. Joins on the captured ``sfqid``.
  - **Databricks** — ``system.query.history`` (``execution_duration_ms``) +
    ``system.billing.usage`` (DBUs) × ``system.billing.list_prices``.
    Latency ~minutes; 365-day retention. Also back-fills ``queue_ms``
    (``waiting_at_capacity_duration_ms`` + ``waiting_for_compute_duration_ms``)
    and ``compile_ms`` (``compilation_duration_ms``).
  - **Redshift Serverless** — ``SYS_QUERY_HISTORY`` (``execution_time``, µs) for
    server time, with ``queue_time`` and ``compile_time`` for ``queue_ms`` and
    ``compile_ms`` (query-owner visible) + ``SYS_SERVERLESS_USAGE``
    (``charged_seconds`` = RPU-seconds, **superuser-only**) for cost. Queried
    separately so a non-superuser still gets authoritative server time when the
    usage view is inaccessible. 7-day retention.
  - **Fabric DW** — ``queryinsights.exec_requests_history``
    (``total_elapsed_time_ms``, plus ``allocated_cpu_time_ms`` and the span into
    metadata). Up to 15-min lag; 30-day. Matched by the unique label the connector
    attaches as an ``OPTION (LABEL)`` hint. Cost is not per statement: the cell's
    capacity windows are re-priced afterwards and split by CPU share
    (``analysis.recost.recost_fabric_windows``).

``server_time_ms`` on Databricks, Redshift and BigQuery is execution only: time
a statement spent waiting in the engine's queue is not in it, where Snowflake's,
Fabric's, MotherDuck's and ClickHouse's figures are total elapsed. Under
concurrency that wait is most of the engine's time on a query, so those three
engines' queue time is back-filled into ``queue_ms`` (**BigQuery** from
``INFORMATION_SCHEMA.JOBS_BY_PROJECT``, ``start_time - creation_time``, 180-day
history), and :data:`QUEUE_RECONCILERS` keeps a row a reconcile candidate until
it lands.

Each row's inline cost is recorded as ESTIMATED (see ``cost/*.py``); a successful
reconcile upgrades it to the engine's authoritative tier (``RECONCILED_CONFIDENCE``:
PROPORTIONAL for Databricks/Redshift/Fabric; Snowflake's records attribution only).

**MotherDuck** reconciles **server time** (not cost): the connector captures only
the query id inline, and this back-fills ``total_elapsed_time`` (+ authoritative
start/end for suite cost) for the whole run in one batch from the **durable**
``MD_QUERY_HISTORY()`` (1y+ retention, so it works long after the run — unlike the
~1-2 min ``recent_queries`` window). Its cost stays ESTIMATED
(wall-clock tier model; run-level warm/cold/suite cost is derived at analysis time,
see ``analysis/motherduck_cost.py``).

ClickHouse's per-query metrics are immediate (``elapsed_ns``), so it needs no
reconciliation and is absent from ``RECONCILERS``. BigQuery's server time and
cost are immediate too (``total_bytes_billed``); it reconciles queue time only.

PREREQUISITES
-------------
Every per-engine query below is built from the provider's system-table
documentation and has run against its live engine; each builder notes what it
depends on. Privileges and timing:
  - Snowflake: ACCOUNT_USAGE imported-privileges grant; run >=8h post-workload
    (widen ``--window-hours`` accordingly).
  - Databricks: SELECT on ``system.query`` + ``system.billing``; the warehouse id
    is derived from the connector's HTTP path automatically.
  - Redshift: cost needs a superuser / ``SYSLOG ACCESS UNRESTRICTED`` role and
    must run within the 7-day ``SYS_SERVERLESS_USAGE`` retention window; server
    time degrades gracefully without it.
  - Fabric: ``queryinsights`` read access; DW component only; >=15-min lag.

NOT COVERED: ``bytes_scanned`` back-fill for Databricks/Redshift/Fabric — the
provider system-table column for bytes read isn't verifiable without a live
engine, so it is left unpopulated rather than guessed. (BigQuery/ClickHouse capture bytes inline.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..cost.base import CostCollector, CostConfidence

if TYPE_CHECKING:
    from ..connectors.base import WarehouseConnector
    from ..results import ResultsDB

logger = logging.getLogger("benchmark.analysis.reconcile")

# Per-engine $ rates are single-sourced from the cost collectors (cost/*.py):
# reconcile builds the platform's collector and reads its rate, so a price fix
# touches one place. The CLI ``--rates`` JSON still overrides per run (keys:
# snowflake_credit_usd, redshift_rpu_usd_per_hour, fabric_capacity_usd_per_hour).
# Databricks needs no warehouse id: its reconcile finds each statement's warehouse
# in the history (see databricks_reconcile_sql).

#: Statement ids per Databricks reconcile query. The id list appears twice in the
#: SQL; this keeps a --all-cells back-fill over thousands of rows at a few hundred KB
#: per statement. Batching cannot change a result: each hour's share denominator is
#: every statement on the warehouse that hour, whatever the batch holds.
DATABRICKS_RECONCILE_BATCH = 2_000

# Authoritative cost tier a row is stamped with after a successful reconcile.
# The inline value is recorded ESTIMATED (see cost/*.py); reconcile upgrades it.
RECONCILED_CONFIDENCE = {
    "databricks": CostConfidence.PROPORTIONAL,
    "redshift": CostConfidence.PROPORTIONAL,
    "fabric": CostConfidence.PROPORTIONAL,
}


@dataclass
class ReconciledMetric:
    """Authoritative metrics for one recorded query, from engine history."""

    platform_query_id: str
    server_time_ms: float | None
    cost_usd: float | None
    metadata: dict | None = None  # merged into the row's metadata JSON (e.g. start/end)
    bytes_scanned: int | None = None  # profiling back-fill (e.g. MotherDuck)
    bytes_spilled: int | None = None
    compile_ms: float | None = None
    queue_ms: float | None = None


def _ms(value: object) -> float | None:
    """A history duration as float milliseconds, None when absent."""
    return float(value) if value is not None else None  # type: ignore[arg-type]


#: Engines whose reconciler back-fills ``queue_ms``. A row on one of them stays a
#: reconcile candidate until its queue time lands, so re-running ``reconcile``
#: fills it in for rows reconciled before it was captured. Databricks keeps its
#: history for a year and BigQuery for 180 days; Redshift for seven days, so older
#: Redshift rows stay candidates and update nothing.
QUEUE_RECONCILERS = frozenset({"bigquery", "databricks", "redshift"})

#: Engines whose reconciler back-fills ``metadata.cpu_ms``, which their cost is split
#: by. A row stays a candidate until it lands, for as long as the engine keeps the
#: history (``results.CPU_HISTORY_DAYS``).
CPU_RECONCILERS = frozenset({"fabric"})


# Reconcile lookback tz buffer. The recorded created_at (and the --window-hours
# window) are in the harness's LOCAL tz, stored naive, but each engine's
# history/billing timestamps are in ITS account tz — which can be hours off. E.g.
# Snowflake QUERY_ATTRIBUTION_HISTORY.start_time is in the account's tz; with the
# harness's created_at hours ahead of it, the oldest band's attribution falls just
# before the naive lookback start and matches 0 rows. Padding the auto-widened start
# by the maximum offset between any two zones (UTC+14..UTC-12 = 26h) guarantees a
# naive local window always encloses the engine timestamp regardless of account tz;
# the explicit query_id filter keeps the scan narrow, so over-widening costs nothing.
_RECONCILE_TZ_BUFFER_HOURS = 26


def _widen_lookback(window_iso: tuple[str, str], oldest: object) -> tuple[str, str]:
    """Extend the window start back to cover the oldest candidate row.

    Pushes the start to ``oldest - _RECONCILE_TZ_BUFFER_HOURS`` so an engine account tz
    behind the harness tz can't leave the earliest run's attribution outside the window.
    The end is unchanged; ``oldest`` may be a datetime or ISO string (None → no-op).
    Pure and unit-testable.
    """
    if oldest is None:
        return window_iso
    from datetime import datetime, timedelta

    if isinstance(oldest, str):
        oldest = datetime.fromisoformat(oldest)
    start_iso = (oldest - timedelta(hours=_RECONCILE_TZ_BUFFER_HOURS)).isoformat()
    return (start_iso, window_iso[1]) if start_iso < window_iso[0] else window_iso


# ── Per-engine SQL builders (docs-validated; pure, unit-testable) ────────────


def snowflake_reconcile_sql(query_ids: list[str], start_iso: str) -> str:
    """Per-query credits from ``QUERY_ATTRIBUTION_HISTORY`` (6-8h lag).

    Returns (query_id, credits), recorded as ``metadata.attributed_credits``. Server
    time is already captured inline (QUERY_HISTORY), so it is not re-fetched here.

    LIVE-VALIDATED: the ACCOUNT_USAGE imported-privileges grant works and
    rows match; attribution settles 6-8h after the query and lands progressively, so
    a run reconciles partially and re-running later back-fills the tail. ``reconcile``
    auto-widens the lookback to cover the oldest unreconciled row, so back-filling
    days-old runs works without manually bumping ``--window-hours``.
    """
    ids = ", ".join(f"'{q}'" for q in query_ids if q)
    return (
        "SELECT query_id,\n"
        "       COALESCE(credits_attributed_compute, 0)\n"
        "       + COALESCE(credits_used_query_acceleration, 0) AS credits\n"
        "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY\n"
        f"WHERE query_id IN ({ids})\n"
        f"  AND start_time >= '{start_iso}'"
    )


def databricks_reconcile_sql(start_iso: str, end_iso: str, statement_ids: list[str]) -> str:
    """Per-statement exec time, queue and compile time, + DBU-attributed cost.

    Returns (statement_id, execution ms, cost USD, queue ms, compile ms). Queue is
    the two documented waits, for capacity and for compute to start. A statement
    the harness timed out is there too (``execution_status`` CANCELED) once its id
    was captured, which is how a timeout that sat in the warehouse's queue is told
    from one that never reached the warehouse.

    Joins ``system.query.history`` to hourly ``system.billing.usage`` priced by
    ``system.billing.list_prices``, allocating each hour's warehouse spend by the
    statement's share of execution time that hour. The share denominator covers
    **every statement on the warehouse that hour** — not just the benchmark ids —
    so co-resident work (e.g. a validate-sf1 pass sharing the billing hour)
    carries its own share instead of being silently attributed to benchmark rows
    (an id-filtered denominator hands them the whole hour's spend). The
    benchmark-id filter applies only to the output.

    The warehouses are the ones that ran the given statements (``bench``), read
    from the history itself rather than from the connected warehouse. The system
    tables are account-wide per region, so a warehouse in a recreated workspace
    can reconcile statements a deleted workspace's warehouse ran.

    VERIFY LIVE: reconcile principal needs SELECT on ``system.query`` +
    ``system.billing``; ``compute.warehouse_id`` / ``usage_metadata.warehouse_id``
    are structs populated for SQL warehouses; ``sku_name`` should match the
    serverless SKU (e.g. ``*SERVERLESS_SQL*``).
    """
    ids = ", ".join(f"'{s}'" for s in statement_ids)
    window = f"start_time >= '{start_iso}' AND start_time < '{end_iso}'"
    return (
        "WITH bench AS (\n"
        "  SELECT DISTINCT compute.warehouse_id AS wh FROM system.query.history\n"
        f"  WHERE {window} AND statement_id IN ({ids})\n"
        "),\n"
        "hist AS (\n"
        "  SELECT statement_id, execution_duration_ms AS dur_ms,\n"
        "         COALESCE(waiting_at_capacity_duration_ms, 0)\n"
        "         + COALESCE(waiting_for_compute_duration_ms, 0) AS queue_ms,\n"
        "         compilation_duration_ms AS compile_ms,\n"
        "         compute.warehouse_id AS wh, date_trunc('HOUR', start_time) AS hr\n"
        "  FROM system.query.history\n"
        f"  WHERE {window}\n"
        "    AND compute.warehouse_id IN (SELECT wh FROM bench)\n"
        "),\n"
        "share AS (SELECT *, dur_ms / SUM(dur_ms) OVER (PARTITION BY wh, hr) AS frac FROM hist),\n"
        "priced AS (\n"
        "  SELECT u.usage_metadata.warehouse_id AS wh,\n"
        "         date_trunc('HOUR', u.usage_start_time) AS hr,\n"
        "         SUM(u.usage_quantity * p.pricing.default) AS hour_usd\n"
        "  FROM system.billing.usage u\n"
        "  JOIN system.billing.list_prices p ON u.sku_name = p.sku_name\n"
        "   AND u.usage_start_time >= p.price_start_time\n"
        "   AND u.usage_start_time < coalesce(p.price_end_time, now())\n"
        f"  WHERE u.usage_start_time >= '{start_iso}' AND u.usage_start_time < '{end_iso}'\n"
        "    AND u.usage_metadata.warehouse_id IN (SELECT wh FROM bench)\n"
        "  GROUP BY 1, 2\n"
        ")\n"
        "SELECT s.statement_id, s.dur_ms, s.frac * pr.hour_usd AS cost_usd,\n"
        "       s.queue_ms, s.compile_ms\n"
        "FROM share s LEFT JOIN priced pr ON s.wh = pr.wh AND s.hr = pr.hr\n"
        f"WHERE s.statement_id IN ({ids})"
    )


def redshift_server_time_sql(query_ids: list[int]) -> str:
    """Per-query server, queue and compile time (µs->ms) from ``SYS_QUERY_HISTORY``.

    Owner-visible. Returns (query_id, execution ms, queue ms, compile ms).
    """
    ids = ", ".join(str(int(q)) for q in query_ids)
    return (
        "SELECT query_id, execution_time / 1000.0 AS server_time_ms,\n"
        "       queue_time / 1000.0 AS queue_ms, compile_time / 1000.0 AS compile_ms\n"
        f"FROM SYS_QUERY_HISTORY WHERE query_id IN ({ids})"
    )


def redshift_cost_sql(query_ids: list[int], start_iso: str, end_iso: str) -> str:
    """Time-weighted RPU-seconds from ``SYS_SERVERLESS_USAGE`` (**superuser-only**).

    ``SYS_SERVERLESS_USAGE`` has no query id — it's per-minute account usage — so
    ``charged_seconds`` (RPU-seconds) is split across queries by their overlap
    share of each usage minute. The share denominator covers **every query in the
    window** — not just the benchmark ids — so co-resident work carries its own
    share instead of being attributed to benchmark rows (the same share-denominator
    hazard as the Databricks allocator). The benchmark-id
    filter applies only to the output. Returns (query_id, rpu_hours).

    VERIFY LIVE: superuser / ``SYSLOG ACCESS UNRESTRICTED``; 7-day retention;
    concurrency inflates per-minute buckets.
    """
    ids = ", ".join(str(int(q)) for q in query_ids)
    return (
        "WITH q AS (\n"
        "  SELECT query_id, start_time, end_time\n"
        "  FROM SYS_QUERY_HISTORY\n"
        f"  WHERE end_time > '{start_iso}' AND start_time < '{end_iso}'\n"
        "),\n"
        "m AS (SELECT start_time AS m0, end_time AS m1, charged_seconds\n"
        "      FROM SYS_SERVERLESS_USAGE WHERE charged_seconds > 0),\n"
        "ov AS (\n"
        "  SELECT q.query_id, m.charged_seconds, m.m0, m.m1,\n"
        "    EXTRACT(EPOCH FROM (LEAST(q.end_time, m.m1) - GREATEST(q.start_time, m.m0))) AS ov_s\n"
        "  FROM q JOIN m ON q.start_time < m.m1 AND q.end_time > m.m0\n"
        "),\n"
        "tot AS (SELECT m0, m1, SUM(ov_s) AS bucket_total FROM ov GROUP BY m0, m1)\n"
        "SELECT ov.query_id,\n"
        "       SUM(ov.charged_seconds * ov.ov_s / NULLIF(tot.bucket_total, 0)) / 3600.0 AS rpu_hours\n"
        "FROM ov JOIN tot ON ov.m0 = tot.m0 AND ov.m1 = tot.m1\n"
        f"WHERE ov.query_id IN ({ids})\n"
        "GROUP BY ov.query_id"
    )


def _tsql_utc(iso: str) -> str:
    """An ISO timestamp as a naive ``YYYY-MM-DD HH:MM:SS`` literal, in UTC when it
    carries an offset (T-SQL's ``datetime2`` rejects the offset)."""
    from datetime import UTC, datetime

    ts = datetime.fromisoformat(iso)
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC).replace(tzinfo=None)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def fabric_reconcile_sql(start_iso: str) -> str:
    """Our requests in ``queryinsights.exec_requests_history`` since ``start_iso``.

    Returns (label, elapsed ms, allocated CPU ms, start, end) per ``OPTION (LABEL)``
    hint the connector attaches. The elapsed time is the row's server time; the CPU
    time is what the capacity window's cost is split by (see
    :func:`benchmark.analysis.recost.recost_fabric_windows`). Grouped by label, so a
    request recorded twice cannot double a row's CPU share.

    ``start_iso`` is the reconcile's lookback start, already widened to the oldest
    candidate row (a fixed 24-hour lookback misses rows reconciled days later); a
    timestamp with an offset is converted to UTC, the view's zone. A naive
    one is the harness's local time, which the widening already pads by a day.

    This reads the dedicated ``label`` column — never ``command``: Fabric strips a
    leading block comment before storing ``command``, so a label embedded there can
    never match (see ``connectors.fabric.execute_query``). It also must not filter
    ``login_name = USER_NAME()``: a ``bmq_`` label is ours by construction, and
    ``USER_NAME()`` resolves to the workspace user while a service-principal
    connection logs under its own GUID.

    Live-validated: the label appears within ~1 min.
    """
    return (
        "SELECT label, MAX(total_elapsed_time_ms), SUM(allocated_cpu_time_ms),\n"
        "       MIN(start_time), MAX(end_time)\n"
        "FROM queryinsights.exec_requests_history\n"
        f"WHERE start_time >= '{_tsql_utc(start_iso)}'\n"
        "  AND label LIKE 'bmq!_%' ESCAPE '!'\n"
        "GROUP BY label"
    )


# ── Per-engine reconcilers (execute + parse; all five live-validated) ─────────


def _reconcile_snowflake(
    connector: WarehouseConnector,
    pqids: list[str],
    *,
    window_iso: tuple[str, str],
    rates: dict,
    collector: CostCollector | None = None,
) -> list[ReconciledMetric]:
    res = connector.execute_query(snowflake_reconcile_sql(pqids, window_iso[0]))
    if res.error_message:
        logger.warning("snowflake attribution reconcile failed: %s", res.error_message)
        return []
    out = []
    for qid, credits in res.rows:
        if credits is None:
            continue
        # A cross-check only: the cost stays list rate x engine time, so no cost (and
        # no tier) is written.
        out.append(
            ReconciledMetric(str(qid), None, None, metadata={"attributed_credits": float(credits)})
        )
    return out


def _reconcile_databricks(
    connector: WarehouseConnector,
    pqids: list[str],
    *,
    window_iso: tuple[str, str],
    rates: dict,
    collector: CostCollector | None = None,
) -> list[ReconciledMetric]:
    out = []
    for start in range(0, len(pqids), DATABRICKS_RECONCILE_BATCH):
        batch = pqids[start : start + DATABRICKS_RECONCILE_BATCH]
        res = connector.execute_query(databricks_reconcile_sql(window_iso[0], window_iso[1], batch))
        if res.error_message:
            logger.warning(
                "databricks reconcile failed for statements %d-%d: %s",
                start,
                start + len(batch) - 1,
                res.error_message,
            )
            continue
        for sid, dur_ms, cost, *waits in res.rows:
            queue_ms, compile_ms = [*waits, None, None][:2]
            billed = float(cost) if cost is not None else None
            out.append(
                ReconciledMetric(
                    str(sid),
                    _ms(dur_ms),
                    billed,
                    # The hour re-split (recost.resplit_databricks_hours) starts from it.
                    metadata={"billed_share_usd": billed} if billed is not None else None,
                    queue_ms=_ms(queue_ms),
                    compile_ms=_ms(compile_ms),
                )
            )
    return out


def _reconcile_redshift(
    connector: WarehouseConnector,
    pqids: list[str],
    *,
    window_iso: tuple[str, str],
    rates: dict,
    collector: CostCollector | None = None,
) -> list[ReconciledMetric]:
    rate = rates.get("redshift_rpu_usd_per_hour") or getattr(collector, "rpu_price_usd", None)
    qids = [int(p) for p in pqids if str(p).isdigit()]
    if not qids:
        return []

    # Server time — query-owner visible; always attempted.
    server_ms: dict[str, float | None] = {}
    waits: dict[str, tuple[float | None, float | None]] = {}
    st = connector.execute_query(redshift_server_time_sql(qids))
    if st.error_message:
        logger.warning("redshift server-time reconcile failed: %s", st.error_message)
    else:
        for qid, ms, *rest in st.rows:
            queue_ms, compile_ms = [*rest, None, None][:2]
            server_ms[str(qid)] = _ms(ms)
            waits[str(qid)] = (_ms(queue_ms), _ms(compile_ms))

    # Cost — SYS_SERVERLESS_USAGE is superuser-only; degrade gracefully so a
    # non-superuser still back-fills authoritative server time.
    cost_by_id: dict[str, float | None] = {}
    cres = connector.execute_query(redshift_cost_sql(qids, window_iso[0], window_iso[1]))
    if cres.error_message:
        logger.warning(
            "redshift cost reconcile unavailable (SYS_SERVERLESS_USAGE needs a "
            "superuser / SYSLOG ACCESS UNRESTRICTED role); server time still "
            "back-filled. detail: %s",
            cres.error_message,
        )
    else:
        for qid, rpu_hours in cres.rows:
            cost_by_id[str(qid)] = (
                float(rpu_hours) * float(rate) if (rpu_hours is not None and rate) else None
            )

    out = []
    for qid in {str(q) for q in qids}:
        s, c = server_ms.get(qid), cost_by_id.get(qid)
        if s is None and c is None:
            continue
        queue_ms, compile_ms = waits.get(qid, (None, None))
        # The idle cap (recost.cap_redshift_idle) starts from the billed share.
        meta = {"billed_share_usd": c} if c is not None else None
        out.append(
            ReconciledMetric(qid, s, c, metadata=meta, queue_ms=queue_ms, compile_ms=compile_ms)
        )
    return out


def _reconcile_fabric(
    connector: WarehouseConnector,
    pqids: list[str],
    *,
    window_iso: tuple[str, str],
    rates: dict,
    collector: CostCollector | None = None,
) -> list[ReconciledMetric]:
    """Fabric: server time plus the CPU time and span each statement used.

    Prices nothing itself. A statement's cost depends on every statement that
    shared the capacity with it, so ``reconcile_platform`` re-prices the cell's
    windows once all of them are back-filled
    (:func:`benchmark.analysis.recost.recost_fabric_windows`). ``cpu_ms``,
    ``start_time`` and ``end_time`` (UTC) land in the row's metadata.
    """
    res = connector.execute_query(fabric_reconcile_sql(window_iso[0]))
    if res.error_message:
        logger.warning("fabric reconcile failed: %s", res.error_message)
        return []
    # Keyed match on the label column — no substring scan, so a query whose text
    # happens to contain another query's label cannot be mis-attributed.
    wanted = set(pqids)
    out = []
    for label, elapsed_ms, cpu_ms, start, end in res.rows:
        if label not in wanted:
            continue
        meta = {
            key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in (("cpu_ms", _ms(cpu_ms)), ("start_time", start), ("end_time", end))
            if value is not None
        }
        out.append(ReconciledMetric(label, _ms(elapsed_ms), None, metadata=meta or None))
    return out


def _reconcile_motherduck(
    connector: WarehouseConnector,
    pqids: list[str],
    *,
    window_iso: tuple[str, str],
    rates: dict,
    collector: CostCollector | None = None,
) -> list[ReconciledMetric]:
    """Batch server-side timing from MotherDuck ``MD_QUERY_HISTORY()``.

    The connector captures only the query id inline, and this back-fills timing for
    the whole run in one shot from the durable ``MD_QUERY_HISTORY()`` (see
    ``MotherDuckConnector.lookup_server_times``) — its 1y+ retention makes post-run
    batch reconcile reliable at any scale. Cost stays estimated (wall-clock tier
    model, ``cost/motherduck.py``); ``start``/``end`` ride along in metadata for the
    suite cost (``analysis/motherduck_cost.py``)."""
    lookup = getattr(connector, "lookup_server_times", None)
    if lookup is None:
        logger.warning("motherduck connector has no lookup_server_times; skipping")
        return []
    settle = float(rates.get("motherduck_settle_seconds", 10.0))
    timings = lookup(pqids, settle_seconds=settle)
    out = []
    for qid, t in timings.items():
        meta = {k: t[k] for k in ("start_time", "end_time") if t.get(k) is not None} or None
        out.append(
            ReconciledMetric(
                str(qid),
                t.get("server_time_ms"),
                None,
                metadata=meta,
                bytes_scanned=t.get("bytes_scanned"),
                bytes_spilled=t.get("bytes_spilled"),
                queue_ms=t.get("queue_ms"),
            )
        )
    return out


#: Job ids per BigQuery history lookup: a query's text is capped at 1 MB, and a
#: concurrency grid has tens of thousands of ids.
BIGQUERY_IDS_PER_LOOKUP = 5000


def bigquery_queue_sql(job_ids: list[str], region: str, start_iso: str) -> str:
    """Per-job queue time from ``INFORMATION_SCHEMA.JOBS_BY_PROJECT`` (180-day history).

    A job waiting in BigQuery's query queue is PENDING from ``creation_time`` to
    ``start_time``, and Google documents the queue time as their difference. The
    connector's ``server_time_ms`` is ``end_time - start_time``, so the queue is
    not in it. Filtering on ``creation_time`` prunes the view's partitions.
    Returns (job_id, queue ms).
    """
    ids = ", ".join(f"'{j}'" for j in job_ids if j)
    start = start_iso.replace("T", " ")
    return (
        "SELECT job_id, TIMESTAMP_DIFF(start_time, creation_time, MILLISECOND) AS queue_ms\n"
        f"FROM `region-{region.lower()}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT\n"
        f"WHERE creation_time >= TIMESTAMP('{start}') AND job_id IN ({ids})"
    )


def _reconcile_bigquery(
    connector: WarehouseConnector,
    pqids: list[str],
    *,
    window_iso: tuple[str, str],
    rates: dict,
    collector: CostCollector | None = None,
) -> list[ReconciledMetric]:
    """Back-fill queue time only; server time and cost are captured inline."""
    region = str(getattr(connector, "region", "") or "")
    if not region:
        logger.warning("bigquery reconcile needs the connector's region; skipping")
        return []
    out = []
    for i in range(0, len(pqids), BIGQUERY_IDS_PER_LOOKUP):
        chunk = pqids[i : i + BIGQUERY_IDS_PER_LOOKUP]
        res = connector.execute_query(bigquery_queue_sql(chunk, region, window_iso[0]))
        if res.error_message:
            logger.warning("bigquery queue reconcile failed: %s", res.error_message)
            continue
        for job_id, queue_ms in res.rows:
            if queue_ms is not None:
                out.append(
                    ReconciledMetric(str(job_id), None, None, queue_ms=max(float(queue_ms), 0.0))
                )
    return out


RECONCILERS = {
    "bigquery": _reconcile_bigquery,
    "snowflake": _reconcile_snowflake,
    "databricks": _reconcile_databricks,
    "redshift": _reconcile_redshift,
    "fabric": _reconcile_fabric,
    "motherduck": _reconcile_motherduck,
}


def reconcile_platform(
    platform: str,
    connector: WarehouseConnector,
    results_db: ResultsDB,
    *,
    scale_factor: int,
    run_type: str = "warm",
    window_iso: tuple[str, str],
    rates: dict | None = None,
) -> dict:
    """Re-query ``platform``'s history/billing and update recorded rows.

    Returns a small summary dict. Engines with immediate metrics have no
    reconciler and are reported as skipped (not an error).
    """
    reconciler = RECONCILERS.get(platform)
    if reconciler is None:
        logger.info("no reconcile for %s (immediate metrics)", platform)
        return {"platform": platform, "updated": 0, "skipped": "immediate-metrics"}

    needs = {
        "missing_queue": platform in QUEUE_RECONCILERS,
        "missing_cpu": platform in CPU_RECONCILERS,
    }
    pqids = results_db.rows_to_reconcile(platform, scale_factor, run_type, **needs)
    if not pqids:
        logger.info("%s: nothing to reconcile at SF%d", platform, scale_factor)
        return {"platform": platform, "updated": 0}

    # Auto-widen the history lookback to cover the OLDEST row we're back-filling.
    # A fixed --window-hours silently drops runs older than the window (their
    # engine-side attribution/billing then never lands), undercounting cost. We
    # extend the window start back to just before the earliest candidate row so
    # reconcile is correct-by-default; --window-hours stays the *minimum* lookback.
    oldest = results_db.oldest_reconcile_candidate(platform, scale_factor, run_type, **needs)
    widened = _widen_lookback(window_iso, oldest)
    if widened != window_iso:
        logger.info(
            "%s: widening reconcile lookback to %s to cover runs from %s "
            "(--window-hours start was %s)",
            platform,
            widened[0],
            oldest,
            window_iso[0],
        )
        window_iso = widened

    confidence = RECONCILED_CONFIDENCE.get(platform, CostConfidence.DIRECT)
    try:
        from ..cost.registry import create_cost_collector

        collector = create_cost_collector(platform)
    except Exception:
        logger.warning(
            "could not build cost collector for %s; cost may stay estimated",
            platform,
            exc_info=True,
        )
        collector = None
    metrics = reconciler(
        connector, pqids, window_iso=window_iso, rates=rates or {}, collector=collector
    )
    updated = 0
    for m in metrics:
        # Only stamp the authoritative tier when this reconcile actually produced
        # a cost; a server-time-only back-fill leaves the (estimated) cost as-is.
        row_confidence = confidence if m.cost_usd is not None else None
        results_db.update_reconciled_metrics(
            m.platform_query_id,
            server_time_ms=m.server_time_ms,
            cost_usd=m.cost_usd,
            cost_confidence=row_confidence,
            bytes_scanned=m.bytes_scanned,
            bytes_spilled=m.bytes_spilled,
            compile_ms=m.compile_ms,
            queue_ms=m.queue_ms,
            metadata_extra=m.metadata,
        )
        updated += 1
    logger.info("%s: reconciled %d/%d queries", platform, updated, len(pqids))
    summary: dict = {"platform": platform, "updated": updated, "candidates": len(pqids)}
    if platform == "fabric":
        # A Fabric row's price depends on every row that shared its capacity window,
        # so the cell is re-priced once its rows are back-filled (windows never span
        # a scale or a run type).
        from .recost import recost_fabric_windows

        priced = recost_fabric_windows(results_db, scale_factor=scale_factor, run_type=run_type)
        summary["priced_usd"] = priced["total_after_usd"]
    elif platform == "redshift" and updated:
        # Billed RPU-seconds include the idle seconds between the harness's queries;
        # cap every window at RPU rate x the time it held the workgroup.
        from .recost import cap_redshift_idle

        cap_redshift_idle(results_db)
    elif platform == "databricks" and updated:
        # A billing hour holds rows of several cells, so the whole engine is re-split
        # (offline, idempotent) once this cell's billed shares are in.
        from .recost import resplit_databricks_hours

        resplit_databricks_hours(results_db)
    return summary

"""SQL benchmark runner — warm-run executor with atomic recording.

CLAUDE.md §Orchestration Flow defines the contract:
  - 1 warmup + 3 measured warm runs per query (warmup discarded)
  - each measured run is recorded atomically to `sql_benchmark_results`
  - errors do not abort the batch; they record `error_message` and continue
  - resumable: completed (platform, query_id, run_number) tuples are skipped
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable

from ..connectors.base import (
    QueryResult,
    WarehouseConnector,
    compute_label_from_environment,
)
from ..connectors.connection import reconnect_in_place
from ..cost.base import CostCollector
from ..exceptions import UnlabelledComputeError
from ..queries.registry import BenchmarkQuery, QueryRegistry
from ..results import ResultsDB
from ..terraform_outputs import provisioned_compute

logger = logging.getLogger("benchmark.sql_benchmark.runner")

# Global per-query timeout policy (CLAUDE.md §Global Query Timeout).
DEFAULT_QUERY_TIMEOUT_S = 600  # 10 minutes
SF1000_QUERY_TIMEOUT_S = 1800  # 30-minute exception at SF1000+ for known-slow queries
# Query ids that legitimately approach 10 min and get the 30-min exception at
# scale >= 1000. Empty: every query runs under the 10-minute default.
SF1000_TIMEOUT_EXCEPTIONS: frozenset[str] = frozenset()

#: Error recorded for a query not sent because its session was lost: a reconnect
#: after an earlier timeout failed, and so did the retry before this query. The
#: analysis counts these as the harness's failures, not the engine's
#: (``analysis.concurrency_shape.HARNESS_ERROR_PATTERNS``).
SESSION_LOST_MESSAGE = "not run: session lost (reconnect after an earlier timeout failed)"

#: Seconds a timed-out query gets to return after its statement is cancelled, before
#: its session is set aside unclosed rather than closed under it (``execute_with_timeout``;
#: only for connectors that can cancel from another thread). pyodbc's cancel returns
#: within a second or two.
ABANDON_GRACE_S = 10


def resolve_query_timeout_s(scale_factor: int, query_id: str) -> int:
    """Per-query wall-clock timeout: 10 min default, 30 min for SF1000+ exceptions."""
    if scale_factor >= 1000 and query_id in SF1000_TIMEOUT_EXCEPTIONS:
        return SF1000_QUERY_TIMEOUT_S
    return DEFAULT_QUERY_TIMEOUT_S


def execute_with_timeout(
    connector: WarehouseConnector,
    sql: str,
    timeout_s: int,
    *,
    platform: str,
    query_id: str,
) -> QueryResult:
    """Run ``connector.execute_query`` under a hard wall-clock timeout.

    Guarantees the batch never hangs on a single query (CLAUDE.md §Global Query
    Timeout). On timeout the connector is reconnected to drop the in-flight
    query/connection — closing the connection is a best-effort server-side cancel
    on most engines — and a timeout-failure ``QueryResult`` is returned so the
    caller records it like any other error. Other exceptions propagate.

    The timed-out row carries the engine's id for the statement when the
    connector can report it (``inflight_query_id``), so ``reconcile`` can tell a
    statement that waited in the engine's queue from one that never reached it.

    The reconnect is retried (``reconnect_in_place``). When every attempt fails
    the connector is marked ``connection_lost`` and the timeout result carries
    ``metadata["connection_lost"]``; a later call on that connector tries to
    reconnect first and, failing that, returns a ``SESSION_LOST_MESSAGE`` error
    without sending the query. Without the retry, a single failed reconnect would
    leave the connector closed and every later query on it failing in 0 ms with
    "connector not connected", an error that is the harness's, not the engine's.

    The query runs on a daemon thread. An abandoned query can keep its thread
    blocked in the driver long after the timeout, and Python joins non-daemon
    threads at exit: a ``ThreadPoolExecutor`` worker stuck on a timed-out query
    can hang the process at exit for hours after its last row is written. A
    daemon thread is dropped at exit.

    On timeout, a connector that can cancel from another thread (``cancel_running``,
    Fabric's pyodbc) has its running statement cancelled, and the abandoned call gets
    :data:`ABANDON_GRACE_S` to return. Only then is its session closed; if it is
    still inside the driver, the session is set aside unclosed
    (``abandon_session``) and a new one opened. Other connectors close at once, as
    their Python drivers raise in the abandoned thread rather than crash. Closing a
    pyodbc connection under a running ``execute`` reads freed memory and can kill
    the process with a segmentation fault.

    Note: this is the engine-agnostic enforcement layer. Native per-engine
    statement timeouts (Snowflake STATEMENT_TIMEOUT_IN_SECONDS, ClickHouse
    max_execution_time, Redshift statement_timeout, pyodbc cursor.timeout, etc.)
    are not set; each would need live validation per engine.
    """
    if getattr(connector, "connection_lost", False) and not reconnect_in_place(connector):
        return QueryResult(
            rows=[],
            columns=[],
            row_count=0,
            wall_clock_ms=0.0,
            error_message=SESSION_LOST_MESSAGE,
            metadata={"connection_lost": True},
        )
    done = threading.Event()
    outcome: dict[str, QueryResult | Exception] = {}

    def _run() -> None:
        try:
            outcome["result"] = connector.execute_query(sql)
        except Exception as exc:  # re-raised on the caller's thread below
            outcome["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_run, name=f"query-{platform}-{query_id}", daemon=True).start()
    if done.wait(timeout_s):
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]
    logger.error(
        "query timeout after %ds; abandoning and reconnecting",
        timeout_s,
        extra={"platform": platform, "query_id": query_id},
    )
    # Read before the disconnect below closes the cursor that holds it.
    inflight = getattr(connector, "inflight_query_id", None)
    inflight_id = inflight() if callable(inflight) else None
    # Stop the statement and let the abandoned call return before its session is
    # closed: closing a C driver's connection (pyodbc) under a running call can
    # segfault the process. If it has not returned within the grace, the session is
    # set aside unclosed (``abandon_session``) instead.
    cancel_running = getattr(connector, "cancel_running", None)
    can_cancel = callable(cancel_running) and (
        getattr(cancel_running, "__func__", None) is not WarehouseConnector.cancel_running
    )
    returned = False
    if can_cancel:
        try:
            cancel_running()
        except Exception:
            logger.warning("cancel after timeout failed", exc_info=True)
        returned = done.wait(ABANDON_GRACE_S)
    abandon = getattr(connector, "abandon_session", None)
    try:
        if returned or not can_cancel or not callable(abandon):
            connector.disconnect()
        else:
            logger.warning(
                "timed-out query still in the driver after %ds; setting its session aside",
                ABANDON_GRACE_S,
                extra={"platform": platform, "query_id": query_id},
            )
            abandon()
    except Exception:
        logger.warning("disconnect after timeout failed", exc_info=True)
    reconnected = reconnect_in_place(connector)
    # Server-side cancel where the connector supports it (e.g. ClickHouse's
    # KILL QUERY by client-generated query_id): closing the connection does
    # NOT stop the query on every engine, and an abandoned "zombie" keeps
    # burning compute under all later measurements.
    cancel = getattr(connector, "cancel_inflight", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:
            logger.warning("server-side cancel after timeout failed", exc_info=True)
    return QueryResult(
        rows=[],
        columns=[],
        row_count=0,
        wall_clock_ms=float(timeout_s * 1000),
        error_message=f"query exceeded timeout of {timeout_s}s",
        platform_query_id=inflight_id,
        metadata={} if reconnected else {"connection_lost": True},
    )


def _looks_unordered(sql: str) -> bool:
    """Heuristic: queries without ORDER BY may legitimately return rows in
    any order. The set/multiset comparison lives in validator.py; the runner
    only flags the query in metadata.
    """
    return "ORDER BY" not in sql.upper()


def execute_query_runs(
    query: BenchmarkQuery,
    connector: WarehouseConnector,
    cost_collector: CostCollector,
    scale_factor: int,
    results_db: ResultsDB,
    platform: str,
    *,
    num_warmups: int = 1,
    num_measured: int = 3,
    run_type: str = "warm",
    timeout_s: int = DEFAULT_QUERY_TIMEOUT_S,
    completed_runs: frozenset[int] = frozenset(),
    provenance_id: str | None = None,
    compute_label: str | None = None,
) -> None:
    """Execute warmup + measured runs of one query, recording each measured run.

    ``completed_runs`` holds measured run_numbers already recorded for this query;
    they are skipped so a partial-crash resume fills only the gaps rather than
    re-executing the whole query (which would double-charge and append duplicate
    rows).

    Per-query timeout circuit breaker: once any run of this query times out, the
    remaining runs are NOT executed — each is recorded as an error row with a
    ``skipped_after_timeout`` marker. A warm-run timeout is deterministic (same
    query, same data, same pinned compute), so re-burning the full timeout budget
    per run (4 × up to 30 min at SF1000) buys no information. The skipped rows
    are still errors, so a later re-invocation retries them like any failure.
    """
    timed_out = False
    for run_num in range(num_warmups + num_measured):
        is_warmup = run_num < num_warmups
        measured_num = None if is_warmup else run_num - num_warmups + 1

        # Per-run resume: skip measured runs already recorded for this query.
        if measured_num is not None and measured_num in completed_runs:
            logger.debug(
                "skipping already-recorded measured run",
                extra={"platform": platform, "query_id": query.id, "run_number": measured_num},
            )
            continue

        label = "warmup" if is_warmup else f"measured#{measured_num}"

        if timed_out:
            result = QueryResult(
                rows=[],
                columns=[],
                row_count=0,
                wall_clock_ms=0.0,
                error_message=(
                    f"skipped: earlier run of this query exceeded timeout of {timeout_s}s"
                ),
                metadata={"skipped_after_timeout": True},
            )
        else:
            result = execute_with_timeout(
                connector, query.sql, timeout_s, platform=platform, query_id=query.id
            )
            if result.error_message and result.error_message.startswith("query exceeded timeout"):
                timed_out = True
                logger.warning(
                    "query timed out; skipping its remaining runs",
                    extra={"platform": platform, "query_id": query.id, "run_number": run_num},
                )

        if is_warmup:
            if result.error_message:
                logger.warning(
                    "warmup failed; will still attempt measured runs",
                    extra={
                        "platform": platform,
                        "query_id": query.id,
                        "run_number": run_num,
                    },
                )
            else:
                logger.debug(
                    "warmup ok",
                    extra={
                        "platform": platform,
                        "query_id": query.id,
                        "run_number": run_num,
                        "wall_clock_ms": result.wall_clock_ms,
                    },
                )
            continue

        # Measured run: cost + atomic write. Correctness is NOT judged here —
        # validate-sf1 owns it. The runner records latency/cost only.
        cost_usd = cost_collector.cost_from_result(result)

        metadata = dict(result.metadata) if result.metadata else {}
        if _looks_unordered(query.sql):
            metadata["unordered_query"] = True

        results_db.record_sql_result(
            platform=platform,
            query_id=query.id,
            block=query.block,
            scale_factor=scale_factor,
            run_type=run_type,
            run_number=measured_num,
            wall_clock_ms=result.wall_clock_ms,
            server_time_ms=result.server_time_ms,
            bytes_scanned=result.bytes_scanned,
            bytes_spilled=result.bytes_spilled,
            compile_ms=result.compile_ms,
            queue_ms=result.queue_ms,
            cache_hit_pct=result.cache_hit_pct,
            platform_query_id=result.platform_query_id,
            provenance_id=provenance_id,
            compute_label=compute_label,
            row_count=result.row_count if result.error_message is None else None,
            cost_usd=cost_usd,
            cost_confidence=cost_collector.confidence,
            error_message=result.error_message,
            metadata=metadata,
        )

        logger.info(
            "%s %s",
            query.id,
            label,
            extra={
                "platform": platform,
                "query_id": query.id,
                "block": query.block,
                "scale_factor": scale_factor,
                "run_type": run_type,
                "run_number": measured_num,
                "wall_clock_ms": result.wall_clock_ms,
                "row_count": result.row_count,
                "cost_usd": cost_usd,
            },
        )


def _normalize_warehouse_size(value: object) -> str:
    """Canonicalize a warehouse-size label for comparison: 'X-Small', 'XSMALL',
    'x_small' all collapse to 'XSMALL'."""
    return str(value).upper().replace("-", "").replace("_", "").replace(" ", "")


# Per-engine (actual_key, provisioned_key, kind) for the comparable compute
# dimension. Fabric + ClickHouse compare cloud-API values (capacity SKU / replica
# count) against terraform-declared expectations; the rest use SQL / SDK lookups.
_COMPUTE_CHECKS: dict[str, tuple[str, str, str]] = {
    "snowflake": ("size", "warehouse_size", "label"),
    "databricks": ("cluster_size", "warehouse_size", "label"),
    "motherduck": ("size", "tier", "label"),
    "bigquery": ("slot_capacity", "reservation_slots", "int"),
    "redshift": ("base_capacity", "baseline_rpu", "int"),
    "fabric": ("capacity_sku", "capacity_sku", "label"),
    "clickhouse": ("num_replicas", "num_replicas", "int"),
}


def _pin_violation(platform: str, actual: dict) -> str | None:
    """Whether the engine was left free to resize itself mid-pass.

    A benchmark measures a *fixed* configuration; an engine that can scale during a
    pass makes its own rows uninterpretable, and the label still claims one size:
    an autoscaling warehouse under a fixed size label can leave thousands of rows
    attributable to no size.

    ClickHouse is checked here because ``_COMPUTE_CHECKS`` compares only
    ``num_replicas``: a service whose replica memory has ``min != max`` would pass
    that check while free to resize. Its bounds come from the Cloud API.

    Returns a description of the violation, or None when pinned (or unknowable).
    """
    if platform != "clickhouse":
        return None
    lo = actual.get("min_replica_memory_gb")
    hi = actual.get("max_replica_memory_gb")
    if lo is None or hi is None:
        return None  # API creds absent — unknowable, not a violation
    if lo != hi:
        return f"replica memory is not pinned: min={lo}GB max={hi}GB (must be equal)"
    return None


def _compute_matches(platform: str, provisioned: dict, actual: dict) -> bool | None:
    """Whether the engine's actual runtime compute matches what terraform
    provisioned for the scale.

    Returns None when not comparable — the engine has no comparable pair
    (ClickHouse, Fabric), or either side lacks the value (no terraform outputs,
    or the runtime query failed).
    """
    spec = _COMPUTE_CHECKS.get(platform)
    if spec is None:
        return None
    actual_key, provisioned_key, kind = spec
    running = actual.get(actual_key)
    expected = provisioned.get(provisioned_key)
    if running is None or expected is None:
        return None
    if kind == "int":
        try:
            return int(running) == int(expected)
        except (TypeError, ValueError):
            return None
    return _normalize_warehouse_size(running) == _normalize_warehouse_size(expected)


def capture_runtime_environment(
    results_db: ResultsDB,
    platform: str,
    scale_factor: int,
    run_type: str,
    connector: WarehouseConnector,
) -> str | None:
    """Record the engine's compute/runtime config for this pass (once per
    platform/scale/run_type) and return the compute-size label for it.

    The returned label (e.g. ``'Small (Gen2)'``) is stamped on every result row so
    the same scale can be run at multiple compute sizes without colliding. Resilient:
    a capture failure is logged and never aborts the benchmark — instrumentation must
    not break the run; returns ``None`` if the environment couldn't be read.
    """
    try:
        env = connector.runtime_environment()
        actual = connector.runtime_compute()  # queried from the engine at runtime
        if actual:
            env["actual"] = actual
        provisioned = provisioned_compute(platform, scale_factor)
        if provisioned:
            # terraform-provisioned compute for this scale (Snowflake's warehouse
            # size is resolved per scale; others are fixed / autoscaling ranges).
            env["provisioned"] = provisioned
        # Validate the running compute against what was provisioned (drift check).
        violation = _pin_violation(platform, actual)
        if violation:
            env["pin_violation"] = violation
            logger.warning(
                "engine is not pinned for this pass — its rows may span sizes: %s",
                violation,
                extra={"platform": platform, "scale_factor": scale_factor},
            )
        matches = _compute_matches(platform, provisioned, actual)
        if matches is not None:
            env["compute_validated"] = matches
            if not matches:
                logger.warning(
                    "runtime compute does not match provisioned compute",
                    extra={
                        "platform": platform,
                        "scale_factor": scale_factor,
                        "provisioned": provisioned.get("warehouse_size"),
                        "actual": actual.get("size"),
                    },
                )
        # Derive the label FIRST so the environment row is keyed by the size it
        # describes. Recording before deriving would collapse a sweep to one row.
        env.setdefault("platform", platform)
        label = compute_label_from_environment(env)
        results_db.record_engine_environment(
            platform, scale_factor, run_type, env, compute_label=label or "-"
        )
        if label is None:
            # Do NOT fall back to the requested size: that records intent as
            # reality. Abort so the operator fixes
            # the capture (credentials, throttling) and re-runs a labelled cell.
            raise UnlabelledComputeError(
                f"{platform} at SF{scale_factor} ({run_type}) produced no compute-size "
                f"label from {env.get('actual') or {}}; refusing to record unlabelled "
                "rows, which the size-keyed analysis cannot see"
            )
        return label
    except UnlabelledComputeError:
        raise
    except Exception:
        logger.warning(
            "failed to capture engine runtime environment",
            extra={"platform": platform, "scale_factor": scale_factor, "run_type": run_type},
            exc_info=True,
        )
        return None


def run_sql_benchmark(
    platform: str,
    connector: WarehouseConnector,
    cost_collector: CostCollector,
    results_db: ResultsDB,
    scale_factor: int,
    *,
    blocks: Iterable[str] = ("tpcds", "dsb", "clickbench"),
    num_warmups: int = 1,
    num_measured: int = 3,
    run_type: str = "warm",
    force: bool = False,
    provenance_id: str | None = None,
    cost_config: dict | None = None,
) -> None:
    """Run the full benchmark suite for one platform at one scale factor.

    When ``force`` is True, the resume check is bypassed and every query is
    re-executed; the new measured runs are appended (latest-wins at report
    time per CLAUDE.md), rather than overwriting the prior rows.
    """
    registry = QueryRegistry(dialect=platform)
    queries = [q for q in registry.all() if q.block in blocks]

    completed = set() if force else results_db.get_completed(platform, scale_factor, run_type)
    logger.info(
        "starting SQL benchmark",
        extra={
            "platform": platform,
            "scale_factor": scale_factor,
            "run_type": run_type,
            "total_queries": len(queries),
            "already_completed_pairs": len(completed),
        },
    )

    with connector:
        compute_label = capture_runtime_environment(
            results_db, platform, scale_factor, run_type, connector
        )
        # Price by the size we actually connected to: if a size-specific cost config
        # exists for this compute_label, upgrade the collector to it (no hand-editing
        # when sweeping sizes).
        if cost_config is not None and compute_label:
            from ..cost.discovery import collector_for

            cost_collector = collector_for(platform, cost_config, compute_label=compute_label)
        for q in queries:
            # Resume: which measured run_numbers are already recorded for this query.
            done_runs = frozenset(rn for (qid, rn) in completed if qid == q.id)
            if len(done_runs) >= num_measured:
                logger.debug("skipping completed query", extra={"query_id": q.id})
                continue

            try:
                execute_query_runs(
                    q,
                    connector,
                    cost_collector,
                    scale_factor=scale_factor,
                    results_db=results_db,
                    platform=platform,
                    num_warmups=num_warmups,
                    num_measured=num_measured,
                    run_type=run_type,
                    timeout_s=resolve_query_timeout_s(scale_factor, q.id),
                    completed_runs=done_runs,
                    provenance_id=provenance_id,
                    compute_label=compute_label,
                )
            except Exception as e:
                logger.error(
                    "unexpected runner exception",
                    extra={"platform": platform, "query_id": q.id},
                    exc_info=True,
                )
                # Record a synthetic error row so we don't lose the failure
                results_db.record_sql_result(
                    platform=platform,
                    query_id=q.id,
                    block=q.block,
                    scale_factor=scale_factor,
                    run_type=run_type,
                    run_number=0,
                    wall_clock_ms=None,
                    server_time_ms=None,
                    row_count=None,
                    cost_usd=None,
                    cost_confidence=cost_collector.confidence,
                    error_message=f"runner exception: {e}",
                    provenance_id=provenance_id,
                    compute_label=compute_label,
                )

    logger.info(
        "SQL benchmark complete",
        extra={"platform": platform, "scale_factor": scale_factor, "run_type": run_type},
    )

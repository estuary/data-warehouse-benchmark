"""Concurrency pass.

Runs N concurrent queries against the engine across a series of concurrency
levels (1, 5, 10, 20, 50, 100 by default). The harness records wall-clock +
cost + the concurrency level (which is also persisted to
``sql_benchmark_results.concurrency``).

Connection model — **pre-warm + barrier**. To offer the warehouse a genuine
level-wide load you need ``level`` independent sessions (a single session
submits queries serially, so it can't produce concurrency). Rather than let
each worker connect lazily on its first task — which (a) dribbles the burst in
as connects complete so short SF1 queries never actually overlap, and (b) fires
``level`` TLS handshakes in a ~2s storm that trips local connection-rate limits
(router/VPN/OS → ``OSError`` Errno 51 "Network is unreachable") — each level:

1. opens its ``level`` sessions ONE AT A TIME with a small stagger (the slow
   ramp: N handshakes spread over ~N×stagger s, gentle enough to stay under the
   local limit; retried per connection);
2. holds them idle at a ``threading.Barrier``;
3. releases the barrier so every worker fires its first query simultaneously —
   a true level-wide burst — then drains a shared work queue.

Connect happens entirely in the pre-warm phase, so the per-query timer measures
execution only (a lazy connect would time the first query's connect into its
latency). A session that can't be opened after retries is skipped and the level
runs at the achieved concurrency (continue-on-failure) rather than aborting.

A session lost mid-level (a timeout, then a reconnect that fails; see
``runner.execute_with_timeout``) leaves the level: its worker stops taking work
and the open sessions drain the queue. If every session is lost, the queries
still queued are recorded as not run (``SESSION_LOST_MESSAGE``).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable

from ..connectors.base import WarehouseConnector
from ..connectors.connection import connect_with_retry
from ..connectors.registry import create_connector
from ..cost.base import CostCollector
from ..cost.discovery import collector_for
from ..exceptions import ConnectorError
from ..queries.registry import BenchmarkQuery, QueryRegistry
from ..results import SUSTAINED_RUN_TYPE, ResultsDB
from .runner import SESSION_LOST_MESSAGE, execute_with_timeout, resolve_query_timeout_s

#: Connect attempts for the pre-pass compute-label probe. The default four
#: (about 40 s of backoff) is right for a blip, but a Fabric capacity that is
#: throttling after a 100-session burst refuses logins for minutes, and a probe
#: that gives up aborts a whole block with zero rows. Six attempts back off
#: to roughly eleven minutes, which outlasts the throttle window.
PROBE_CONNECT_ATTEMPTS = 6

logger = logging.getLogger("benchmark.sql_benchmark.concurrency")


# Snowflake warehouse-size tier → the display name SHOW WAREHOUSES reports (and
# thus the warm runner's compute_label). Used as a deterministic fallback so a
# probe-connect blip during a band sweep never leaves rows unlabeled. Generation
# is Gen2 (terraform provisions generation="2"), matching the warm-path label.
_SF_SIZE_DISPLAY = {
    "XSMALL": "X-Small",
    "SMALL": "Small",
    "MEDIUM": "Medium",
    "LARGE": "Large",
    "XLARGE": "X-Large",
}

# Spacing between successive pre-warm connects (the slow ramp). ``level`` new TLS
# handshakes are issued one at a time this far apart, so setup spreads over
# ~level×stagger seconds instead of bursting all at once. A burst of ~100
# simultaneous handshakes trips local connection-rate limits (Errno 51); a gentle
# ramp does not. Sessions sit idle until the barrier releases, so peak in-flight
# concurrency is unaffected — only the setup ramp is slowed.
_CONNECT_STAGGER_S = 0.15

# Safety cap so one wedged worker can't block the barrier (and the whole level)
# forever. Generous: the ramp itself can take level×stagger seconds.
_BARRIER_TIMEOUT_S = 600.0


def _label_from_warehouse_override(connector_config: dict) -> str | None:
    """Deterministic compute label from a ``BENCHMARK_WH_<SIZE>`` override, or
    ``None`` when no override is set (default warehouse — nothing to disambiguate)."""
    wh = connector_config.get("warehouse")
    if not isinstance(wh, str) or not wh.startswith("BENCHMARK_WH_"):
        return None
    size = _SF_SIZE_DISPLAY.get(wh.removeprefix("BENCHMARK_WH_").upper())
    return f"{size} (Gen2)" if size else None


def _execute_on(
    connector: WarehouseConnector,
    cost: CostCollector,
    platform: str,
    query: BenchmarkQuery,
    scale_factor: int,
    concurrency: int,
) -> dict:
    """Run one query on an ALREADY-CONNECTED connector; never connects itself.

    The session is established during pre-warm (before the barrier), so the timer
    here measures query execution only — connect latency never leaks into
    ``wall_clock_ms``. Any execution failure is caught and returned as a fully
    populated error row (never raised), so one bad query can't wedge the pool.
    """
    start = time.perf_counter()
    try:
        result = execute_with_timeout(
            connector,
            query.sql,
            resolve_query_timeout_s(scale_factor, query.id),
            platform=platform,
            query_id=query.id,
        )
    except Exception as exc:  # execute failed — record, don't hang the pool
        elapsed = (time.perf_counter() - start) * 1000
        logger.warning(
            "concurrency query failed",
            extra={"platform": platform, "query_id": query.id, "concurrency": concurrency},
            exc_info=True,
        )
        return {
            "platform": platform,
            "query_id": query.id,
            "block": query.block,
            "scale_factor": scale_factor,
            "concurrency": concurrency,
            "run_number": 1,
            "wall_clock_ms": elapsed,
            "server_time_ms": None,
            # These MUST mirror the success-path keys — the recorder reads every
            # one, so a missing key raises KeyError mid-pass and aborts the level.
            "bytes_scanned": None,
            "platform_query_id": None,
            "row_count": None,
            "cost_usd": None,
            "cost_confidence": None,
            "error_message": f"{type(exc).__name__}: {exc}",
            "metadata": {"error_type": type(exc).__name__},
        }
    elapsed = (time.perf_counter() - start) * 1000
    return {
        "platform": platform,
        "query_id": query.id,
        "block": query.block,
        "scale_factor": scale_factor,
        "concurrency": concurrency,
        "run_number": 1,
        "wall_clock_ms": result.wall_clock_ms or elapsed,
        "server_time_ms": result.server_time_ms,
        # Queue time is what concurrency measures on engines whose server time is
        # execution only; it must be carried here or no concurrency row has it.
        "queue_ms": result.queue_ms,
        "compile_ms": result.compile_ms,
        "bytes_scanned": result.bytes_scanned,
        "platform_query_id": result.platform_query_id,
        "row_count": result.row_count if result.error_message is None else None,
        "cost_usd": cost.cost_from_result(result),
        "cost_confidence": cost.confidence,
        "error_message": result.error_message,
        "metadata": result.metadata,
    }


def _drain_unrun(
    work: queue.Queue[BenchmarkQuery],
    results_q: queue.Queue[dict],
    platform: str,
    scale_factor: int,
    level: int,
) -> None:
    """Record every query still queued as not run: no session is left to run it.

    Keeps the one-row-per-query count the recorder loop waits on, and records
    each query rather than dropping it (CLAUDE.md: record everything).
    """
    while True:
        try:
            q = work.get_nowait()
        except queue.Empty:
            return
        results_q.put(
            {
                "platform": platform,
                "query_id": q.id,
                "block": q.block,
                "scale_factor": scale_factor,
                "concurrency": level,
                "run_number": 1,
                "wall_clock_ms": 0.0,
                "server_time_ms": None,
                "bytes_scanned": None,
                "platform_query_id": None,
                "row_count": None,
                "cost_usd": None,
                "cost_confidence": None,
                "error_message": SESSION_LOST_MESSAGE,
                "metadata": {"connection_lost": True, "not_run": True},
            }
        )


def _record(
    results_db: ResultsDB,
    row: dict,
    *,
    run_type: str,
    requested: int,
    achieved: int,
    compute_label: str | None,
    provenance_id: str | None,
    tag: str | None = None,
) -> None:
    """Write one worker row, from the main thread (DuckDB is single-writer).

    ``concurrency`` is the level REQUESTED. When pre-warm cannot open that many
    sessions the pass proceeds at ``achieved`` (continue-on-failure), and without
    recording it the row would claim a load that was never offered, with nothing in
    the data to reveal it. Only stamped when they differ, so the metadata stays
    quiet in the normal case and loud in the one that matters.

    ``tag`` (the CLI's ``--tag``) is stamped as ``metadata.tag`` so repeated passes of
    one cell (replicates, a cold then a warm burst) can be told apart after
    latest-wins has collapsed them in the views.
    """
    metadata = row["metadata"]
    extra: dict = {}
    if achieved != requested:
        extra = {"achieved_concurrency": achieved, "requested_concurrency": requested}
    if tag:
        extra["tag"] = tag
    if extra:
        metadata = {**metadata, **extra} if isinstance(metadata, dict) else extra
    results_db.record_sql_result(
        platform=row["platform"],
        query_id=row["query_id"],
        block=row["block"],
        scale_factor=row["scale_factor"],
        run_type=run_type,
        run_number=row["run_number"],
        wall_clock_ms=row["wall_clock_ms"],
        server_time_ms=row["server_time_ms"],
        queue_ms=row.get("queue_ms"),
        compile_ms=row.get("compile_ms"),
        bytes_scanned=row["bytes_scanned"],
        platform_query_id=row["platform_query_id"],
        row_count=row["row_count"],
        cost_usd=row["cost_usd"],
        cost_confidence=row["cost_confidence"],
        error_message=row["error_message"],
        metadata=metadata,
        concurrency=row["concurrency"],
        compute_label=compute_label,
        provenance_id=provenance_id,
    )


def _close_pool(pool: list[tuple[WarehouseConnector, CostCollector]]) -> None:
    for connector, _cost in pool:
        try:
            connector.disconnect()
        except Exception:
            logger.debug("connector disconnect failed", exc_info=True)


def _open_pool(
    platform: str,
    connector_config: dict,
    cost_config: dict,
    size: int,
    *,
    compute_label: str | None = None,
    stagger_s: float = _CONNECT_STAGGER_S,
    sleep: Callable[[float], None] = time.sleep,
) -> list[tuple[WarehouseConnector, CostCollector]]:
    """Open ``size`` sessions ONE AT A TIME with a stagger (the slow ramp).

    Each connect is retried with backoff (``connect_with_retry``). A session that
    still can't be opened is skipped rather than aborting the level, so the pass
    proceeds at the achieved concurrency (continue-on-failure per CLAUDE.md).

    Each session gets a cost collector built for ``compute_label`` — the size the
    level is actually running on — so a concurrency sweep prices each size
    correctly. Building collectors at constructor defaults instead makes cost
    **invariant to compute size** — one price across every replica config or
    tier — which silently invalidates the cost axis of the concurrency corpus.

    Args:
        size: how many sessions to open (the requested concurrency level).
        compute_label: the resolved compute-size label for this pass; selects the
            per-size rate entry from ``cost_config``.
        stagger_s: delay between successive connects; spreads the handshake burst.
        sleep: injectable for tests.

    Returns:
        The successfully opened ``(connector, cost_collector)`` pairs (len ≤ size).
    """
    pool: list[tuple[WarehouseConnector, CostCollector]] = []
    for i in range(size):
        try:
            connector = connect_with_retry(
                lambda: create_connector(platform, config=connector_config)
            )
        except ConnectorError:
            logger.warning(
                "pre-warm connect failed after retries; slot skipped",
                extra={"platform": platform, "slot": i + 1, "requested": size},
                exc_info=True,
            )
            continue
        pool.append((connector, collector_for(platform, cost_config, compute_label)))
        if stagger_s and i + 1 < size:
            sleep(stagger_s)
    return pool


def _run_level(
    platform: str,
    connector_config: dict,
    cost_config: dict,
    results_db: ResultsDB,
    queries: list[BenchmarkQuery],
    scale_factor: int,
    level: int,
    compute_label: str | None,
    provenance_id: str | None = None,
    tag: str | None = None,
) -> None:
    """Pre-warm ``level`` sessions, then fire all queries in a synchronized burst.

    Every worker owns one pre-opened session and rendezvous at a barrier, so the
    burst starts simultaneously (a true level-wide offered load) instead of
    dribbling in as lazy connects complete. Results are recorded from the MAIN
    thread as they arrive (DuckDB is single-writer; this keeps recording atomic
    and immediate per CLAUDE.md).
    """
    logger.info(
        "concurrency-pass pre-warming sessions",
        extra={"platform": platform, "concurrency": level, "scale_factor": scale_factor},
    )
    pool = _open_pool(platform, connector_config, cost_config, level, compute_label=compute_label)
    achieved = len(pool)
    if achieved == 0:
        logger.error(
            "concurrency level opened 0 sessions; skipping level",
            extra={"platform": platform, "concurrency": level, "scale_factor": scale_factor},
        )
        return
    if achieved < level:
        logger.warning(
            "concurrency level running at reduced concurrency %d/%d",
            achieved,
            level,
            extra={"platform": platform, "concurrency": level, "scale_factor": scale_factor},
        )

    work: queue.Queue[BenchmarkQuery] = queue.Queue()
    for q in queries:
        work.put(q)
    results_q: queue.Queue[dict] = queue.Queue()
    barrier = threading.Barrier(achieved)
    live = {"sessions": achieved}
    live_lock = threading.Lock()

    def worker(slot: int) -> None:
        connector, cost = pool[slot]
        try:
            barrier.wait(timeout=_BARRIER_TIMEOUT_S)
        except threading.BrokenBarrierError:
            logger.warning("barrier broken; worker proceeding", extra={"slot": slot})
        while True:
            try:
                q = work.get_nowait()
            except queue.Empty:
                break
            # _execute_on never raises — always yields exactly one row per query,
            # so exactly len(queries) rows reach results_q (the recorder loop below
            # relies on that count to know when the level is drained).
            row = _execute_on(connector, cost, platform, q, scale_factor, level)
            results_q.put(row)
            if (row.get("metadata") or {}).get("connection_lost"):
                # The session is gone and reconnecting failed: leave the rest of the
                # queue to the sessions still open rather than fail it in 0 ms.
                logger.warning(
                    "session lost; worker leaving the level",
                    extra={"platform": platform, "concurrency": level, "slot": slot},
                )
                with live_lock:
                    live["sessions"] -= 1
                    last = live["sessions"] == 0
                if last:
                    _drain_unrun(work, results_q, platform, scale_factor, level)
                return

    logger.info(
        "concurrency-pass firing burst",
        extra={
            "platform": platform,
            "concurrency": level,
            "achieved_concurrency": achieved,
            "scale_factor": scale_factor,
        },
    )
    threads = [
        threading.Thread(target=worker, args=(i,), name=f"cc-{level}-{i}") for i in range(achieved)
    ]
    for t in threads:
        t.start()

    for _ in range(len(queries)):
        _record(
            results_db,
            results_q.get(),
            run_type="concurrency",
            requested=level,
            achieved=achieved,
            compute_label=compute_label,
            provenance_id=provenance_id,
            tag=tag,
        )

    for t in threads:
        t.join()
    # Close the level's sessions so the engine isn't left holding N idle sessions
    # when the next level ramps up.
    _close_pool(pool)
    logger.info(
        "concurrency-pass level complete",
        extra={
            "platform": platform,
            "concurrency": level,
            "achieved_concurrency": achieved,
            "scale_factor": scale_factor,
        },
    )


def _resolve_compute_label(
    platform: str,
    connector_config: dict,
    results_db: ResultsDB,
    scale_factor: int,
    run_type: str,
) -> str:
    """The compute-size label for a pass, from a live probe (or the Snowflake override).

    Raises ``UnlabelledComputeError`` rather than let a pass record an unlabelled band.
    """
    # Capture the compute-size label the same way the warm runner does (live
    # engine environment → identical label string), so band series line up with
    # the warm size-sweep rows in the report. Primary is the live probe (also
    # records engine_environment + drift check); if it blips (a transient connect
    # failure would otherwise leave the whole band's rows unlabeled and colliding),
    # fall back to a deterministic label from the requested warehouse override.
    from ..exceptions import UnlabelledComputeError
    from .runner import capture_runtime_environment

    compute_label: str | None = None
    unlabelled: UnlabelledComputeError | None = None
    try:
        probe = connect_with_retry(
            lambda: create_connector(platform, config=connector_config),
            attempts=PROBE_CONNECT_ATTEMPTS,
        )
        try:
            compute_label = capture_runtime_environment(
                results_db, platform, scale_factor, run_type, probe
            )
        finally:
            probe.disconnect()
    except UnlabelledComputeError as exc:
        # The probe answered but could not name a size. Remember it rather than
        # swallowing it: the override fallback below is Snowflake-only, so for every
        # other engine "continue" means writing a whole band unlabelled.
        unlabelled = exc
    except Exception:
        logger.warning("compute-label probe failed; using override fallback", exc_info=True)
    if compute_label is None:
        compute_label = _label_from_warehouse_override(connector_config)
    if compute_label is None:
        # No live label and no deterministic override. Recording here writes an
        # unlabelled band while the pass reports success, and an unlabelled
        # band is invisible to the size-keyed views — aborting is strictly
        # better than recording.
        raise unlabelled or UnlabelledComputeError(
            f"{platform} at SF{scale_factor} ({run_type}): no compute-size label from "
            "the live probe and no deterministic override; refusing to record an "
            "unlabelled band"
        )
    logger.info(
        "concurrency compute label resolved",
        extra={"platform": platform, "scale_factor": scale_factor, "compute_label": compute_label},
    )
    return compute_label


def run_concurrency_pass(
    platform: str,
    connector_config: dict,
    cost_config: dict,
    results_db: ResultsDB,
    scale_factor: int = 100,
    *,
    blocks: Iterable[str] = ("tpcds", "dsb"),
    concurrency_levels: Iterable[int] = (1, 5, 10, 20, 50, 100),
    repeats_per_level: int = 1,
    provenance_id: str | None = None,
    tag: str | None = None,
) -> None:
    """Run each concurrency level as a pre-warmed, barrier-synchronized burst.

    For each level the work queue is the query set (times ``repeats_per_level``);
    ``level`` pre-opened sessions drain it concurrently.

    The compute-size label is captured once up front (from a probe connector on
    ``connector_config``) and stamped on every row, so a concurrency grid swept
    across warehouse bands (via ``--compute-size``) records one distinct series
    per band instead of colliding at the same (scale, level).

    ``provenance_id`` links every recorded row to the invocation that produced it,
    exactly as the warm and cold runners do; rows recorded before provenance
    capture existed carry none.
    """
    # Platform's translated SQL (not the canonical DuckDB dialect), matching
    # the warm runner.
    queries = [q for q in QueryRegistry(dialect=platform).all() if q.block in blocks]
    if repeats_per_level > 1:
        queries = queries * repeats_per_level

    compute_label = _resolve_compute_label(
        platform, connector_config, results_db, scale_factor, "concurrency"
    )
    for level in concurrency_levels:
        logger.info(
            "concurrency-pass starting level",
            extra={"platform": platform, "concurrency": level, "scale_factor": scale_factor},
        )
        _run_level(
            platform,
            connector_config,
            cost_config,
            results_db,
            queries,
            scale_factor,
            level,
            compute_label,
            provenance_id=provenance_id,
            tag=tag,
        )


# ── Sustained load ──────────────────────────────────────────────────────────
#
# A concurrency level is a burst: each suite once across N sessions, over in
# seconds to minutes. That cannot show what happens when load outlasts an engine's
# smoothing or queueing (Fabric spreads a burst's capacity use over later time and
# throttles only once the debt accrues). A sustained pass holds N sessions busy for
# a fixed time instead, cycling through the suite.

#: Worker sentinel on the results queue: one per worker, when it stops taking work.
_WORKER_DONE = object()


def _run_sustained(
    platform: str,
    connector_config: dict,
    cost_config: dict,
    results_db: ResultsDB,
    queries: list[BenchmarkQuery],
    scale_factor: int,
    sessions: int,
    seconds: float,
    compute_label: str | None,
    provenance_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    tag: str | None = None,
) -> int:
    """Hold ``sessions`` pre-opened sessions busy for ``seconds``; return rows recorded.

    Every session starts at the barrier, then takes the next query from an endless
    cycle over the suite until the time is up; a query already running when time
    runs out finishes (or times out) and is recorded. Pass ``k`` of the suite is
    recorded as ``run_number`` ``k`` so passes never collapse under latest-wins, and
    each row carries ``metadata.submitted_s``, seconds from the start of the pass to
    the query's submission, so latency can be read against elapsed time.
    """
    pool = _open_pool(
        platform, connector_config, cost_config, sessions, compute_label=compute_label
    )
    achieved = len(pool)
    if achieved == 0:
        logger.error(
            "sustained pass opened 0 sessions; nothing run",
            extra={"platform": platform, "concurrency": sessions, "scale_factor": scale_factor},
        )
        return 0
    started: list[float] = []
    barrier = threading.Barrier(achieved, action=lambda: started.append(clock()))
    lock = threading.Lock()
    cursor = {"next": 0}
    results_q: queue.Queue = queue.Queue()

    def take() -> tuple[int, BenchmarkQuery, float] | None:
        with lock:
            elapsed = clock() - started[0]
            if elapsed >= seconds:
                return None
            i = cursor["next"]
            cursor["next"] += 1
        return i // len(queries) + 1, queries[i % len(queries)], elapsed

    def worker(slot: int) -> None:
        connector, cost = pool[slot]
        try:
            try:
                barrier.wait(timeout=_BARRIER_TIMEOUT_S)
            except threading.BrokenBarrierError:
                logger.warning("barrier broken; worker proceeding", extra={"slot": slot})
                with lock:
                    if not started:
                        started.append(clock())
            while (item := take()) is not None:
                pass_no, q, submitted = item
                row = _execute_on(connector, cost, platform, q, scale_factor, sessions)
                meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                row["run_number"] = pass_no
                row["metadata"] = {**meta, "submitted_s": round(submitted, 3)}
                results_q.put(row)
                if meta.get("connection_lost"):
                    logger.warning(
                        "session lost; worker leaving the sustained pass",
                        extra={"platform": platform, "concurrency": sessions, "slot": slot},
                    )
                    return
        finally:
            results_q.put(_WORKER_DONE)

    logger.info(
        "sustained pass starting",
        extra={
            "platform": platform,
            "concurrency": sessions,
            "achieved_concurrency": achieved,
            "scale_factor": scale_factor,
            "seconds": seconds,
        },
    )
    threads = [
        threading.Thread(target=worker, args=(i,), name=f"sustained-{i}") for i in range(achieved)
    ]
    for t in threads:
        t.start()
    recorded = done = 0
    while done < achieved:
        row = results_q.get()
        if row is _WORKER_DONE:
            done += 1
            continue
        _record(
            results_db,
            row,
            run_type=SUSTAINED_RUN_TYPE,
            requested=sessions,
            achieved=achieved,
            compute_label=compute_label,
            provenance_id=provenance_id,
            tag=tag,
        )
        recorded += 1
    for t in threads:
        t.join()
    _close_pool(pool)
    logger.info(
        "sustained pass complete: %d queries recorded",
        recorded,
        extra={"platform": platform, "concurrency": sessions, "scale_factor": scale_factor},
    )
    return recorded


def run_sustained_pass(
    platform: str,
    connector_config: dict,
    cost_config: dict,
    results_db: ResultsDB,
    scale_factor: int = 100,
    *,
    sessions: int,
    minutes: float,
    blocks: Iterable[str] = ("tpcds",),
    provenance_id: str | None = None,
    tag: str | None = None,
) -> int:
    """Hold ``sessions`` sessions busy on the suite for ``minutes``.

    Rows are recorded as run type ``sustained`` with ``concurrency = sessions``,
    one ``run_number`` per pass through the suite. The compute-size label is
    resolved as for a burst pass, so the rows line up with the grid's sizes.

    Returns:
        The number of queries recorded.
    """
    queries = [q for q in QueryRegistry(dialect=platform).all() if q.block in blocks]
    if not queries:
        logger.warning("no queries for blocks %s", list(blocks), extra={"platform": platform})
        return 0
    compute_label = _resolve_compute_label(
        platform, connector_config, results_db, scale_factor, SUSTAINED_RUN_TYPE
    )
    return _run_sustained(
        platform,
        connector_config,
        cost_config,
        results_db,
        queries,
        scale_factor,
        sessions,
        minutes * 60.0,
        compute_label,
        provenance_id=provenance_id,
        tag=tag,
    )

"""Concurrency behaviour reduced to comparable scalars.

The concurrency grid is 6 engines x 5-6 compute sizes x 4 scales x up to 7 levels.
Rendered as curves that is unreadable, and rendered as a single "latency at N=100"
number it is misleading. This module reduces each cell to three quantities that can
be compared across every dimension at once.

**1. The queueing exponent, k.** Latency under load follows a power law closely
enough to fit: ``L(N) = L(1) * N^k`` (observed R^2 mostly 0.7-1.0). One scalar then
describes the whole curve:

* ``k ~ 0`` — added sessions are nearly free; the engine had spare parallel capacity.
* ``k ~ 0.5`` — partial queueing; some parallelism, some waiting.
* ``k ~ 1`` — pure queueing; each added session costs proportional wait.
* ``k > 1`` — contention; added sessions cost superlinearly.

**2. The shed rate.** The fraction of queries that did not complete at the highest
level measured: our timeout fired, the connection dropped, or the engine returned an
error (``failure_class_sql``; the harness's own failures are left out). This is
*independent* of k and must be reported beside it, because a single latency number
cannot tell an engine that queues everything from one that completes only part of a
burst. The shed rate says that queries did not complete, not why: a timeout does not
show whether the engine queued the query past our budget or never accepted it.

**Latency is the engine's time, queue included, network excluded** (``engine_ms_sql``).
Under load most of a query's time can be spent waiting in the engine's queue. The
engine time is defined once, in :mod:`benchmark.analysis.engine_time`, and the shared
views carry it as ``server_time_ms`` on every run type: execution, plus compilation and
queue wait where an engine reports them apart
(:data:`~benchmark.analysis.engine_time.EXECUTION_ONLY_ENGINES`,
:data:`~benchmark.analysis.engine_time.COMPILE_EXCLUDED_ENGINES`). A row whose queue
time was not captured has none, and a cell where too few rows have it has its latency
withheld (``WITHHELD_NO_ENGINE_TIME``) rather than published without it.

**3. Useful concurrency at an SLA.** The highest level whose median latency stays
within a latency target *and* whose shed rate stays acceptable. This is the
buyer-facing unifier: it folds starting latency, degradation slope and shedding into
one number, and it is the only one of the three that answers "how many analysts can
point at this."

That third metric is why k alone must never be published as a ranking. MotherDuck has
among the *worst* exponents measured (k up to 1.17 — it queues hard) and the *best*
useful concurrency at SF100 (100 sessions under a 5 s target), because it starts fast
enough to absorb the degradation. BigQuery has among the best exponents and middling
useful concurrency, because slot scheduling puts a floor under its solo latency.

.. warning:: These reduce a metric with a known limitation. Workers drain a shared
   queue, so offered load decays toward 1 as the queue empties; ``k`` is therefore a
   lower bound on true sustained-load degradation, and the shed rate is measured
   under a burst rather than a steady state. See the concurrency methodology note.
"""

from __future__ import annotations

import logging
import math
import statistics
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    import duckdb

logger = logging.getLogger("benchmark.analysis.concurrency_shape")

# Levels whose shed rate exceeds this are excluded from the k fit: once an engine is
# rejecting work, the surviving queries are a biased sample (the easy ones) and their
# latency understates degradation.
MAX_SHED_PCT_FOR_FIT = 10.0

# Shed rate above which a level is not considered "served" for the SLA metric.
SLA_MAX_SHED_PCT = 5.0

# Minimum distinct levels needed before a power-law fit means anything.
MIN_LEVELS_FOR_FIT = 3

# Minimum rows in a (platform, scale, compute size, level) cell before it is worth
# reducing: about a quarter of one decision-support level pass (201 queries).
# ClickBench's 43-query suite needs its own floor; see ``concurrency_grid.CLICKBENCH``.
MIN_ROWS_PER_CELL = 50


# A shed rate is only an engine property if the client did not cause it. Cells whose
# errors include client-side cancellations above this share are not publishable as
# engine behaviour — the observed shedding is partly ours. See `client_capped_pct`.
MAX_CLIENT_CAPPED_PCT = 1.0

# Errors raised by the CLIENT giving up, not by the engine refusing work — e.g. a
# connector-side network timeout cancelling in-flight queries (see
# connectors/snowflake.py). Counting those as engine sheds attributes our own
# socket hanging up to the engine.
CLIENT_CANCEL_PATTERNS = ("%cancelled by the client%", "%execution canceled%")


def client_cancel_predicate(column: str = "error_message") -> str:
    """SQL predicate matching errors WE caused rather than the engine.

    Shared by every consumer so one definition governs which failures are ours.
    A cell's failure counts are only an engine property if this is false for all
    of them, and the report has several independent paths that read error rates
    (concurrency pivot, sizing matrix, value disqualification, warm incidents) —
    each one of which would otherwise be free to disagree about attribution.
    """
    return " OR ".join(f"{column} LIKE '{p}'" for p in CLIENT_CANCEL_PATTERNS)


#: Share of a level's successful rows that must carry engine time for its median
#: to be published. Below it the cell's latency is withheld, not guessed.
MIN_ENGINE_TIME_PCT = 95.0

#: The concurrency levels of the benchmark's protocol ladder. Every published
#: concurrency figure reads these levels only: some ClickBench rows (recorded without
#: run records) carry a 40-session level outside the ladder, which the shared report
#: views and the grid leave out.
PROTOCOL_LEVELS = (1, 5, 10, 20, 50, 100)

#: Why a cell is not published, for the export and the report.
WITHHELD_CLIENT_CAPPED = "client-capped"
WITHHELD_NO_ENGINE_TIME = "engine time not captured"


def engine_ms_sql(alias: str = "") -> str:
    """SQL for the engine's time on one successful query, over a shared-view row.

    The views already carry the engine time as ``server_time_ms``
    (:func:`benchmark.analysis.engine_time.engine_time_sql`: queue and compilation
    included, NULL where an execution-only engine's queue time was not captured), so
    this only drops failed rows. Read it over a view, never the raw table, whose
    ``server_time_ms`` is the engine's own execution-only figure.
    """
    a = f"{alias}." if alias else ""
    return f"CASE WHEN {a}error_message IS NOT NULL THEN NULL ELSE {a}server_time_ms END"


#: Our own timeout, or our client giving up: we stopped waiting.
TIMEOUT_PATTERNS = ("query exceeded timeout of %", *CLIENT_CANCEL_PATTERNS)

#: The connection broke under a running query (the service or the network
#: dropped it) rather than the engine answering with an error.
CONNECTION_ERROR_PATTERNS = (
    "%Communication link failure%",
    "%TCP Provider%",
    "%RemoteDisconnected%",
    "%Connection aborted%",
    "%Connection reset%",
)

#: Not a measurement of the engine. "not connected" and "session lost" are
#: queries sent into a session the harness had already lost (a failed reconnect
#: after a timeout; see ``runner.execute_with_timeout``). "Broken pipe" is a query
#: written into a session the network had reaped while it sat idle at the level's
#: barrier, before the engine saw anything (Redshift at 1 TB; TCP keepalive guards
#: against it, docs/ARCHITECTURE.md §5).
HARNESS_ERROR_PATTERNS = ("%connector not connected%", "not run: session lost%", "%Broken pipe%")

#: The failure classes that count as a query not completed (``shed_pct``).
UNFINISHED_CLASSES = ("timeout", "connection", "engine")


def _like_any(column: str, patterns: tuple[str, ...]) -> str:
    return "(" + " OR ".join(f"{column} LIKE '{p}'" for p in patterns) + ")"


def failure_class_sql(column: str = "error_message") -> str:
    """SQL naming why a concurrency row failed: NULL (it did not), ``harness``,
    ``timeout``, ``connection`` or ``engine`` (the engine returned an error).

    ``harness`` rows are left out of every concurrency figure: nothing about the
    engine was measured. The other three all count as unfinished for the user,
    and are also reported apart so a reader can see which kind a cell's are.
    """
    return (
        f"CASE WHEN {column} IS NULL THEN NULL"
        f" WHEN {_like_any(column, HARNESS_ERROR_PATTERNS)} THEN 'harness'"
        f" WHEN {_like_any(column, TIMEOUT_PATTERNS)} THEN 'timeout'"
        f" WHEN {_like_any(column, CONNECTION_ERROR_PATTERNS)} THEN 'connection'"
        " ELSE 'engine' END"
    )


class CappedCell(NamedTuple):
    """A cell whose failures were substantially ours, not the engine's."""

    platform: str
    scale_factor: int
    compute_label: str
    run_type: str
    rows: int
    errors: int
    client_cancelled: int

    @property
    def client_cancel_pct(self) -> float:
        return 100.0 * self.client_cancelled / self.rows if self.rows else 0.0

    @property
    def errors_all_ours(self) -> bool:
        """True when the engine raised nothing at all in this cell."""
        return self.errors > 0 and self.client_cancelled == self.errors


def capped_cells(
    con: duckdb.DuckDBPyConnection,
    *,
    run_type: str = "concurrency",
    blocks: tuple[str, ...] | None = None,
    threshold_pct: float = MAX_CLIENT_CAPPED_PCT,
) -> list[CappedCell]:
    """Cells whose client-cancellation share exceeds ``threshold_pct``.

    Reported at (platform, scale, compute_label) granularity — the whole compute
    ladder for that cell, not the individual levels that cancelled. Both the
    latency medians (survivor-biased, because cancelled queries drop out) and the
    error rates are compromised across the cell, so withholding a subset of its
    levels would leave a curve whose shape is still partly ours.

    Args:
        con: read-only results DB connection.
        run_type: which pass to inspect.
        blocks: restrict to these workload blocks; None means all.
        threshold_pct: share of a cell's rows that must be client-cancelled.

    Returns:
        One :class:`CappedCell` per affected cell, worst first.
    """
    from ..report.views import install_views

    install_views(con)
    block_filter = " AND block IN (" + ", ".join(f"'{b}'" for b in blocks) + ")" if blocks else ""
    rows = con.execute(
        f"""
        SELECT platform, scale_factor, compute_label,
               count(*) AS rows_n,
               sum(CASE WHEN error_message IS NOT NULL THEN 1 ELSE 0 END) AS errors,
               sum(CASE WHEN {client_cancel_predicate()} THEN 1 ELSE 0 END) AS cancelled
        FROM results_latest
        WHERE run_type = ? AND compute_label IS NOT NULL{block_filter}
        GROUP BY 1, 2, 3
        HAVING sum(CASE WHEN {client_cancel_predicate()} THEN 1 ELSE 0 END) * 100.0
               / count(*) > ?
        ORDER BY cancelled * 1.0 / rows_n DESC
        """,
        [run_type, threshold_pct],
    ).fetchall()
    out = [
        CappedCell(
            platform=p,
            scale_factor=int(sf),
            compute_label=lbl,
            run_type=run_type,
            rows=int(n),
            errors=int(errs),
            client_cancelled=int(canc),
        )
        for p, sf, lbl, n, errs, canc in rows
    ]
    for cell in out:
        logger.warning(
            "%s %s SF%s %s: %.1f%% of rows client-cancelled (%d of %d errors were ours)"
            " — not publishable as engine behaviour",
            cell.run_type,
            cell.platform,
            cell.scale_factor,
            cell.compute_label,
            cell.client_cancel_pct,
            cell.client_cancelled,
            cell.errors,
            extra={"platform": cell.platform, "scale_factor": cell.scale_factor},
        )
    return out


def engine_time_withheld_cells(
    con: duckdb.DuckDBPyConnection, blocks: tuple[str, ...] = ("tpcds", "dsb")
) -> dict[tuple[str, int, str], float]:
    """Cells whose latency is withheld because engine time was not captured.

    Keyed ``(platform, scale_factor, compute_label)`` (campaign tags kept apart, so
    it serves pooled and per-scale consumers alike), valued by the lowest share of
    a level's successful rows that carried engine time. The same rule as
    :func:`shapes`: any level under :data:`MIN_ENGINE_TIME_PCT` withholds the cell.
    """
    from ..report.views import install_views

    install_views(con)
    block_list = ", ".join(f"'{b}'" for b in blocks)
    rows = con.execute(
        f"""
        SELECT platform, scale_factor, compute_label, min(pct) FROM (
            SELECT platform, scale_factor, compute_label, concurrency,
                   100.0 * count(engine_ms)
                     / NULLIF(count(*) FILTER (WHERE error_message IS NULL), 0) AS pct
            FROM concurrency_results
            WHERE block IN ({block_list}) AND compute_label IS NOT NULL
              AND concurrency IS NOT NULL AND failure_class IS DISTINCT FROM 'harness'
            GROUP BY 1, 2, 3, 4
        ) GROUP BY 1, 2, 3
        HAVING min(pct) < {MIN_ENGINE_TIME_PCT}
        """
    ).fetchall()
    return {(p, int(sf), lbl): float(pct) for p, sf, lbl, pct in rows}


class CellShape(NamedTuple):
    """Concurrency behaviour of one (platform, scale, compute size)."""

    platform: str
    #: None when the reducer pooled campaign scale tags (``shapes(pool_scales=True)``):
    #: ClickBench's tag is not a data volume, so the cell is (platform, compute size).
    scale_factor: int | None
    compute_label: str
    k: float | None
    r_squared: float | None
    shed_pct_at_max: float
    max_level: int
    solo_latency_ms: float | None
    failure_mode: str
    useful_concurrency: int
    client_capped_pct: float
    publishable: bool
    #: True when ``useful_concurrency`` equals ``max_level`` — the cell PASSED at the
    #: highest level we ever asked of it, so its ceiling is a lower bound rather than
    #: a measurement. Reporting a censored 50 beside an uncensored 100 reads as "half
    #: the users" when it may only mean "we never tried 100 here" — and roughly half
    #: the measured cells are censored. Render it visibly (``50+``) rather than
    #: silently.
    censored: bool
    #: Why the cell is not publishable (``WITHHELD_*``), None when it is.
    withheld_reason: str | None = None


def _fit_power_law(points: list[tuple[int, float]]) -> tuple[float | None, float | None]:
    """Least-squares slope of log(latency) on log(concurrency), with R^2."""
    if len(points) < MIN_LEVELS_FOR_FIT:
        return None, None
    xs = [math.log(n) for n, _ in points]
    ys = [math.log(v) for _, v in points]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None, None
    k = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / denom
    predicted = [my + k * (x - mx) for x in xs]
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, predicted, strict=True))
    ss_tot = sum((y - my) ** 2 for y in ys)
    return k, (1 - ss_res / ss_tot if ss_tot else None)


def classify(k: float | None, shed_pct: float) -> str:
    """Name the failure mode from the two independent axes.

    A CAUTION ON THE LABELS. ``rejects`` names a *signature* — latency flat, a large
    share of queries not completing — not a mechanism. Most non-completions in this
    corpus are our own timeout firing (the rest are engine errors such as memory
    limits, or dropped connections; ``failure_class_sql`` tells them apart), so a
    timeout-driven ``rejects`` shows that the queries the engine finished were fast,
    not whether it refused the rest or queued them past our budget. Do not write "the
    engine rejected N%" from this label; write what was measured. The shed rate is
    also partly a property of the timeout we chose.

    The point of the two-axis view: ``absorbs`` and ``rejects`` look identical if you
    only plot latency, and ``queues`` looks worse than ``rejects`` even though it
    served every query.
    """
    if k is None:
        return "unknown"
    sheds = shed_pct >= SLA_MAX_SHED_PCT
    steep = k >= 0.4
    if not steep and not sheds:
        return "absorbs"
    if not steep and sheds:
        return "rejects"
    if steep and not sheds:
        return "queues"
    return "queues+sheds"


def shapes(
    con: duckdb.DuckDBPyConnection,
    *,
    sla_ms: float = 5000.0,
    blocks: tuple[str, ...] = ("tpcds", "dsb"),
    min_rows: int = MIN_ROWS_PER_CELL,
    pool_scales: bool = False,
    levels: tuple[int, ...] | None = None,
) -> list[CellShape]:
    """Reduce every concurrency cell to its shape.

    Args:
        con: Read-only results DB connection. Reads the shared ``results_latest``
            view (installed lazily via :func:`benchmark.report.views.install_views`).
        sla_ms: Latency target for the useful-concurrency metric.
        blocks: Workload blocks to include, pooled into one cell. The default is the
            decision-support pair; ClickBench is reduced as its own workload with the
            settings in ``concurrency_grid.CLICKBENCH``.
        min_rows: Rows a level must carry to count as measured.
        pool_scales: Pool campaign scale tags into one cell per (platform, compute
            size) and report ``scale_factor`` as None. Right for ClickBench, whose
            tag is a campaign label over one fixed ~100 GB table, so the same
            configuration measured under two tags is one configuration.
        levels: Keep only these concurrency levels; None keeps every level run.

    Returns:
        One :class:`CellShape` per (platform, scale, compute size), sorted.
    """
    from ..report.views import install_views

    install_views(con)
    block_list = ", ".join(f"'{b}'" for b in blocks)
    client_cancel = client_cancel_predicate()
    unfinished = ", ".join(f"'{c}'" for c in UNFINISHED_CLASSES)
    scale_expr = "NULL::INTEGER" if pool_scales else "scale_factor"
    group_by = [
        "platform",
        *([] if pool_scales else ["scale_factor"]),
        "compute_label",
        "concurrency",
    ]
    level_filter = (
        f" AND concurrency IN ({', '.join(str(int(n)) for n in levels)})" if levels else ""
    )
    # Harness rows (failure_class 'harness') measured nothing about the engine and
    # are left out of every rate and of the row floor.
    rows = con.execute(
        f"""
        SELECT platform, {scale_expr} AS scale_factor, compute_label, concurrency,
               quantile_cont(engine_ms, 0.5)
                 FILTER (WHERE error_message IS NULL) AS p50,
               100.0 * count(*) FILTER (WHERE failure_class IN ({unfinished}))
                 / count(*) AS shed_pct,
               100.0 * count(*) FILTER (WHERE {client_cancel}) / count(*) AS client_capped_pct,
               100.0 * count(engine_ms)
                 / NULLIF(count(*) FILTER (WHERE error_message IS NULL), 0) AS engine_time_pct
        FROM concurrency_results
        WHERE block IN ({block_list}) AND failure_class IS DISTINCT FROM 'harness'
          AND compute_label IS NOT NULL AND concurrency IS NOT NULL{level_filter}
        GROUP BY {", ".join(group_by)}
        HAVING count(*) >= {int(min_rows)}
        ORDER BY 1, 2, 3, 4
        """
    ).fetchall()

    cells: dict[tuple[str, int | None, str], list[tuple[int, float | None, float, float]]] = {}
    no_queue_time: set[tuple[str, int | None, str]] = set()
    for platform, scale, label, level, p50, shed, capped, timed in rows:
        key = (platform, int(scale) if scale is not None else None, label)
        if timed is not None and timed < MIN_ENGINE_TIME_PCT:
            no_queue_time.add(key)
        cells.setdefault(key, []).append((int(level), p50, float(shed), float(capped or 0.0)))

    out: list[CellShape] = []
    # None sorts as 0 so pooled cells order like everything else.
    for (platform, scale, label), points in sorted(
        cells.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0, kv[0][2])
    ):
        withheld = (platform, scale, label) in no_queue_time
        if withheld:
            # The engine's wait was not captured: a median without it is not the
            # engine's time, so the cell keeps its failure rates and loses its latency.
            points = [(n, None, shed, capped) for n, _, shed, capped in points]
        fit_points = [
            (n, v) for n, v, shed, _ in points if v and v > 0 and shed < MAX_SHED_PCT_FOR_FIT
        ]
        k, r2 = _fit_power_law(fit_points)
        top = max(points, key=lambda p: p[0])
        solo = next((v for n, v, _, _ in points if n == 1), None)
        served = [n for n, v, shed, _ in points if v and v <= sla_ms and shed < SLA_MAX_SHED_PCT]
        capped_pct = max(c for _, _, _, c in points)
        reason = (
            WITHHELD_NO_ENGINE_TIME
            if withheld
            else WITHHELD_CLIENT_CAPPED
            if capped_pct > MAX_CLIENT_CAPPED_PCT
            else None
        )
        out.append(
            CellShape(
                platform=platform,
                scale_factor=scale,
                compute_label=label,
                k=k,
                r_squared=r2,
                shed_pct_at_max=top[2],
                max_level=top[0],
                solo_latency_ms=solo,
                failure_mode=classify(k, top[2]),
                useful_concurrency=max(served) if served else 0,
                client_capped_pct=capped_pct,
                publishable=reason is None,
                censored=bool(served) and max(served) == top[0],
                withheld_reason=reason,
            )
        )
    logger.info("reduced %d concurrency cells to shapes", len(out))
    return out


def to_records(cells: list[CellShape]) -> list[dict[str, Any]]:
    """Plain dicts, for export to parquet or a report table."""
    return [c._asdict() for c in cells]

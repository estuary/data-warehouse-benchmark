"""The engine's time on a query: the one definition every latency and cost figure uses.

**Engine time is the engine's whole hold on a query, network excluded:** execution,
plus compilation and any wait in the engine's own queue where the engine reports them
apart from execution. Snowflake, Fabric, MotherDuck and ClickHouse report total elapsed
time, which already holds all three. Databricks reports compilation apart
(:data:`COMPILE_EXCLUDED_ENGINES`), and Databricks, Redshift and BigQuery report
execution without the queue wait (:data:`EXECUTION_ONLY_ENGINES`; reconcile back-fills
it into ``queue_ms`` from each engine's history).

The same rule holds on every run type: a single-user query is timed the way a query
under load is, so warm and concurrency latency, and the cost each row is priced on,
all count the same time. Single-session waits are small but not zero
everywhere: BigQuery's job dispatch is about 70 ms a query, Databricks' and Redshift's
waits measured 0 ms at one session.

Two readers, one builder (:func:`engine_time_sql`):

- **Latency** (the shared report views, ``report/views.py``) reads it with
  ``missing_wait="null"``: a row whose wait was not captured has no engine time, and is
  left out rather than published without it (the concurrency cells' 95% rule,
  ``concurrency_shape.MIN_ENGINE_TIME_PCT``).
- **Cost** (``analysis/recost.py``) reads it with ``missing_wait="zero"``: a price is
  owed whether or not the wait was captured, so an uncaptured wait counts as zero.
"""

from __future__ import annotations

from typing import Literal

#: Engines whose ``server_time_ms`` leaves compilation out, with the column holding it.
#: Snowflake, Fabric, MotherDuck and ClickHouse report total elapsed, which includes it.
#: A row whose compile time was not captured counts it as zero. Redshift's
#: ``execution_time`` also excludes compilation, but its compiled segments are cached
#: after the warm-up run and the history that would have measured the share has
#: expired; it is not listed.
COMPILE_EXCLUDED_ENGINES: dict[str, str] = {"databricks": "compile_ms"}

#: Engines whose ``server_time_ms`` is execution only, with the columns that hold the
#: rest of the engine's time on a query. The first column is required: without it the
#: engine's wait is unknown, not zero. Compilation is not listed here: it is
#: :data:`COMPILE_EXCLUDED_ENGINES`'s.
EXECUTION_ONLY_ENGINES: dict[str, tuple[str, ...]] = {
    "bigquery": ("queue_ms",),
    "databricks": ("queue_ms",),
    "redshift": ("queue_ms",),
}


#: Engines whose single-session queue wait measured zero, with the evidence. For latency,
#: a single-user row (any run type but concurrency) of one of these whose wait was not
#: captured counts it as zero rather than having no engine time; under concurrency the
#: wait is never assumed. Redshift keeps its history for seven days, so some of its
#: warm cells have no captured wait.
SINGLE_SESSION_ZERO_WAIT: dict[str, str] = {
    "redshift": (
        "0 ms on all 1,464 single-user rows of a dedicated re-measurement (8 and 16 RPU, 1 GB,"
        " every suite) and on all 5,220 one-session concurrency rows"
    ),
}


def _col(alias: str, column: str) -> str:
    return f"{alias}.{column}" if alias else column


def execution_sql(alias: str = "", *, present: set[str] | None = None) -> str:
    """SQL for the engine's execution time with compilation included.

    ``server_time_ms``, plus the compile column on :data:`COMPILE_EXCLUDED_ENGINES`
    (NULL compile counts as zero; NULL server time stays NULL). ``present`` names the
    columns the source carries; a compile column it lacks is left out.
    """
    st = _col(alias, "server_time_ms")
    whens = " ".join(
        f"WHEN {_col(alias, 'platform')} = '{platform}' THEN {st} + COALESCE({_col(alias, column)}, 0)"
        for platform, column in COMPILE_EXCLUDED_ENGINES.items()
        if present is None or column in present
    )
    return f"(CASE {whens} ELSE {st} END)" if whens else st


def engine_time_sql(
    alias: str = "",
    *,
    missing_wait: Literal["null", "zero"] = "null",
    present: set[str] | None = None,
) -> str:
    """SQL for the engine's time on a query: execution, compilation and queue wait.

    On :data:`EXECUTION_ONLY_ENGINES` the wait columns are added to
    :func:`execution_sql`. When the required one was not captured the result is NULL
    (``missing_wait="null"``, for latency; a single-user row of a
    :data:`SINGLE_SESSION_ZERO_WAIT` engine counts it as zero) or the wait counts as
    zero (``missing_wait="zero"``, for cost). Extra columns always count missing as zero.
    NULL server time stays NULL. ``present`` names the columns the source carries; a
    wait column it lacks is treated as not captured.
    """
    execution = execution_sql(alias, present=present)
    whens = []
    for platform, (required, *extra) in EXECUTION_ONLY_ENGINES.items():
        captured = present is None or required in present
        extras = [
            f"COALESCE({_col(alias, c)}, 0)" for c in extra if present is None or c in present
        ]
        if missing_wait == "zero":
            wait = [f"COALESCE({_col(alias, required)}, 0)"] if captured else []
            value = " + ".join([execution, *wait, *extras])
        elif captured:
            parts = " + ".join([execution, _col(alias, required), *extras])
            missing = "NULL"
            if platform in SINGLE_SESSION_ZERO_WAIT and (present is None or "run_type" in present):
                missing = (
                    f"CASE WHEN {_col(alias, 'run_type')} <> 'concurrency' THEN "
                    f"{' + '.join([execution, *extras])} END"
                )
            value = f"CASE WHEN {_col(alias, required)} IS NULL THEN {missing} ELSE {parts} END"
        else:
            value = "NULL"
        whens.append(f"WHEN {_col(alias, 'platform')} = '{platform}' THEN {value}")
    return f"(CASE {' '.join(whens)} ELSE {execution} END)"

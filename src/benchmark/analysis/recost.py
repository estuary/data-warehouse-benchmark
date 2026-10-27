"""Repair passes that re-price already-recorded rows from their stored metadata.

Distinct from :mod:`benchmark.analysis.reconcile`: reconcile *back-fills* metrics
that were missing or provisional by re-querying the engine's history, and only
considers rows that look incomplete (NULL server time, ``estimated`` confidence,
zero cost). A **re-cost** pass instead corrects rows that were recorded with a
confident-looking but *wrong* price, using data already captured at run time. No
engine round-trip, no benchmark re-run.

Re-cost passes must be **idempotent** — running one twice produces the same
prices — so they always recompute from the immutable per-row metadata rather than
adjusting the current ``cost_usd`` in place.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..cost.bigquery import BigQueryCostCollector

if TYPE_CHECKING:
    from ..results import ResultsDB

logger = logging.getLogger("benchmark.analysis.recost")

# `compute_label_from_environment` stamps reservation runs as
# "<EDITION> / <N> slots" (e.g. "ENTERPRISE / 800 slots") and on-demand runs as
# the literal "on-demand". Matching the label — not the cost-config — is what
# makes this correct: the label is derived from INFORMATION_SCHEMA.RESERVATIONS
# at run time, so it records what actually ran, whereas cost_config.json is a
# point-in-time capture that the sweep's reservation teardown invalidates.
RESERVATION_LABEL_PATTERN = "%/%slots"

MS_PER_HOUR = 3_600_000.0


def bigquery_reservation_recost_sql(usd_per_slot_hour: float) -> str:
    """SQL that re-prices BigQuery reservation rows from consumed slot-time.

    Args:
        usd_per_slot_hour: Enterprise edition slot-hour list price.

    Returns:
        An ``UPDATE`` statement re-pricing every reservation-labelled BigQuery
        row from ``metadata.slot_millis``. Errored rows are priced at 0 (they
        consumed no billable slot-time we can attribute); rows with no
        ``slot_millis`` recorded are left untouched so a missing measurement is
        never silently converted into a $0 measurement.
    """
    return f"""
        UPDATE sql_benchmark_results
        SET cost_usd = CASE
                WHEN error_message IS NOT NULL THEN 0.0
                ELSE (CAST(json_extract_string(metadata, '$.slot_millis') AS BIGINT)
                      / {MS_PER_HOUR}) * {usd_per_slot_hour}
            END,
            cost_confidence = 'direct'
        WHERE platform = 'bigquery'
          AND compute_label LIKE '{RESERVATION_LABEL_PATTERN}'
          AND json_extract_string(metadata, '$.slot_millis') IS NOT NULL
    """


def recost_bigquery_reservations(results_db: ResultsDB, *, dry_run: bool = False) -> dict[str, Any]:
    """Re-price BigQuery slot-reservation rows from consumed slot-time.

    **What it repairs.** A ``capture-cost-config`` run after the slot sweep has
    torn its reservation down (see
    ``compute.bigquery_reservation.BigQueryReservationManager.teardown``) records
    BigQuery as ``tier: "on-demand"``, so rows stamped ``ENTERPRISE / N slots``
    are priced through the collector's on-demand branch, ``total_bytes_billed`` ×
    $6.25/TiB. That is the wrong pricing model and, because bytes scanned do not
    vary with reservation size, *invariant to the very axis the slot sweep
    measures*: it reads as a finding ("cost is flat across reservation size")
    when it is an artifact.

    **The model used here.** Cost is ``slot_millis`` × the Enterprise slot-hour
    rate — the *consumed* model. The alternative *committed* model (provisioned
    slots × rate × wall-clock time the reservation was held) is the truer
    description of a fixed-baseline reservation, and the collector implements
    it, but it is **not computable from what was recorded**: the sweep
    interleaved reservation sizes across days, so per-size wall-clock spans
    derived from ``created_at`` run to 24+ hours of mostly-idle time and would
    price idle capacity as though it were query cost.

    Consumed pricing is a directly measured, per-query quantity, is bounded
    above by the reservation cap (``ignore_idle_slots=True`` means a query
    cannot borrow beyond its N slots), and needs no allocation across
    concurrent queries — so it stays correct under the concurrency ladders,
    where a committed allocation would over-count by the concurrency factor
    unless given a share denominator.

    **What it understates.** A fixed reservation bills for provisioned capacity
    whether or not queries are using it. This pass prices only slot-time
    actually consumed, so it reflects BigQuery Editions *autoscale* economics
    rather than a held fixed baseline. Reports built on these figures must say
    so.

    Args:
        results_db: Results database to repair, mutated in place unless
            ``dry_run``.
        dry_run: When True, compute and return the before/after summary without
            writing.

    Returns:
        Summary dict with the affected row count and the total cost before and
        after, so the magnitude of the correction is visible to the caller.
    """
    rate = BigQueryCostCollector.ENTERPRISE_USD_PER_SLOT_HOUR
    with results_db.connect() as con:
        before = con.execute(
            f"""
            SELECT count(*),
                   coalesce(sum(cost_usd), 0),
                   count(*) FILTER (
                       WHERE json_extract_string(metadata, '$.slot_millis') IS NULL)
            FROM sql_benchmark_results
            WHERE platform = 'bigquery'
              AND compute_label LIKE '{RESERVATION_LABEL_PATTERN}'
            """
        ).fetchone()
        rows, cost_before, missing_slots = before[0], before[1], before[2]

        projected = con.execute(
            f"""
            SELECT coalesce(sum(CASE
                       WHEN error_message IS NOT NULL THEN 0.0
                       ELSE (CAST(json_extract_string(metadata, '$.slot_millis') AS BIGINT)
                             / {MS_PER_HOUR}) * {rate}
                   END), 0)
            FROM sql_benchmark_results
            WHERE platform = 'bigquery'
              AND compute_label LIKE '{RESERVATION_LABEL_PATTERN}'
              AND json_extract_string(metadata, '$.slot_millis') IS NOT NULL
            """
        ).fetchone()[0]

        if not dry_run:
            con.execute(bigquery_reservation_recost_sql(rate))

    summary = {
        "platform": "bigquery",
        "rows": rows,
        "rows_without_slot_millis": missing_slots,
        "cost_before_usd": round(cost_before, 4),
        "cost_after_usd": round(projected, 4),
        "dry_run": dry_run,
    }
    logger.info(
        "bigquery reservation re-cost%s: %d rows, $%.2f -> $%.2f (%d rows lacked slot_millis)",
        " (dry run)" if dry_run else "",
        rows,
        cost_before,
        projected,
        missing_slots,
    )
    return summary


def clear_fabricated_redshift_estimates(
    results_db: ResultsDB, *, dry_run: bool = False
) -> dict[str, Any]:
    """Void Redshift ``estimated``-tier costs produced by the per-query 60s floor.

    **What it repairs.** The ``estimated`` Redshift rows this voids carry
    Redshift Serverless's 60-second minimum charge applied to *every query*. That
    minimum bills a compute **session**, and a benchmark pass runs hundreds of
    queries back-to-back inside one session, so a per-query floor asserts hundreds
    of separate wake-ups. Such rows collapse onto one of a handful of flat
    values: exactly $0.40 at 64 RPU whether the query ran 355 ms or 60 s, a ~170x
    overstatement on a sub-second query.

    Left in place these rows do real damage: they make 128 RPU look *cheaper*
    than 64 RPU at SF10, inverting the size-sweep cost curve, because the 36
    fabricated rows in that cell outweigh the 567 correctly-priced ones.

    **Why they are voided rather than re-priced.** ``SYS_SERVERLESS_USAGE`` has a
    7-day retention window; these rows date from 2026-08-04/06 and that window
    has closed, so no true RPU-second figure can be recovered. Substituting a
    corrected estimate would be inventing a second number to replace the first. Setting ``cost_usd`` and ``cost_confidence`` to
    NULL records the honest state — *this row's cost is unknown* — and makes the
    rows fall out of ``sum``/``median`` aggregates automatically instead of
    relying on every future query remembering to filter them. Latency data on
    these rows is unaffected and stays in the benchmark.

    The original value is preserved under ``metadata.voided_cost_usd`` so the
    correction is auditable and reversible.

    Args:
        results_db: Results database to repair, mutated in place unless
            ``dry_run``.
        dry_run: When True, report what would be voided without writing.

    Returns:
        Summary dict with the affected row count and the fabricated spend removed.
    """
    with results_db.connect() as con:
        before = con.execute(
            """
            SELECT count(*), coalesce(sum(cost_usd), 0), count(DISTINCT cost_usd)
            FROM sql_benchmark_results
            WHERE platform = 'redshift' AND cost_confidence = 'estimated'
            """
        ).fetchone()
        rows, fabricated, distinct_values = before[0], before[1], before[2]

        if not dry_run and rows:
            con.execute(
                """
                UPDATE sql_benchmark_results
                SET metadata = json_merge_patch(
                        coalesce(metadata, '{}'),
                        json_object(
                            'voided_cost_usd', cost_usd,
                            'voided_reason', 'per-query 60s floor; billing window closed'
                        )),
                    cost_usd = NULL,
                    cost_confidence = NULL
                WHERE platform = 'redshift' AND cost_confidence = 'estimated'
                """
            )

    summary = {
        "platform": "redshift",
        "rows_voided": rows,
        "fabricated_usd_removed": round(fabricated, 4),
        "distinct_cost_values": distinct_values,
        "dry_run": dry_run,
    }
    logger.info(
        "redshift fabricated-estimate void%s: %d rows, $%.2f removed (%d distinct values)",
        " (dry run)" if dry_run else "",
        rows,
        fabricated,
        distinct_values,
    )
    return summary


def mixed_confidence_cells(results_db: ResultsDB) -> list[dict[str, Any]]:
    """Cells whose rows carry more than one ``cost_confidence`` tier.

    A benchmark cell — one (platform, scale, run_type, compute_label) — should be
    priced one way. When it is not, an aggregate over that cell silently averages
    measurements of different quality, which is exactly how the Redshift 64 RPU
    inversion hides: 36 fabricated rows outweigh 567 reconciled ones inside a
    single cell.

    Not every mix is a defect. ClickHouse and MotherDuck are ``estimated`` by
    design (no per-query billing API), so a cell that is uniformly estimated is
    correct. This surfaces only *mixed* cells, which always warrant a look.

    Returns:
        One dict per mixed cell, ordered by row count descending, each carrying
        the tiers present and the per-tier row counts and spend.
    """
    with results_db.connect() as con:
        rows = con.execute(
            """
            SELECT platform, scale_factor, run_type, coalesce(compute_label, '-') AS lbl,
                   string_agg(DISTINCT cost_confidence, '+') AS tiers,
                   count(*) AS n
            FROM sql_benchmark_results
            WHERE cost_confidence IS NOT NULL
            GROUP BY 1, 2, 3, 4
            HAVING count(DISTINCT cost_confidence) > 1
            ORDER BY n DESC
            """
        ).fetchall()
    return [
        {
            "platform": r[0],
            "scale_factor": r[1],
            "run_type": r[2],
            "compute_label": r[3],
            "tiers": r[4],
            "rows": r[5],
        }
        for r in rows
    ]


def _rate_per_ms(
    platform: str, label: str, cost_config: dict[str, Any], collector_for: Any
) -> float:
    """USD per wall-clock millisecond for one rate-priced (platform, size).

    ClickHouse is priced from the label itself through the rate card
    (:func:`benchmark.report.compute_rates.effective_usd_per_hour`): a
    ``<replicas>x<memory>GB`` label fixes the bill, because ClickHouse Cloud
    provisions 2 vCPU per 8 GiB and bills per compute unit (8 GiB, 2 vCPU). The
    captured size map in ``cost_config.json`` is NOT used for it: its
    ``1x16GB``/``1x32GB`` entries have their vCPU counts swapped, and it prices
    the warm single-replica rows at another size's rate (1x120GB at $0.30/h
    against $4.48 list, most other sizes at about twice list). Pricing
    from the label makes the stored cost agree with the hourly price every table
    and chart already shows. Other platforms keep the size-aware collector.
    """
    if platform in ("clickhouse", "snowflake"):
        from ..report.compute_rates import effective_usd_per_hour

        usd_per_hour = effective_usd_per_hour(platform, label)
        if usd_per_hour is None:
            raise ValueError(f"{platform} label {label!r} has no list rate")
        return usd_per_hour / MS_PER_HOUR
    collector = collector_for(platform, cost_config, label)
    return collector.estimate_cost(1.0)  # linear in elapsed time


def recost_rate_priced_rows(
    results_db: ResultsDB,
    cost_config: dict[str, Any],
    *,
    platforms: tuple[str, ...] = ("clickhouse", "motherduck", "databricks"),
    run_type: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-price rate-modelled rows at the compute size they actually ran on.

    **What it repairs.** A row priced by a collector built at its **constructor
    defaults**, rather than for the row's compute size, carries a cost *invariant
    to compute size* on the engines whose price is a pure rate model. The 136,395
    concurrency rows priced that way show it:

    * ClickHouse implies exactly **$4.776/hr across all eight replica configs**
      (the ``vcpus=16, replicas=2`` default), from 3x8GB to 3x120GB.
    * MotherDuck implies **$2.40/hr across all five Duckling tiers** (the
      ``standard`` default), understating Giga by an order of magnitude.
    * Databricks' ``estimated`` rows all imply **$16.80/hr** (the ``medium``
      default) regardless of label.

    That invalidates the cost axis of the concurrency corpus, which is 69% of all
    recorded rows.

    **Why it is repairable offline.** All three collectors are pure
    ``wall_clock_ms x rate(compute_size)`` functions and both inputs are already on
    every row, so the correct price is recomputable with no engine round-trip and no
    re-run, the same property the BigQuery reservation repair relies on.

    Rows with no ``compute_label`` are skipped: without a size there is no rate, and
    inventing one would replace a wrong number with a different wrong number.

    **Shared-load rows on ClickHouse and MotherDuck are window-priced**, not priced
    here: ``wall_clock_ms x rate`` charges the whole service to every query in
    flight. This pass leaves their concurrency and sustained rows to
    :func:`recost_capacity_windows` and runs it last, so a full repair never
    re-introduces that overstatement.

    **Only ``estimated``-tier rows are touched.** Databricks carries a mix: rows
    reconciled from ``system.billing.usage`` are ``proportional`` and authoritative,
    and overwriting those with a rate model would *degrade* them. ClickHouse and
    MotherDuck have no reconcile path, so all their rows are ``estimated`` and all
    are repaired. Snowflake is rate-priced the same way; its rows priced by
    attribution are moved to this model by :func:`price_snowflake_on_engine_time`.

    Args:
        results_db: Results database to repair, mutated in place unless ``dry_run``.
        cost_config: Captured cost config, supplying the per-size rate entries.
        platforms: Rate-priced platforms to re-cost.
        run_type: Restrict to one pass, or ``None`` (the default) to repair
            **every** run type. Defaults to all deliberately: a concurrency-only
            default would leave the *warm* rows priced from a superseded rate
            table (ClickHouse ``1x32GB`` 3.75x high and ``2x32GB`` ~2x low, in
            opposite directions, on the exact cost-vs-size axis the report is
            built on). A repair that skips a slice of the corpus by default is a
            repair that will be forgotten.
        dry_run: Compute and return the summary without writing.

    Returns:
        Summary dict with per-(platform, label) row counts and before/after totals.
    """
    from ..cost.discovery import collector_for

    changes: list[dict[str, Any]] = []
    windowed = [p for p in platforms if p in RATE_WINDOW_PLATFORMS]
    not_windowed = (
        "AND NOT (platform IN ({}) AND run_type IN ({}))".format(
            ", ".join(f"'{p}'" for p in windowed),
            ", ".join(f"'{rt}'" for rt in SHARED_RUN_TYPES),
        )
        if windowed
        else ""
    )
    with results_db.connect() as con:
        cells = con.execute(
            """
            SELECT platform, compute_label, run_type, count(*), coalesce(sum(cost_usd), 0)
            FROM sql_benchmark_results
            WHERE platform IN ({}) AND (? IS NULL OR run_type = ?)
              AND compute_label IS NOT NULL AND error_message IS NULL
              AND cost_confidence = 'estimated' {}
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
            """.format(", ".join(f"'{p}'" for p in platforms), not_windowed),
            [run_type, run_type],
        ).fetchall()

        held = _engine_ms_sql("sql_benchmark_results")
        for platform, label, rt, n, before in cells:
            rate_per_ms = _rate_per_ms(platform, label, cost_config, collector_for)
            row = con.execute(
                f"""
                SELECT coalesce(sum({held}), 0) FROM sql_benchmark_results
                WHERE platform = ? AND compute_label = ? AND run_type = ?
                  AND error_message IS NULL AND cost_confidence = 'estimated'
                """,
                [platform, label, rt],
            ).fetchone()
            after = row[0] * rate_per_ms
            changes.append(
                {
                    "platform": platform,
                    "compute_label": label,
                    "run_type": rt,
                    "rows": n,
                    "before_usd": round(before, 4),
                    "after_usd": round(after, 4),
                }
            )
            if not dry_run:
                con.execute(
                    f"""
                    UPDATE sql_benchmark_results
                    SET cost_usd = {held} * ?
                    WHERE platform = ? AND compute_label = ? AND run_type = ?
                      AND error_message IS NULL AND cost_confidence = 'estimated'
                    """,
                    [rate_per_ms, platform, label, rt],
                )

    if windowed and (run_type is None or run_type in SHARED_RUN_TYPES):
        shared_load = recost_capacity_windows(
            results_db, cost_config, platforms=tuple(windowed), dry_run=dry_run
        )
        changes.extend(shared_load["cells"])
    total_before = round(sum(c["before_usd"] for c in changes), 4)
    total_after = round(sum(c["after_usd"] for c in changes), 4)
    logger.info(
        "rate-priced re-cost%s: %d cells, $%.2f -> $%.2f",
        " (dry run)" if dry_run else "",
        len(changes),
        total_before,
        total_after,
    )
    return {
        "cells": changes,
        "total_before_usd": total_before,
        "total_after_usd": total_after,
        "dry_run": dry_run,
    }


# ── capacity windows: engines that bill running compute, not queries ─────────
#
# Fabric, ClickHouse Cloud, MotherDuck's per-second Ducklings and a Databricks SQL
# warehouse bill compute for as long as it runs, however many queries share it. A
# per-query price of "elapsed x the compute's full rate" therefore bills it once per
# query in flight, which under a 100-session burst is up to a hundred times over. The
# passes below price a *window* of shared compute instead and split it across the
# queries that shared it.

#: Run types whose statements share the compute at once: every row of one pass at one
#: level is one window. Warm and cold statements run one at a time, so each is its own.
SHARED_RUN_TYPES = ("concurrency", "sustained")

#: A statement that starts more than this long after every earlier statement of its
#: window has ended opens a new window. Zero: a window is a stretch in which at least
#: one statement is running, so every gap (client turnaround, the harness's per-query
#: history lookups, reconnects, login throttling) leaves query cost, on every engine
#: alike. A positive gap would bill sub-minute gaps as compute: at one session and
#: 1 GB, a 60 s gap charged MotherDuck 2.5x its queries' time and Redshift 1.4x,
#: against 1.0x on Fabric and BigQuery.
IDLE_GAP_MS = 0


def _window_sessions(source: str, out: str) -> str:
    """Two CTEs that cut the rows of CTE ``source`` into windows, the second named ``out``.

    ``source`` needs ``run_id``, ``window_key``, ``start_ms`` and ``end_ms``; ``out``
    adds ``window_id``: the key plus a session number that steps wherever a row
    starts more than :data:`IDLE_GAP_MS` after every earlier row of its key ended.
    """
    return f"""
        {out}_gapped AS (
            SELECT *, max(end_ms) OVER (
                       PARTITION BY window_key ORDER BY start_ms, run_id
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end_ms
            FROM {source}
        ),
        {out} AS (
            SELECT *, window_key || '#' || CAST(sum(
                       CASE WHEN prev_end_ms IS NULL OR start_ms > prev_end_ms + {int(IDLE_GAP_MS)}
                            THEN 1 ELSE 0 END) OVER (
                       PARTITION BY window_key ORDER BY start_ms, run_id
                       ROWS UNBOUNDED PRECEDING) AS VARCHAR) AS window_id
            FROM {out}_gapped
        )"""


def _exec_ms_sql(alias: str = "r") -> str:
    """SQL (over ``alias.``) for a row's execution time on the engine.

    :func:`~benchmark.analysis.engine_time.execution_sql` (``server_time_ms`` plus
    compilation where the engine leaves it out), as its latency counts it. A row with
    no engine time, or a zero (Redshift reports 0 ms for a few queries that took
    seconds), falls back to wall clock. Used to split a window among its queries where
    the engine reports execution apart from its queue (Redshift, Databricks; BigQuery
    where slot time is missing).
    """
    from .engine_time import execution_sql

    return (
        f"(CASE WHEN {alias}.server_time_ms > 0 THEN {execution_sql(alias)} "
        f"ELSE coalesce({alias}.wall_clock_ms, 0) END)"
    )


def _engine_ms_sql(alias: str = "r") -> str:
    """SQL (over ``alias.``) for the time a row held the compute: its engine time.

    Every engine is priced on the engine's own time, not the client's wall clock,
    which adds network round trip and result fetch (CLAUDE.md rule 8 keeps network
    out of latency, and cost follows it). At 1 GB wall clock runs 1.1x engine time
    on ClickHouse to 2.9x on Redshift, so wall-clock pricing would charge engines for
    our network distance.

    Engine time is the one definition latency uses too,
    :func:`~benchmark.analysis.engine_time.engine_time_sql`: execution plus the
    compilation and queue wait an engine reports apart, on every run type. A queued
    query holds its place on compute that is billed while it waits, and counting that
    wait for some engines and not others would favor them. For a price, a row whose
    queue time was not captured counts it as zero (``missing_wait="zero"``). A row with
    no engine time falls back to wall clock; never longer than the client's wall clock.
    """
    from .engine_time import engine_time_sql

    engine = (
        f"(CASE WHEN {alias}.server_time_ms > 0 "
        f"THEN {engine_time_sql(alias, missing_wait='zero')} "
        f"ELSE coalesce({alias}.wall_clock_ms, 0) END)"
    )
    return f"least({engine}, coalesce(nullif({alias}.wall_clock_ms, 0), {engine}))"


def _run_span_columns() -> str:
    """``start_ms``/``end_ms`` of the time a row held the compute, on the harness clock.

    ``created_at`` is recorded as the statement returns; the hold is its engine time
    (:func:`_engine_ms_sql`) ending then. Anchoring at the return shifts each interval
    by its result fetch, which moves overlaps a little but leaves every length exact.
    """
    return (
        f"epoch_ms(r.created_at) AS end_ms, epoch_ms(r.created_at) - {_engine_ms_sql()} AS start_ms"
    )


def fabric_window_pricing_sql(
    usd_per_hour: dict[str, float], cu_seconds_per_cpu_second: float
) -> str:
    """SELECT pricing every labelled Fabric row from the capacity window it ran in.

    Takes two parameter pairs, ``(scale_factor, scale_factor, run_type, run_type)``,
    each ``NULL`` for all. Returns one row per priced result: ``run_id``,
    ``run_type``, ``sku``, ``window_id``, ``before_usd``, ``after_usd``, ``basis``
    and ``engine_timed`` (a successful row with engine time, which the pass stamps
    ``proportional``). See :func:`recost_fabric_windows` for the model.

    Args:
        usd_per_hour: SKU list rate by lower-case SKU name (``f2`` ... ``f128``). A
            SKU's capacity units are its number: F16 holds 16 CUs.
        cu_seconds_per_cpu_second: CU-seconds billed per CPU-second consumed.
    """
    rates = ", ".join(
        f"('{sku.upper()}', {int(sku[1:])}, {float(rate)!r})"
        for sku, rate in sorted(usd_per_hour.items())
    )
    shared = ", ".join(f"'{rt}'" for rt in SHARED_RUN_TYPES)
    return f"""
        WITH rates(sku, cu, usd_per_hour) AS (VALUES {rates}),
        keyed AS (
            SELECT r.run_id, r.run_type, k.sku, k.cu, k.usd_per_hour,
                   r.error_message IS NULL AS ok,
                   r.server_time_ms IS NOT NULL AS has_engine_time,
                   {_engine_ms_sql()} AS elapsed_ms,
                   TRY_CAST(json_extract_string(r.metadata, '$.cpu_ms') AS DOUBLE) AS cpu_ms,
                   r.run_type IN ({shared}) AND r.provenance_id IS NOT NULL AS shared,
                   CASE WHEN r.run_type IN ({shared}) AND r.provenance_id IS NOT NULL
                        THEN concat_ws('|', r.provenance_id, r.run_type, r.concurrency,
                                       r.scale_factor, k.sku)
                        ELSE CAST(r.run_id AS VARCHAR) END AS window_key,
                   {_run_span_columns()},
                   r.cost_usd AS before_usd
            FROM sql_benchmark_results r
            JOIN rates k ON upper(r.compute_label) = k.sku
            WHERE r.platform = 'fabric'
              AND (? IS NULL OR r.scale_factor = ?) AND (? IS NULL OR r.run_type = ?)
        ),
        {_window_sessions("keyed", "base")},
        win AS (
            SELECT window_id, any_value(cu) AS cu, any_value(usd_per_hour) AS usd_per_hour,
                   bool_or(shared) AS shared,
                   CASE WHEN bool_or(shared) THEN (max(end_ms) - min(start_ms)) / 1000.0
                        ELSE sum(elapsed_ms) / 1000.0 END AS span_s,
                   coalesce(sum(cpu_ms), 0) / 1000.0 AS cpu_s,
                   count(*) FILTER (WHERE ok) AS n_ok,
                   count(*) FILTER (WHERE ok AND cpu_ms IS NULL) AS ok_without_cpu,
                   coalesce(sum(cpu_ms) FILTER (WHERE ok), 0) AS ok_cpu_ms,
                   coalesce(sum(elapsed_ms) FILTER (WHERE ok), 0) AS ok_elapsed_ms
            FROM base GROUP BY window_id
        ),
        priced AS (
            SELECT *, n_ok > 0 AND ok_without_cpu = 0 AND ok_cpu_ms > 0 AS by_cpu
            FROM win
        ),
        billed AS (
            SELECT *,
                   usd_per_hour / cu / 3600.0
                   * CASE WHEN by_cpu
                          THEN greatest(cu * span_s, {float(cu_seconds_per_cpu_second)!r} * cpu_s)
                          ELSE cu * span_s END AS window_usd
            FROM priced
        )
        SELECT b.run_id, b.run_type, b.sku, b.window_id, b.before_usd,
               CASE WHEN NOT b.ok THEN 0.0
                    WHEN w.by_cpu THEN w.window_usd * b.cpu_ms / w.ok_cpu_ms
                    WHEN w.ok_elapsed_ms > 0 THEN w.window_usd * b.elapsed_ms / w.ok_elapsed_ms
                    ELSE 0.0 END AS after_usd,
               CASE WHEN NOT w.shared THEN 'query'
                    WHEN w.by_cpu THEN 'window_cpu_share'
                    ELSE 'window_time_share' END AS basis,
               b.ok AND b.has_engine_time AS engine_timed
        FROM base b JOIN billed w USING (window_id)
    """


def recost_fabric_windows(
    results_db: ResultsDB,
    *,
    scale_factor: int | None = None,
    run_type: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Price Fabric rows at what the capacity bills, split across the statements sharing it.

    **Why.** Pricing each statement at its own elapsed time × its SKU's full hourly
    rate is right for a warm statement, which has the capacity to itself, and wrong
    under concurrency: a 100-session burst holds one capacity, and charging each
    statement the whole capacity for its own elapsed time bills that capacity once
    per statement in flight, and gives a pass's idle stretches to its statements
    too. Priced that way, one round of concurrency rows comes to $483, several times
    the capacity's whole bill for those days, against $35.71 by window. Each row is
    priced from its own label, never one flat rate across the 64× SKU ladder.

    **The model** (``cost/fabric.py`` has the billing facts):

    * A window is a stretch in which at least one statement of one invocation
      (``provenance_id``) at one level of a concurrency or sustained pass held the
      capacity for its engine time (:func:`_run_span_columns`), or one warm/cold
      statement on its own. A statement that starts after every earlier one has
      ended opens the next (:data:`IDLE_GAP_MS`), so reconnects and client
      turnaround, which can run to an hour in one pass, are not query cost. A
      timed-out statement's own 600 s stay in its window, and
      whatever it ran on after we gave up is in its CPU time.
    * Window cost = SKU rate × max(span, CU-seconds consumed / SKU CUs). The
      CU-seconds are every statement's ``metadata.cpu_ms`` (failed ones included:
      they used the capacity too) × ``CU_SECONDS_PER_CPU_SECOND``, counted only when
      every successful statement has one.
    * The cost goes to the window's successful rows by CPU share, or by elapsed
      share (engine time, else wall clock) when CPU time is missing, which prices SKU
      time only. Failed rows cost 0, the collector's contract, so a cell's total is
      the window's cost whatever the report filters.
    * Successful rows with engine time are stamped ``proportional``. Rows without it
      keep their tier: their weight is harness time. ``metadata.cost_basis`` records
      ``window_cpu_share``, ``window_time_share`` or ``query``.
    * Rows with **no label** cannot be size-priced: their cost is voided to NULL
      (the original kept under ``metadata.cost_usd_voided``), since a price at a
      guessed rate is a wrong number wearing a confidence tier.

    Idempotent: every price recomputes from immutable per-row inputs.

    Args:
        results_db: Results database, updated in place unless ``dry_run``.
        scale_factor: Restrict to one scale (windows never span scales), or None.
        run_type: Restrict to one run type, or None.
        dry_run: Compute and return the summary without writing.

    Returns:
        Summary with per-(run type, SKU, basis) row counts, windows and before/after
        totals.
    """
    from ..cost.fabric import CU_SECONDS_PER_CPU_SECOND, FabricCostCollector

    sql = fabric_window_pricing_sql(
        FabricCostCollector.CAPACITY_USD_PER_HOUR, CU_SECONDS_PER_CPU_SECOND
    )
    params = [scale_factor, scale_factor, run_type, run_type]
    with results_db.connect() as con:
        con.execute(f"CREATE TEMP TABLE fabric_priced AS {sql}", params)
        cells = con.execute(
            """
            SELECT run_type, sku, basis, count(*), count(DISTINCT window_id),
                   coalesce(sum(before_usd), 0), coalesce(sum(after_usd), 0)
            FROM fabric_priced GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
            """
        ).fetchall()
        unpriced = con.execute(
            """
            SELECT count(*) FROM sql_benchmark_results
            WHERE platform = 'fabric' AND compute_label IS NOT NULL
              AND (? IS NULL OR scale_factor = ?) AND (? IS NULL OR run_type = ?)
              AND run_id NOT IN (SELECT run_id FROM fabric_priced)
            """,
            params,
        ).fetchone()[0]
        if unpriced:
            logger.warning("fabric: %d labelled rows have no list rate; left as-is", unpriced)
        if not dry_run:
            con.execute(
                """
                UPDATE sql_benchmark_results AS t
                SET cost_usd = p.after_usd,
                    cost_confidence = CASE WHEN p.engine_timed THEN 'proportional'
                                           ELSE t.cost_confidence END,
                    metadata = json_merge_patch(
                        coalesce(t.metadata, '{}'), json_object('cost_basis', p.basis))
                FROM fabric_priced AS p
                WHERE t.run_id = p.run_id
                """
            )
        unlabelled = con.execute(
            """
            SELECT count(*), coalesce(sum(cost_usd), 0) FROM sql_benchmark_results
            WHERE platform = 'fabric' AND compute_label IS NULL AND cost_usd IS NOT NULL
              AND (? IS NULL OR scale_factor = ?) AND (? IS NULL OR run_type = ?)
            """,
            params,
        ).fetchone()
        if unlabelled[0] and not dry_run:
            con.execute(
                """
                UPDATE sql_benchmark_results
                SET metadata = json_merge_patch(
                        coalesce(metadata, '{}'),
                        json_object(
                            'cost_usd_voided', cost_usd,
                            'cost_void_reason',
                            'no compute size: a fabric capacity price cannot be set')),
                    cost_usd = NULL, cost_confidence = NULL
                WHERE platform = 'fabric' AND compute_label IS NULL AND cost_usd IS NOT NULL
                  AND (? IS NULL OR scale_factor = ?) AND (? IS NULL OR run_type = ?)
                """,
                params,
            )
        con.execute("DROP TABLE fabric_priced")

    changes = [
        {
            "run_type": rt,
            "compute_label": sku,
            "basis": basis,
            "rows": n,
            "windows": windows,
            "before_usd": round(before, 4),
            "after_usd": round(after, 4),
        }
        for rt, sku, basis, n, windows, before, after in cells
    ]
    total_before = round(sum(c["before_usd"] for c in changes), 4)
    total_after = round(sum(c["after_usd"] for c in changes), 4)
    logger.info(
        "fabric window re-cost%s: %d cells, $%.2f -> $%.2f; %d unlabelled rows ($%.2f) voided",
        " (dry run)" if dry_run else "",
        len(changes),
        total_before,
        total_after,
        unlabelled[0],
        unlabelled[1],
    )
    return {
        "cells": changes,
        "total_before_usd": total_before,
        "total_after_usd": total_after,
        "unlabelled_rows_voided": int(unlabelled[0]),
        "unlabelled_usd_voided": round(unlabelled[1], 4),
        "dry_run": dry_run,
    }


#: Rate-modelled engines (``estimated``, engine time x the size's rate) whose shared-load
#: rows are window-priced: ClickHouse Cloud bills a service's compute units while it
#: runs, a MotherDuck Duckling bills per second while it is warm, and a Snowflake
#: warehouse bills its credits per second while it is resumed
#: (:func:`price_snowflake_on_engine_time` has why Snowflake is rate-priced).
RATE_WINDOW_PLATFORMS = ("clickhouse", "motherduck", "snowflake")

#: ``(platform, lower-case label)`` sizes that genuinely bill per query, which keep the
#: per-query price: MotherDuck Pulse bills compute units per query.
PER_QUERY_BILLED_LABELS = frozenset({("motherduck", "pulse")})


def rate_window_pricing_sql(rates: list[tuple[str, str, float]]) -> str:
    """SELECT pricing rate-modelled shared-load rows from the window they ran in.

    Takes one parameter pair, ``(scale_factor, scale_factor)``, ``NULL`` for all.
    Returns ``run_id``, ``platform``, ``compute_label``, ``run_type``, ``window_id``,
    ``before_usd`` and ``after_usd``. A window is one pass's rows at one level on one
    size, cut at idle gaps (:func:`_window_sessions`); it costs the size's rate × its
    span, split across its successful rows by elapsed time (engine time, else wall
    clock). Failed rows cost 0.

    Args:
        rates: ``(platform, compute_label, usd_per_ms)`` for each size to price.
    """
    values = ", ".join(
        "('{}', '{}', {!r})".format(p, label.replace("'", "''"), float(rate))
        for p, label, rate in rates
    )
    shared = ", ".join(f"'{rt}'" for rt in SHARED_RUN_TYPES)
    return f"""
        WITH rates(platform, compute_label, usd_per_ms) AS (VALUES {values}),
        keyed AS (
            SELECT r.run_id, r.platform, r.compute_label, r.run_type, k.usd_per_ms,
                   r.error_message IS NULL AS ok,
                   {_engine_ms_sql()} AS elapsed_ms,
                   concat_ws('|', r.platform, r.compute_label, r.scale_factor, r.run_type,
                             r.concurrency, coalesce(CAST(r.provenance_id AS VARCHAR), ''))
                       AS window_key,
                   {_run_span_columns()},
                   r.cost_usd AS before_usd
            FROM sql_benchmark_results r
            JOIN rates k ON r.platform = k.platform AND r.compute_label = k.compute_label
            WHERE r.run_type IN ({shared}) AND r.cost_confidence = 'estimated'
              AND (? IS NULL OR r.scale_factor = ?)
        ),
        {_window_sessions("keyed", "base")},
        win AS (
            SELECT window_id, any_value(usd_per_ms) AS usd_per_ms,
                   max(end_ms) - min(start_ms) AS span_ms,
                   coalesce(sum(elapsed_ms) FILTER (WHERE ok), 0) AS ok_elapsed_ms
            FROM base GROUP BY window_id
        )
        SELECT b.run_id, b.platform, b.compute_label, b.run_type, b.window_id, b.before_usd,
               CASE WHEN b.ok AND w.ok_elapsed_ms > 0
                    THEN w.usd_per_ms * w.span_ms * b.elapsed_ms / w.ok_elapsed_ms
                    ELSE 0.0 END AS after_usd
        FROM base b JOIN win w USING (window_id)
    """


def recost_capacity_windows(
    results_db: ResultsDB,
    cost_config: dict[str, Any],
    *,
    platforms: tuple[str, ...] = RATE_WINDOW_PLATFORMS,
    scale_factor: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Price ClickHouse, MotherDuck and Snowflake shared-load rows as windows of compute.

    **Why.** These engines' rows are rate-modelled: elapsed time × the size's hourly
    rate, a fair price for a query that has the compute to itself. Under a burst,
    that charges every query in flight the whole service for its own elapsed time,
    so a level's cost grows with its concurrency rather than with the time the
    compute ran: against list rate × each window's span, per-query pricing puts
    ClickHouse's concurrency cost at 0.6× at one session to 5.4× at fifty ($462,
    about $191 by window), and MotherDuck's per-second Ducklings at up to 23× (about
    $763 against $195).

    **The model.** Concurrency and sustained rows only, at ``estimated`` confidence
    (all of these engines' rows). A window is one pass's rows at one level on one
    size (``provenance_id`` where recorded), while at least one of them held the
    service (:func:`_run_span_columns`, :data:`IDLE_GAP_MS`); it costs the size's rate
    (:func:`_rate_per_ms`, the same rate the per-query repair uses) × its span, and
    its successful rows share that by engine time. Failed rows cost 0.
    :data:`PER_QUERY_BILLED_LABELS` keep their per-query price. The Duckling
    cooldown that bills after a window's last query is idle time, and like every
    engine's idle time it is not in ``cost_usd``. ``metadata.cost_basis`` records
    ``window_time_share``; the tier stays ``estimated``.

    Idempotent: every price recomputes from immutable per-row inputs.

    Args:
        results_db: Results database, updated in place unless ``dry_run``.
        cost_config: Captured cost config, supplying the per-size rate entries.
        platforms: Rate-modelled platforms to price (a subset of
            :data:`RATE_WINDOW_PLATFORMS`).
        scale_factor: Restrict to one scale, or None for all.
        dry_run: Compute and return the summary without writing.

    Returns:
        Summary with per-(platform, size, run type) row and window counts and
        before/after totals.
    """
    from ..cost.discovery import collector_for

    wanted = [p for p in platforms if p in RATE_WINDOW_PLATFORMS]
    if not wanted:
        return {"cells": [], "total_before_usd": 0.0, "total_after_usd": 0.0, "dry_run": dry_run}
    shared = ", ".join(f"'{rt}'" for rt in SHARED_RUN_TYPES)
    with results_db.connect() as con:
        labels = con.execute(
            f"""
            SELECT DISTINCT platform, compute_label FROM sql_benchmark_results
            WHERE platform IN ({", ".join(f"'{p}'" for p in wanted)})
              AND run_type IN ({shared}) AND cost_confidence = 'estimated'
              AND compute_label IS NOT NULL AND (? IS NULL OR scale_factor = ?)
            ORDER BY 1, 2
            """,
            [scale_factor, scale_factor],
        ).fetchall()
        rates: list[tuple[str, str, float]] = []
        for platform, label in labels:
            if (platform, label.lower()) in PER_QUERY_BILLED_LABELS:
                continue
            try:
                rates.append(
                    (platform, label, _rate_per_ms(platform, label, cost_config, collector_for))
                )
            except ValueError:
                logger.warning(
                    "%s label %r has no rate; its shared-load rows are left", platform, label
                )
        if not rates:
            return {
                "cells": [],
                "total_before_usd": 0.0,
                "total_after_usd": 0.0,
                "dry_run": dry_run,
            }
        con.execute(
            f"CREATE TEMP TABLE rate_windows AS {rate_window_pricing_sql(rates)}",
            [scale_factor, scale_factor],
        )
        cells = con.execute(
            """
            SELECT platform, compute_label, run_type, count(*), count(DISTINCT window_id),
                   coalesce(sum(before_usd), 0), coalesce(sum(after_usd), 0)
            FROM rate_windows GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
            """
        ).fetchall()
        if not dry_run:
            con.execute(
                """
                UPDATE sql_benchmark_results AS t
                SET cost_usd = p.after_usd,
                    metadata = json_merge_patch(
                        coalesce(t.metadata, '{}'), json_object('cost_basis', 'window_time_share'))
                FROM rate_windows AS p
                WHERE t.run_id = p.run_id
                """
            )
        con.execute("DROP TABLE rate_windows")

    changes = [
        {
            "platform": platform,
            "compute_label": label,
            "run_type": rt,
            "rows": n,
            "windows": windows,
            "before_usd": round(before, 4),
            "after_usd": round(after, 4),
        }
        for platform, label, rt, n, windows, before, after in cells
    ]
    total_before = round(sum(c["before_usd"] for c in changes), 4)
    total_after = round(sum(c["after_usd"] for c in changes), 4)
    logger.info(
        "rate-window re-cost%s: %d cells, $%.2f -> $%.2f",
        " (dry run)" if dry_run else "",
        len(changes),
        total_before,
        total_after,
    )
    return {
        "cells": changes,
        "total_before_usd": total_before,
        "total_after_usd": total_after,
        "dry_run": dry_run,
    }


def databricks_hour_resplit_sql(usd_per_hour: dict[str, float]) -> str:
    """SELECT re-splitting each Databricks billing hour across the windows that ran in it.

    Returns ``run_id``, ``run_type``, ``compute_label``, ``before_usd``, ``billed_usd``
    (the row's share as ``reconcile`` allocated it) and ``after_usd``. See
    :func:`resplit_databricks_hours` for the model.

    Args:
        usd_per_hour: list $/hour by compute label (``2X-Small`` ... ``Large``). Rows
            whose label is missing are left out and keep their billed share.
    """
    shared = ", ".join(f"'{rt}'" for rt in SHARED_RUN_TYPES)
    rates = ", ".join(
        "('{}', {!r})".format(label.replace("'", "''"), float(rate))
        for label, rate in sorted(usd_per_hour.items())
    )
    return f"""
        WITH rates(compute_label, usd_per_hour) AS (VALUES {rates}),
        keyed AS (
            SELECT r.run_id, r.run_type, r.compute_label, k.usd_per_hour,
                   r.error_message IS NULL AS ok,
                   {_exec_ms_sql()} AS exec_ms,
                   coalesce(TRY_CAST(json_extract_string(r.metadata, '$.billed_share_usd')
                                     AS DOUBLE), r.cost_usd, 0) AS billed_usd,
                   CASE WHEN r.run_type IN ({shared})
                        THEN concat_ws('|', r.compute_label, r.scale_factor, r.run_type,
                                       r.concurrency,
                                       coalesce(CAST(r.provenance_id AS VARCHAR), ''))
                        ELSE CAST(r.run_id AS VARCHAR) END AS window_key,
                   {_run_span_columns()},
                   date_trunc('hour', r.created_at - to_milliseconds(
                       CAST(round(coalesce(r.wall_clock_ms, 0)) AS BIGINT))) AS hr,
                   r.cost_usd AS before_usd
            FROM sql_benchmark_results r
            JOIN rates k ON r.compute_label = k.compute_label
            WHERE r.platform = 'databricks' AND r.cost_confidence = 'proportional'
        ),
        {_window_sessions("keyed", "base")},
        seg AS (
            SELECT hr, window_id, any_value(usd_per_hour) AS usd_per_hour,
                   max(end_ms) - min(start_ms) AS seg_ms,
                   coalesce(sum(exec_ms) FILTER (WHERE ok), 0) AS ok_exec_ms
            FROM base GROUP BY hr, window_id
        ),
        hour_weight AS (
            SELECT hr, sum(seg_ms * usd_per_hour) FILTER (WHERE ok_exec_ms > 0) AS hour_w
            FROM seg GROUP BY hr
        ),
        pot AS (SELECT hr, sum(billed_usd) AS pot_usd FROM base GROUP BY hr)
        SELECT b.run_id, b.run_type, b.compute_label, b.before_usd, b.billed_usd,
               CASE WHEN h.hour_w IS NULL OR h.hour_w <= 0 THEN b.billed_usd
                    WHEN NOT b.ok OR s.ok_exec_ms <= 0 THEN 0.0
                    ELSE least(p.pot_usd * (s.seg_ms * s.usd_per_hour) / h.hour_w,
                               s.seg_ms * s.usd_per_hour / {MS_PER_HOUR})
                         * b.exec_ms / s.ok_exec_ms
               END AS after_usd
        FROM base b
        JOIN seg s USING (hr, window_id)
        JOIN hour_weight h USING (hr)
        JOIN pot p USING (hr)
    """


def resplit_databricks_hours(results_db: ResultsDB, *, dry_run: bool = False) -> dict[str, Any]:
    """Split each billed Databricks hour across its windows by list rate x time, then execution.

    **The skew this repairs.** ``reconcile`` prices a Databricks statement from its
    warehouse's hourly bill, split by the statement's share of that hour's execution
    time. Every hour's total is billing-true, but that split is wrong twice over:

    * Under concurrency a burst's statements run overlapped, so a 100-session burst
      holds a hundred times the execution time of a one-session pass that ran as
      long: cost moves from low levels to high ones (0.4-0.6x list rate x window
      time at one session, 1.3-3.2x at five and up).
    * Sizes that share a billed hour (a resized warehouse running two sizes' passes
      back to back) are split by execution time alone, as if a second of Large cost
      what a second of 2X-Small does. At 1 and 10 GB, where passes are short, a
      size's cost runs from 0.55x to 5.3x its list rate; at 1 GB Small comes out at
      0.29x 2X-Small's cost against 3x in list price.

    **The model.** For each billing hour (hours from the harness's submit time,
    ``created_at - wall_clock_ms``; the harness's zone is a whole number of hours
    from UTC, so its hours are the billing hours, and a statement within about a
    second of the hour can land in the neighbouring one): the pot is the sum of the
    hour's rows' billed shares, across every size, exactly as ``reconcile``
    allocated them. It goes to the windows that ran in the hour by the time each ran
    x its size's list $/hour (``report.compute_rates``), then within a window by
    execution time. A window is one pass's rows at one level, cut at idle gaps; a
    warm or cold statement is its own. Failed rows cost 0, their share going to the
    successful rows; an hour with none keeps its rows' billed shares, and so does
    any row whose size has no list rate. Each row's billed share is kept under
    ``metadata.billed_share_usd`` (``reconcile`` writes it there) so a re-run starts
    from it, and ``metadata.cost_basis`` records ``hour_window_share``. Only
    ``proportional`` rows take part: the few ``estimated`` rows lack a billed share.

    Idempotent: prices recompute from the stored billed shares.
    """
    from ..report.compute_rates import effective_usd_per_hour

    with results_db.connect() as con:
        labels = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT compute_label FROM sql_benchmark_results WHERE platform ="
                " 'databricks' AND cost_confidence = 'proportional' AND compute_label IS NOT NULL"
            ).fetchall()
        ]
        rates = {}
        for label in labels:
            rate = effective_usd_per_hour("databricks", label)
            if rate:
                rates[label] = rate
            else:
                logger.warning(
                    "databricks label %r has no list rate; its rows keep their share", label
                )
        if not rates:
            return {
                "cells": [],
                "total_before_usd": 0.0,
                "total_after_usd": 0.0,
                "dry_run": dry_run,
            }
        con.execute(f"CREATE TEMP TABLE dbx_resplit AS {databricks_hour_resplit_sql(rates)}")
        cells = con.execute(
            """
            SELECT run_type, compute_label, count(*), coalesce(sum(before_usd), 0),
                   coalesce(sum(billed_usd), 0), coalesce(sum(after_usd), 0)
            FROM dbx_resplit GROUP BY 1, 2 ORDER BY 1, 2
            """
        ).fetchall()
        if not dry_run:
            con.execute(
                """
                UPDATE sql_benchmark_results AS t
                SET cost_usd = p.after_usd,
                    metadata = json_merge_patch(
                        coalesce(t.metadata, '{}'),
                        json_object('billed_share_usd', p.billed_usd,
                                    'cost_basis', 'hour_window_share'))
                FROM dbx_resplit AS p
                WHERE t.run_id = p.run_id
                """
            )
        con.execute("DROP TABLE dbx_resplit")

    changes = [
        {
            "run_type": rt,
            "compute_label": label,
            "rows": n,
            "before_usd": round(before, 4),
            "billed_usd": round(billed, 4),
            "after_usd": round(after, 4),
        }
        for rt, label, n, before, billed, after in cells
    ]
    total_before = round(sum(c["before_usd"] for c in changes), 4)
    total_after = round(sum(c["after_usd"] for c in changes), 4)
    logger.info(
        "databricks hour re-split%s: %d cells, $%.2f -> $%.2f (billed $%.2f)",
        " (dry run)" if dry_run else "",
        len(changes),
        total_before,
        total_after,
        sum(c["billed_usd"] for c in changes),
    )
    return {
        "cells": changes,
        "total_before_usd": total_before,
        "total_after_usd": total_after,
        "dry_run": dry_run,
    }


# ── one basis for capacity engines: list rate x the time compute was held ─────
#
# Redshift and BigQuery reservations bill capacity while it is held, like every engine
# above. A single-user statement is its own window (its engine time); a pass under load
# is priced by window, while a query ran. Redshift keeps its billed share, capped at
# that capacity-time (the idle seconds between the harness's queries leave query cost);
# BigQuery prices the reservation baseline held.


def capacity_time_sql(
    platform: str,
    usd_per_hour: dict[str, float],
    *,
    mode: str,
    where: str = "",
    share_sql: str | None = None,
    unbilled_sql: str | None = None,
) -> str:
    """SELECT pricing ``platform``'s labelled rows at list rate x the time they held compute.

    ``mode="price"`` sets each window to rate x span; ``mode="cap"`` keeps the window's
    billed total (``metadata.billed_share_usd``, else ``cost_usd``) up to rate x span.
    A row ``unbilled_sql`` marks has no billed share to keep (its billing is gone): it
    enters the window's billed total at rate x its engine time, so a window of such
    rows costs rate x span and one mixed with billed rows is capped as usual.
    Either way the window's cost goes to its successful rows by ``share_sql`` (a
    resource measure, e.g. slot time) where every successful row of the window has
    it, else by execution time (``server_time_ms``, else wall-clock); failed rows
    cost 0. The span is the time a query held the compute (:func:`_run_span_columns`).
    Returns ``run_id``, ``run_type``, ``compute_label``, ``before_usd``,
    ``billed_usd``, ``kept_billed_usd`` (``billed_usd``, NULL for an unbilled row, so
    its stand-in is never recorded as billing), ``after_usd``, ``capped`` (the cap
    bound), ``by_share`` and ``unbilled``.

    Args:
        platform: Engine id.
        usd_per_hour: List $/hour by compute label; other labels are left out.
        mode: ``"price"`` or ``"cap"``.
        where: Extra ``AND ...`` filter on ``r.`` columns.
        share_sql: SQL over ``r.`` giving a row's resource share, or ``None``.
        unbilled_sql: SQL predicate over ``r.`` marking rows with no billed share.
    """
    if mode not in ("price", "cap"):
        raise ValueError(f"mode must be price or cap, not {mode!r}")
    rates = ", ".join(
        "('{}', {!r})".format(label.replace("'", "''"), float(rate))
        for label, rate in sorted(usd_per_hour.items())
    )
    shared = ", ".join(f"'{rt}'" for rt in SHARED_RUN_TYPES)
    window_usd = (
        "w.usd_per_ms * w.span_ms"
        if mode == "price"
        else "least(w.billed_total, w.usd_per_ms * w.span_ms)"
    )
    return f"""
        WITH rates(compute_label, usd_per_hour) AS (VALUES {rates}),
        keyed AS (
            SELECT r.run_id, r.run_type, r.compute_label,
                   k.usd_per_hour / {MS_PER_HOUR} AS usd_per_ms,
                   r.error_message IS NULL AS ok,
                   {_exec_ms_sql()} AS exec_ms,
                   {share_sql or "CAST(NULL AS DOUBLE)"} AS share,
                   coalesce({unbilled_sql or "false"}, false) AS unbilled,
                   CASE WHEN coalesce({unbilled_sql or "false"}, false)
                        THEN k.usd_per_hour / {MS_PER_HOUR} * {_engine_ms_sql()}
                        ELSE coalesce(TRY_CAST(json_extract_string(
                                 r.metadata, '$.billed_share_usd') AS DOUBLE), r.cost_usd, 0)
                   END AS billed_usd,
                   CASE WHEN r.run_type IN ({shared})
                        THEN concat_ws('|', r.compute_label, r.scale_factor, r.run_type,
                                       r.concurrency,
                                       coalesce(CAST(r.provenance_id AS VARCHAR), ''))
                        ELSE CAST(r.run_id AS VARCHAR) END AS window_key,
                   {_run_span_columns()},
                   r.cost_usd AS before_usd
            FROM sql_benchmark_results r
            JOIN rates k ON r.compute_label = k.compute_label
            WHERE r.platform = '{platform}' {where}
        ),
        {_window_sessions("keyed", "base")},
        win AS (
            SELECT window_id, any_value(usd_per_ms) AS usd_per_ms,
                   max(end_ms) - min(start_ms) AS span_ms,
                   coalesce(sum(billed_usd), 0) AS billed_total,
                   coalesce(bool_and(coalesce(share, 0) > 0) FILTER (WHERE ok), false) AS by_share,
                   coalesce(sum(share) FILTER (WHERE ok), 0) AS ok_share,
                   coalesce(sum(exec_ms) FILTER (WHERE ok), 0) AS ok_exec_ms
            FROM base GROUP BY window_id
        ),
        weighed AS (
            SELECT b.*, w.usd_per_ms, w.span_ms, w.billed_total, w.by_share,
                   CASE WHEN w.by_share THEN b.share ELSE b.exec_ms END AS weight,
                   CASE WHEN w.by_share THEN w.ok_share ELSE w.ok_exec_ms END AS ok_weight
            FROM base b JOIN win w USING (window_id)
        )
        SELECT w.run_id, w.run_type, w.compute_label, w.before_usd, w.billed_usd,
               CASE WHEN w.unbilled THEN NULL ELSE w.billed_usd END AS kept_billed_usd,
               CASE WHEN NOT w.ok OR w.ok_weight <= 0 THEN 0.0
                    ELSE {window_usd} * w.weight / w.ok_weight END AS after_usd,
               w.billed_total > w.usd_per_ms * w.span_ms AS capped, w.by_share, w.unbilled
        FROM weighed w
    """


def _apply_priced(
    con: Any, table: str, basis: str, *, keep: str | None, confidence: str | None
) -> None:
    """Write ``table``'s ``after_usd`` onto its rows, stamping ``metadata.cost_basis`` and,
    when ``keep`` names a metadata key, the row's ``billed_usd`` under it."""
    keep_sql = f", '{keep}', p.kept_billed_usd" if keep else ""
    conf_sql = f", cost_confidence = '{confidence}'" if confidence else ""
    con.execute(
        f"""
        UPDATE sql_benchmark_results AS t
        SET cost_usd = p.after_usd{conf_sql},
            metadata = json_merge_patch(coalesce(t.metadata, '{{}}'),
                                        json_object('cost_basis', '{basis}'{keep_sql}))
        FROM {table} AS p
        WHERE t.run_id = p.run_id
        """
    )


def _summarize(con: Any, table: str, label: str, dry_run: bool) -> dict[str, Any]:
    cells = con.execute(
        f"""
        SELECT run_type, compute_label, count(*), coalesce(sum(before_usd), 0),
               coalesce(sum(after_usd), 0)
        FROM {table} GROUP BY 1, 2 ORDER BY 1, 2
        """
    ).fetchall()
    changes = [
        {
            "run_type": rt,
            "compute_label": lbl,
            "rows": n,
            "before_usd": round(before, 4),
            "after_usd": round(after, 4),
        }
        for rt, lbl, n, before, after in cells
    ]
    total_before = round(sum(c["before_usd"] for c in changes), 4)
    total_after = round(sum(c["after_usd"] for c in changes), 4)
    logger.info(
        "%s%s: %d cells, $%.2f -> $%.2f",
        label,
        " (dry run)" if dry_run else "",
        len(changes),
        total_before,
        total_after,
    )
    return {
        "cells": changes,
        "total_before_usd": total_before,
        "total_after_usd": total_after,
        "dry_run": dry_run,
    }


def _label_rates(con: Any, platform: str, where: str = "") -> dict[str, float]:
    from ..report.compute_rates import effective_usd_per_hour

    rates: dict[str, float] = {}
    for (label,) in con.execute(
        f"SELECT DISTINCT compute_label FROM sql_benchmark_results r"
        f" WHERE r.platform = '{platform}' AND r.compute_label IS NOT NULL {where}"
    ).fetchall():
        rate = effective_usd_per_hour(platform, label)
        if rate:
            rates[label] = rate
        else:
            logger.warning(
                "%s label %r has no list rate; its rows are left as they are", platform, label
            )
    return rates


#: Redshift rows priced from billed RPU-seconds: reconciled from billing, or voided
#: (an estimate that carried a per-query 60 s floor, cleared after the 7-day billing
#: window closed; :func:`clear_fabricated_redshift_estimates`) and priced at RPU rate
#: x engine time instead.
_REDSHIFT_VOIDED = "json_extract(r.metadata, '$.voided_cost_usd') IS NOT NULL"
_REDSHIFT_CAPPED = f"AND (r.cost_confidence = 'proportional' OR {_REDSHIFT_VOIDED})"
_REDSHIFT_UNBILLED = (
    f"({_REDSHIFT_VOIDED} AND json_extract(r.metadata, '$.billed_share_usd') IS NULL)"
)


def cap_redshift_idle(results_db: ResultsDB, *, dry_run: bool = False) -> dict[str, Any]:
    """Cap Redshift's billed cost at RPU rate x the time each query or burst held it.

    **The skew this repairs.** ``reconcile`` splits ``SYS_SERVERLESS_USAGE``'s billed
    RPU-seconds across the queries that overlapped each usage minute. The workgroup
    bills every second it is active, and in a single-user pass the harness spends
    more time between queries (fetching and recording results) than the queries
    take at small scales: a pass runs 6-12x its queries' summed wall time at 1 GB,
    5-8x at 10 GB, 3x at 100 GB and 1.7x at 1 TB, against 1.4-2.8x on the other
    engines. Uncapped, those idle seconds become query cost, and Redshift reads
    1.7-12x its list rate x query time.

    **The model.** A single-user statement is its own window (its wall-clock time);
    under load a window is one pass's rows at one level, cut at idle gaps
    (:func:`capacity_time_sql`). A window costs its billed total, capped at the RPU
    rate x its span, split across its successful rows by execution time (Redshift's
    ``execution_time``, which leaves out queue and result fetch); failed rows cost
    0. Idle between windows leaves query cost, as on every engine. Each row's billed
    share is kept under ``metadata.billed_share_usd`` (reconcile writes it there)
    and ``metadata.cost_basis`` records ``capped_capacity_time``; the tier stays
    ``proportional``. Idempotent: prices recompute from the billed shares.

    **Rows with no billing.** 188 successful rows have voided estimates
    (:func:`clear_fabricated_redshift_estimates`) and a closed billing window. They
    carry engine time, so they are priced on the same rule, RPU rate x the time they
    held the workgroup (``unbilled_sql`` in :func:`capacity_time_sql`: in a window with
    billed rows they share its cap), and stamped ``estimated`` since no bill backs
    them. Their voided figure stays under ``metadata.voided_cost_usd``.
    """
    with results_db.connect() as con:
        rates = _label_rates(con, "redshift", _REDSHIFT_CAPPED)
        if not rates:
            return {
                "cells": [],
                "total_before_usd": 0.0,
                "total_after_usd": 0.0,
                "dry_run": dry_run,
            }
        sql = capacity_time_sql(
            "redshift",
            rates,
            mode="cap",
            where=_REDSHIFT_CAPPED,
            unbilled_sql=_REDSHIFT_UNBILLED,
        )
        con.execute(f"CREATE TEMP TABLE redshift_capped AS {sql}")
        summary = _summarize(con, "redshift_capped", "redshift idle cap", dry_run)
        if not dry_run:
            _apply_priced(
                con,
                "redshift_capped",
                "capped_capacity_time",
                keep="billed_share_usd",
                confidence=None,
            )
            con.execute(
                """
                UPDATE sql_benchmark_results AS t SET cost_confidence = 'estimated'
                FROM redshift_capped AS p WHERE t.run_id = p.run_id AND p.unbilled
                """
            )
        con.execute("DROP TABLE redshift_capped")
    return summary


#: BigQuery rows priced on the reservation held: every reservation-labelled row.
_BIGQUERY_RESERVATION = f"AND r.compute_label LIKE '{RESERVATION_LABEL_PATTERN}'"
#: A BigQuery row's share of its window: the slot time it used.
_BIGQUERY_SLOT_MS = "TRY_CAST(json_extract_string(r.metadata, '$.slot_millis') AS DOUBLE)"


def recost_bigquery_capacity(results_db: ResultsDB, *, dry_run: bool = False) -> dict[str, Any]:
    """Price BigQuery reservation rows on the baseline held, not the slots consumed.

    **The skew this repairs.** :func:`recost_bigquery_reservations` prices
    reservation rows on ``slot_millis`` consumed. A fixed-baseline Enterprise
    reservation bills its baseline for as long as it exists, used or not, so
    consumption reads 0.3-4% of the reservation's cost for the time the queries held
    it at 1-10 GB and 20-90% at 100 GB-1 TB, while every other capacity engine is
    priced on capacity-time. The consumed model avoids pricing the reservation's
    whole held span, which would count days of idle time, and concurrent queries
    more than once; windows answer both.

    **The model.** Baseline slots x the Enterprise slot-hour rate x the time a
    window held them (:func:`capacity_time_sql`, ``mode="price"``): a single-user
    statement is its own window; under load one pass's rows at one level, cut at
    idle gaps. The window's cost goes to its successful rows by slot-time share
    (``metadata.slot_millis``: the slots each query actually used of the baseline,
    as Fabric splits by CPU), or by execution time where a successful row lacks it;
    failed rows cost 0. The consumed price stays under ``metadata.slot_ms_cost_usd``;
    ``metadata.cost_basis`` records ``reservation_held``, and the tier becomes
    ``proportional`` (a time-weighted allocation of what the reservation bills).
    On-demand rows are untouched: bytes billed is their bill. Idempotent.
    """
    rate = BigQueryCostCollector.ENTERPRISE_USD_PER_SLOT_HOUR
    with results_db.connect() as con:
        rates = _label_rates(con, "bigquery", _BIGQUERY_RESERVATION)
        if not rates:
            return {
                "cells": [],
                "total_before_usd": 0.0,
                "total_after_usd": 0.0,
                "dry_run": dry_run,
            }
        sql = capacity_time_sql(
            "bigquery",
            rates,
            mode="price",
            where=_BIGQUERY_RESERVATION,
            share_sql=_BIGQUERY_SLOT_MS,
        )
        con.execute(f"CREATE TEMP TABLE bq_held AS {sql}")
        summary = _summarize(con, "bq_held", "bigquery reservation held", dry_run)
        if not dry_run:
            con.execute(
                f"""
                UPDATE sql_benchmark_results AS t
                SET metadata = json_merge_patch(coalesce(t.metadata, '{{}}'), json_object(
                        'slot_ms_cost_usd',
                        CASE WHEN t.error_message IS NOT NULL THEN 0.0
                             ELSE TRY_CAST(json_extract_string(t.metadata, '$.slot_millis')
                                           AS DOUBLE) / {MS_PER_HOUR} * {rate} END))
                FROM bq_held AS p WHERE t.run_id = p.run_id
                """
            )
            _apply_priced(con, "bq_held", "reservation_held", keep=None, confidence="proportional")
        con.execute("DROP TABLE bq_held")
    return summary


#: $/credit (Enterprise list) at which attribution-priced Snowflake rows (``direct``)
#: carry their cost, to recover their credits.
HISTORICAL_SNOWFLAKE_CREDIT_USD = 3.0


def price_snowflake_on_engine_time(
    results_db: ResultsDB, *, dry_run: bool = False
) -> dict[str, Any]:
    """Price Snowflake at list rate x engine time, as every other engine is priced.

    **Why not attribution.** ``QUERY_ATTRIBUTION_HISTORY`` (credits x $/credit)
    records each query for the same duration we do, and metering confirms the rate
    (the busiest hours bill exactly 1.35 credits per size-hour, the Gen2 multiplier on
    GCP), but attribution counts whole warehouse-seconds: every query carries about
    1-2 s more than its engine time, so attribution prices single-user queries at
    1.68x list x engine time under a second, 1.02x at 30 s and over (1.44x in all),
    and a query sharing its second with a neighbour can read $0. The other six
    engines are priced on engine time to the millisecond with gaps left out, so
    Snowflake is too.

    **The model.** The same as ClickHouse and MotherDuck
    (:func:`recost_rate_priced_rows`): a single-user row costs the size's list rate
    (``report.compute_rates``: credits/hour x the Gen2 multiplier x the Standard
    edition's $/credit, the cheapest edition that runs the benchmark) x its engine
    time; a pass under load is priced by window (:func:`recost_capacity_windows`).
    The tier is ``estimated``. Attribution stays on each row as
    ``metadata.attributed_credits``, a cross-check (``reconcile`` records it);
    attribution-priced rows (``direct``) get theirs recovered from the stored cost
    at :data:`HISTORICAL_SNOWFLAKE_CREDIT_USD`. Idempotent.

    ``dry_run`` prices a copy of the database and reports the before/after.
    """
    if dry_run:
        from ..results import ResultsDB as _ResultsDB

        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "dry_run.duckdb"
            shutil.copy(results_db.path, copy)
            summary = price_snowflake_on_engine_time(_ResultsDB(copy))
        return {**summary, "dry_run": True}
    with results_db.connect() as con:
        con.execute(
            f"""
            UPDATE sql_benchmark_results
            SET metadata = json_merge_patch(coalesce(metadata, '{{}}'), json_object(
                    'attributed_credits', coalesce(
                        TRY_CAST(json_extract_string(metadata, '$.attributed_usd') AS DOUBLE),
                        cost_usd) / {HISTORICAL_SNOWFLAKE_CREDIT_USD}))
            WHERE platform = 'snowflake' AND cost_confidence = 'direct'
              AND json_extract(metadata, '$.attributed_credits') IS NULL
            """
        )
        con.execute(
            "UPDATE sql_benchmark_results SET cost_confidence = 'estimated'"
            " WHERE platform = 'snowflake' AND cost_confidence = 'direct'"
            " AND compute_label IS NOT NULL"
        )
    return recost_rate_priced_rows(results_db, {}, platforms=("snowflake",))


# ClickHouse Cloud sets a query's memory ceiling at this fraction of the replica's
# RAM, so an OOM error message is an engine-emitted witness of the size that was
# actually live — independent of whatever label the harness stamped. Verified
# across nine distinct labels.
OOM_CEILING_FRACTION = 0.90

_OOM_WITNESS_SQL = f"""
    SELECT run_id, platform, scale_factor, run_type, concurrency, compute_label,
           query_id, created_at, cost_usd,
           CAST(regexp_extract(error_message, 'maximum: ([0-9.]+) GiB', 1) AS DOUBLE)
             / {OOM_CEILING_FRACTION} AS measured_gb_per_replica,
           CAST(regexp_extract(compute_label, '\\d+x(\\d+)GB', 1) AS DOUBLE) AS labelled_gb
    FROM sql_benchmark_results
    WHERE platform = 'clickhouse'
      AND compute_label IS NOT NULL
      AND error_message LIKE '%maximum:%GiB%'
"""


def mislabelled_by_memory_witness(
    results_db: ResultsDB, *, tolerance_gb: float = 0.5
) -> list[dict[str, Any]]:
    """ClickHouse rows whose OOM ceiling contradicts their ``compute_label``.

    A standing integrity check, not a one-off. ClickHouse caps a query's memory at
    ``OOM_CEILING_FRACTION`` x the replica's RAM, so ``maximum: N GiB`` in an error
    message states the size that was *actually live* when the query ran. Comparing
    that against the stamped label catches rows recorded while the service was
    mid-resize — where the harness had already moved to the next label but the
    engine had not yet finished scaling.

    It also shows why ``engine_environment`` is not a size record for recorded
    rows: for ``(clickhouse, SF10, warm)`` it finds rows measurably at 16 GB in a
    cell whose ``engine_environment`` capture says 32 GB.

    Args:
        results_db: Results database to audit (read-only).
        tolerance_gb: Allowed gap between measured and labelled per-replica GB.

    Returns:
        One dict per disagreeing row, with both the labelled and measured size.
    """
    with results_db.connect() as con:
        rows = con.execute(
            f"SELECT * FROM ({_OOM_WITNESS_SQL}) w "
            "WHERE w.labelled_gb IS NOT NULL AND w.measured_gb_per_replica IS NOT NULL "
            f"  AND abs(w.measured_gb_per_replica - w.labelled_gb) > {tolerance_gb} "
            "ORDER BY w.created_at"
        ).fetchall()
        cols = [d[0] for d in con.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]


def quarantine_mislabelled_rows(results_db: ResultsDB, *, dry_run: bool = False) -> dict[str, Any]:
    """Void the ``compute_label`` on rows the memory witness contradicts.

    These rows measured a **transitional** state: the service was still scaling when
    they ran, so they belong to no stable configuration. Neither available
    alternative is honest — keeping the stamped label inflates the *larger* size's
    error rate with failures it did not produce, and relabelling to the measured
    size files them into a different cell's pass, mixing populations.

    So the label is set to NULL and the original preserved under
    ``metadata.compute_label_quarantined``. NULL drops a row out of the
    size-aware and canonical views loudly (see ``report/canonical.py``), while the
    row stays in the append-only record and continues to count toward overall
    completion — it *was* a real failure, just not one attributable to a size.

    The metadata key deliberately differs from the backfill's
    ``compute_label_was_null``: "we captured a label and it was wrong" and "we never
    captured one" are different states, and conflating them makes a canonical-tier
    filter unsafe.

    Args:
        results_db: Results database to repair, mutated unless ``dry_run``.
        dry_run: Report what would be quarantined without writing.

    Returns:
        Summary with the affected rows grouped by cell.
    """
    offenders = mislabelled_by_memory_witness(results_db)
    ids = [o["run_id"] for o in offenders]
    if ids and not dry_run:
        with results_db.connect() as con:
            con.execute(
                """
                UPDATE sql_benchmark_results
                SET metadata = json_merge_patch(
                        coalesce(metadata, '{}'),
                        json_object(
                            'compute_label_quarantined', compute_label,
                            'quarantine_reason',
                            'OOM ceiling contradicts label — service was mid-resize')),
                    compute_label = NULL
                WHERE run_id IN (SELECT unnest(?::UUID[]))
                """,
                [ids],
            )
    summary = {
        "rows": len(offenders),
        "cells": sorted(
            {
                (o["scale_factor"], o["run_type"], o["compute_label"], o["measured_gb_per_replica"])
                for o in offenders
            }
        ),
        "nonzero_cost_rows": sum(1 for o in offenders if (o["cost_usd"] or 0) > 0),
        "dry_run": dry_run,
    }
    logger.info(
        "memory-witness quarantine%s: %d rows across %d cells (%d carried nonzero cost)",
        " (dry run)" if dry_run else "",
        summary["rows"],
        len(summary["cells"]),
        summary["nonzero_cost_rows"],
    )
    return summary

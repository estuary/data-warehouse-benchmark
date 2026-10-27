"""Check Fabric passes: engine time on every row and no result-set cache hits.

A Fabric burst is only sound if every query was computed, not served from the
warehouse's result-set cache, and if reconcile found each query's engine time. This
reads the rows of the passes named, then looks their labels up in
``queryinsights.exec_requests_history`` (``result_cache_hit``: 0 not cached,
1 cached by this run, 2 served from cache). The capacity must be Active.

Passes are named by ``metadata.tag`` (``--tags``), or, for passes that carry no tag
(``benchmark sql`` has none), by every row written since a local timestamp
(``--since``, as ``created_at`` is local), optionally narrowed to run types, and
grouped per cell (run type, size, suite, level). ``--require-cpu`` also fails a
successful row without ``metadata.cpu_ms``: the CPU time the Fabric cost model needs
to price bursting past the SKU (``analysis.recost.recost_fabric_windows``).

    uv run python scripts/ops/check_fabric_passes.py --scale 100 --tags cold-r1,warm-r1
    uv run python scripts/ops/check_fabric_passes.py --scale 10 \\
        --since '2026-10-01 08:00:00' --run-types warm,concurrency --require-cpu

Exits 1 when any row of a pass lacks engine time (or CPU time, with
``--require-cpu``) or was served from the cache, or when a named tag or the window
has no rows.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from benchmark.connectors.fabric import FabricConnector
from benchmark.logging_config import configure_logging

logger = logging.getLogger("benchmark.scripts.check_fabric_passes")

#: Labels per IN (...) list; keeps each queryinsights lookup a modest statement.
LABEL_BATCH = 200
RESULTS_DB = Path("results/benchmark.duckdb")

#: (pass, label, server_time_ms, error_message, cpu_ms) per latest row.
PassRow = tuple[str, str, float | None, str | None, float | None]


def pass_rows(
    scale: int,
    *,
    tags: list[str] | None = None,
    since: str | None = None,
    run_types: list[str] | None = None,
) -> list[PassRow]:
    """The latest row per query of each pass, from its tag or from a time window.

    With ``tags`` a pass is one tag. With ``since`` it is one cell written after that
    local timestamp: run type, size, suite and level.
    """
    con = duckdb.connect(str(RESULTS_DB), read_only=True)
    try:
        if tags is not None:
            pass_key = "json_extract_string(metadata, '$.tag')"
            where = f"{pass_key} IN (SELECT unnest(?::VARCHAR[]))"
            params: list[object] = [scale, tags]
        else:
            pass_key = (
                "concat_ws(' ', run_type, compute_label, block,"
                " 'L' || coalesce(CAST(concurrency AS VARCHAR), '-'))"
            )
            where = "created_at >= CAST(? AS TIMESTAMP)"
            params = [scale, since]
            if run_types:
                where += " AND run_type IN (SELECT unnest(?::VARCHAR[]))"
                params.append(run_types)
        # Latest row per (pass, query, run): a pass cut short and re-run under the same
        # tag keeps both attempts in the append-only table.
        return con.execute(
            f"""
            SELECT pass, platform_query_id, server_time_ms, error_message, cpu_ms FROM (
              SELECT {pass_key} AS pass, platform_query_id, server_time_ms, error_message,
                     TRY_CAST(json_extract_string(metadata, '$.cpu_ms') AS DOUBLE) AS cpu_ms,
                     ROW_NUMBER() OVER (
                       PARTITION BY {pass_key}, query_id, run_number
                       ORDER BY created_at DESC) AS rn
              FROM sql_benchmark_results
              WHERE platform = 'fabric' AND scale_factor = ? AND {where})
            WHERE rn = 1
            """,
            params,
        ).fetchall()
    finally:
        con.close()


def cache_hits(labels: list[str]) -> dict[str, int]:
    """Label -> result_cache_hit from queryinsights (the connector's own check runs first)."""
    out: dict[str, int] = {}
    with FabricConnector({}) as fc:
        for i in range(0, len(labels), LABEL_BATCH):
            batch = labels[i : i + LABEL_BATCH]
            in_list = ", ".join(f"'{label}'" for label in batch)
            fc.cursor.execute(
                "SELECT label, MAX(result_cache_hit) FROM queryinsights.exec_requests_history "
                f"WHERE label IN ({in_list}) GROUP BY label"
            )
            out.update({str(r[0]): int(r[1]) for r in fc.cursor.fetchall()})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scale", type=int, required=True)
    named = ap.add_mutually_exclusive_group(required=True)
    named.add_argument("--tags", help="comma list of metadata.tag values")
    named.add_argument("--since", help="local timestamp: every row written since then")
    ap.add_argument("--run-types", help="with --since: comma list of run types to include")
    ap.add_argument(
        "--require-cpu",
        action="store_true",
        help="also fail a successful row without metadata.cpu_ms",
    )
    args = ap.parse_args()
    configure_logging("INFO")

    def split(value: str | None) -> list[str]:
        return [v.strip() for v in (value or "").split(",") if v.strip()]

    tags = split(args.tags) if args.tags else None
    rows = pass_rows(args.scale, tags=tags, since=args.since, run_types=split(args.run_types))
    hits = cache_hits([r[1] for r in rows if r[1]])
    passes = tags if tags is not None else sorted({r[0] for r in rows})
    if not passes:
        logger.error("no Fabric rows at scale %d since %s", args.scale, args.since)
        return 1
    problems = 0
    for name in passes:
        mine = [r for r in rows if r[0] == name]
        ok = [r for r in mine if r[3] is None]
        no_engine = sum(r[2] is None for r in ok)
        no_cpu = sum(r[4] is None for r in ok)
        served = sum(hits.get(r[1] or "", 0) == 2 for r in mine)
        unseen = sum((r[1] or "") not in hits for r in mine)
        logger.info(
            "%s: %d rows, %d errors, %d without engine time, %d without CPU time, %d served "
            "from the result cache, %d not found in queryinsights",
            name,
            len(mine),
            len(mine) - len(ok),
            no_engine,
            no_cpu,
            served,
            unseen,
        )
        if not mine or no_engine or served or (args.require_cpu and no_cpu):
            problems += 1
    if problems:
        logger.error("%d pass(es) failed the check", problems)
        return 1
    logger.info(
        "all passes pass: engine time%s on every row, nothing served from the cache",
        " and CPU time" if args.require_cpu else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

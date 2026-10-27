"""Assert every row a pass just wrote carries the compute label it was meant to.

WHY THIS EXISTS. For engines that resize out of band (ClickHouse, MotherDuck,
Redshift, Databricks) ``--compute-size`` is not passed to the runner: the label is
read from the live service by ``capture_runtime_environment`` at pass start, once per
block. A capture that fails or reads the wrong size (a resize that has not landed),
or a block that never ran, leaves the cell FRACTURED: size-keyed analysis sees one
block under the size and the other under another size or none, while the pass
reports zero failures.

Call this after each pass so a fractured cell is caught in seconds rather than found
weeks later in a figure.

Exit codes: 0 clean, 1 wrong/missing labels found.

    uv run python scripts/ops/check_compute_labels.py \\
        --platform clickhouse --scale 100 --expect 1x32GB --since-minutes 240
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from benchmark.logging_config import configure_logging  # noqa: E402

logger = logging.getLogger("benchmark.scripts.check_compute_labels")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", required=True)
    ap.add_argument("--scale", type=int, required=True)
    ap.add_argument("--expect", required=True, help="the compute_label the pass should carry")
    ap.add_argument("--run-type", default="warm")
    ap.add_argument(
        "--since-minutes",
        type=int,
        help="lookback window in minutes. Prefer --since: a duration plus any buffer "
        "can bleed into the adjacent pass and report a false fracture",
    )
    ap.add_argument(
        "--since",
        help="only inspect rows created at or after this LOCAL timestamp "
        "('YYYY-MM-DD HH:MM:SS'). Capture it immediately before the pass starts — "
        "created_at is naive LOCAL time, so a UTC value silently matches nothing",
    )
    ap.add_argument(
        "--expect-blocks",
        help="Comma-separated blocks that MUST be present, e.g. 'tpcds,dsb'. Without "
        "this the check validates labels only, which passes a half-populated cell "
        "whose present rows carry the right label.",
    )
    ap.add_argument(
        "--expect-levels",
        help="Comma-separated concurrency levels that must all be present per block.",
    )
    ap.add_argument("--results-db", default="results/benchmark.duckdb")
    args = ap.parse_args()
    configure_logging("INFO")

    if not (args.since or args.since_minutes):
        logger.error("pass --since (preferred) or --since-minutes")
        return 1

    con = duckdb.connect(args.results_db, read_only=True)
    if args.since:
        window_sql = "created_at >= ?"
        window_arg: object = args.since
    else:
        window_sql = "created_at >= now() - INTERVAL (?) MINUTE"
        window_arg = args.since_minutes
    rows = con.execute(
        f"""
        SELECT COALESCE(compute_label, '(NULL)') AS lbl, block,
               count(*) AS n, count(DISTINCT query_id) AS q
        FROM sql_benchmark_results
        WHERE platform = ? AND scale_factor = ? AND run_type = ?
          AND {window_sql}
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
        [args.platform, args.scale, args.run_type, window_arg],
    ).fetchall()

    if not rows:
        logger.error(
            "no %s rows for %s at SF%s since %s — the pass wrote nothing",
            args.run_type,
            args.platform,
            args.scale,
            args.since or f"{args.since_minutes} min ago",
        )
        return 1

    bad = 0
    for lbl, block, n, q in rows:
        ok = lbl == args.expect
        logger.log(
            logging.INFO if ok else logging.ERROR,
            "  %-12s %-11s %5d rows  %3d queries%s",
            lbl,
            block,
            n,
            q,
            "" if ok else f"   <-- expected {args.expect}",
        )
        if not ok:
            bad += n

    if args.expect_blocks:
        want_blocks = [b.strip() for b in args.expect_blocks.split(",") if b.strip()]
        want_levels = (
            {int(x) for x in args.expect_levels.split(",")} if args.expect_levels else None
        )
        for block in want_blocks:
            got = con.execute(
                f"""
                SELECT count(DISTINCT query_id), count(DISTINCT concurrency),
                       string_agg(DISTINCT CAST(concurrency AS VARCHAR), ',')
                FROM sql_benchmark_results
                WHERE platform = ? AND scale_factor = ? AND run_type = ?
                  AND block = ? AND compute_label = ? AND {window_sql}
                """,
                [args.platform, args.scale, args.run_type, block, args.expect, window_arg],
            ).fetchone()
            queries = got[0] if got else 0
            if not queries:
                logger.error("  block %s ABSENT — the cell is half-populated", block)
                bad += 1
                continue
            if want_levels is not None:
                have = {int(x) for x in (got[2] or "").split(",") if x}
                missing = sorted(want_levels - have)
                if missing:
                    logger.error("  block %s missing level(s) %s", block, missing)
                    bad += 1
                    continue
            logger.info("  block %s present: %d queries, %d level(s)", block, queries, got[1])

    if bad:
        logger.error(
            "%d row(s) carry the wrong or no compute label — the cell is fractured; "
            "repair the label or re-run the pass before trusting any figure from it",
            bad,
        )
        return 1
    logger.info("all rows carry %s", args.expect)
    return 0


if __name__ == "__main__":
    sys.exit(main())

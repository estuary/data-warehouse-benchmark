"""Set result rows aside: keep them in the table, leave them out of every report view.

A set-aside row carries ``metadata.set_aside`` (the reason). The shared views
(``report/views.py``) drop it before latest-wins, so the rows it would have superseded
stay published; nothing is deleted (the table is append-only). Use it for a measurement
taken as a check rather than as a replacement, e.g. warm cells re-measured to check one
property, so the original ladder stays published from one pass:

    uv run python scripts/ops/set_aside_rows.py --platform redshift --run-type warm \\
        --scale 1 --labels "8 RPU,16 RPU" --since "YYYY-MM-DD HH:MM:SS" \\
        --until "YYYY-MM-DD HH:MM:SS" \\
        --reason "single-user queue-time check; the original ladder stays published"

``--since``/``--until`` are local timestamps, as ``created_at`` is. ``--dry-run`` reports
the rows without writing. Run it when nothing else writes the database.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from benchmark.logging_config import configure_logging
from benchmark.results import SET_ASIDE_KEY

logger = logging.getLogger("benchmark.scripts.set_aside_rows")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path("results/benchmark.duckdb"))
    ap.add_argument("--platform", required=True)
    ap.add_argument("--run-type", required=True)
    ap.add_argument("--scale", type=int, required=True)
    ap.add_argument("--labels", required=True, help="comma list of compute labels")
    ap.add_argument("--since", required=True, help="local timestamp, inclusive")
    ap.add_argument("--until", required=True, help="local timestamp, exclusive")
    ap.add_argument("--reason", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    configure_logging("INFO")

    labels = [x.strip() for x in args.labels.split(",") if x.strip()]
    where = (
        "platform = ? AND run_type = ? AND scale_factor = ? AND compute_label IN ({})"
        " AND created_at >= ? AND created_at < ?"
    ).format(", ".join("?" for _ in labels))
    params = [args.platform, args.run_type, args.scale, *labels, args.since, args.until]
    con = duckdb.connect(str(args.db), read_only=args.dry_run)
    try:
        rows = con.execute(
            f"SELECT compute_label, block, count(*) FROM sql_benchmark_results WHERE {where}"
            " GROUP BY 1, 2 ORDER BY 1, 2",
            params,
        ).fetchall()
        total = sum(r[2] for r in rows)
        for label, block, n in rows:
            logger.info("%s %s: %d rows", label, block, n)
        if args.dry_run or total == 0:
            logger.info(
                "%s: %d rows match; nothing written",
                "dry run" if args.dry_run else "no rows",
                total,
            )
            return 0
        con.execute(
            "UPDATE sql_benchmark_results SET metadata = json_merge_patch("
            f"coalesce(metadata, '{{}}'), json_object('{SET_ASIDE_KEY}', ?)) WHERE {where}",
            [args.reason, *params],
        )
        logger.info("set aside %d rows: %s", total, args.reason)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

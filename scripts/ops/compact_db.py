"""Compact the results database into a fresh file, verify it, then swap it in.

DuckDB reuses freed blocks but never shrinks its file, so every re-pricing or
reconcile pass that rewrites rows leaves dead space behind, and each commit of the
database stores the whole file again in Git LFS. ``COPY FROM DATABASE`` writes the
live contents into a new file.

The copy replaces the original only after it matches it exactly: the same tables,
row counts and content hashes per table, and the same constraints, columns (types,
defaults, nullability), views, sequences and macros. Otherwise the original is left
untouched and the script exits 1. The original is kept beside it as
``<name>.pre-compact`` unless ``--no-backup``.

Run it when nothing writes the database (no measured run, reconcile or re-pricing
going), and last before committing it: later writes leave dead space again.

    uv run python scripts/ops/compact_db.py                 # results/benchmark.duckdb
    uv run python scripts/ops/compact_db.py --check-only    # compact to a temp file, report, keep original
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from benchmark.logging_config import configure_logging

logger = logging.getLogger("benchmark.scripts.compact_db")

#: Catalog listings compared between the original and the copy (``{db}`` = alias).
CATALOG_QUERIES = {
    "constraints": "SELECT table_name, constraint_type, constraint_text FROM duckdb_constraints()"
    " WHERE database_name = '{db}' ORDER BY ALL",
    "columns": "SELECT table_name, column_name, data_type, column_default, is_nullable"
    " FROM duckdb_columns() WHERE database_name = '{db}' ORDER BY ALL",
    "views": "SELECT view_name, sql FROM duckdb_views() WHERE database_name = '{db}'"
    " AND NOT internal ORDER BY ALL",
    "sequences": "SELECT sequence_name FROM duckdb_sequences() WHERE database_name = '{db}'"
    " ORDER BY ALL",
    "macros": "SELECT function_name FROM duckdb_functions() WHERE database_name = '{db}'"
    " AND NOT internal ORDER BY ALL",
}


def compact(src: Path, dst: Path) -> None:
    """Write ``src``'s contents into a new file ``dst``."""
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{src.as_posix()}' AS src (READ_ONLY)")
        con.execute(f"ATTACH '{dst.as_posix()}' AS dst")
        con.execute("COPY FROM DATABASE src TO dst")
    finally:
        con.close()


def differences(src: Path, dst: Path) -> list[str]:
    """Every way ``dst`` differs from ``src``; empty when they match."""
    con = duckdb.connect()
    problems: list[str] = []
    try:
        con.execute(f"ATTACH '{src.as_posix()}' AS src (READ_ONLY)")
        con.execute(f"ATTACH '{dst.as_posix()}' AS dst (READ_ONLY)")
        tables = {
            db: {
                r[0]
                for r in con.execute(
                    f"SELECT table_name FROM duckdb_tables() WHERE database_name = '{db}'"
                ).fetchall()
            }
            for db in ("src", "dst")
        }
        if tables["src"] != tables["dst"]:
            problems.append(f"tables differ: {sorted(tables['src'] ^ tables['dst'])}")
        for table in sorted(tables["src"] & tables["dst"]):
            a = con.execute(f"SELECT count(*), sum(hash(t)) FROM src.{table} t").fetchone()
            b = con.execute(f"SELECT count(*), sum(hash(t)) FROM dst.{table} t").fetchone()
            if a != b:
                problems.append(f"{table}: rows/hash {a} != {b}")
            else:
                logger.info("%s: %d rows match", table, a[0])
        for kind, sql in CATALOG_QUERIES.items():
            a = con.execute(sql.format(db="src")).fetchall()
            b = con.execute(sql.format(db="dst")).fetchall()
            if a != b:
                problems.append(f"{kind} differ ({len(a)} vs {len(b)})")
    finally:
        con.close()
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=Path("results/benchmark.duckdb"))
    ap.add_argument(
        "--check-only", action="store_true", help="compact and verify, keep the original"
    )
    ap.add_argument("--no-backup", action="store_true", help="do not keep <name>.pre-compact")
    args = ap.parse_args()
    configure_logging("INFO")

    src: Path = args.db
    if not src.exists():
        logger.error("no database at %s", src)
        return 2
    if src.with_name(src.name + ".wal").exists():
        logger.error(
            "%s has a write-ahead log: a writer is open or did not close; not compacting", src
        )
        return 2
    tmp = src.with_name(src.name + ".compact-tmp")
    tmp.unlink(missing_ok=True)
    t0 = time.perf_counter()
    compact(src, tmp)
    before, after = os.path.getsize(src), os.path.getsize(tmp)
    logger.info(
        "compacted %s: %.1f MB -> %.1f MB in %.1f s",
        src,
        before / 1e6,
        after / 1e6,
        time.perf_counter() - t0,
    )
    problems = differences(src, tmp)
    if problems:
        for p in problems:
            logger.error("copy differs: %s", p)
        tmp.unlink(missing_ok=True)
        return 1
    if args.check_only:
        tmp.unlink(missing_ok=True)
        logger.info("check only: the copy matches; original kept")
        return 0
    if not args.no_backup:
        os.replace(src, src.with_name(src.name + ".pre-compact"))
    os.replace(tmp, src)
    logger.info("replaced %s with the verified copy", src)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""TPC-DS data generation via the DuckDB `tpcds` extension.

The upstream TPC-DS generator is the `dsdgen` C binary, which requires a
TPC-supplied source download and a C build. We use DuckDB's built-in `tpcds`
extension instead, which produces the same canonical schema and
within-tolerance row counts for SF1-SF100 with zero build steps. At SF1000 the
extension can OOM on fact-table generation without a spill directory (see
:func:`generate_tpcds_sf`).

The extension's row counts differ very slightly from hardcoded reference
counts (e.g. `store_returns` ~287k vs ~288k at SF1); there is also small
per-run randomization in two of the returns tables. We therefore do NOT
hardcode expected counts: the row counts the extension actually produces are
the canonical manifest, persisted to `data/tpcds/expected_counts.json`.
``validate-loads`` then compares engine-loaded counts against that manifest.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from functools import lru_cache
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

logger = logging.getLogger("benchmark.datagen.tpcds")


TPCDS_TABLES: tuple[str, ...] = (
    "call_center",
    "catalog_page",
    "catalog_returns",
    "catalog_sales",
    "customer",
    "customer_address",
    "customer_demographics",
    "date_dim",
    "household_demographics",
    "income_band",
    "inventory",
    "item",
    "promotion",
    "reason",
    "ship_mode",
    "store",
    "store_returns",
    "store_sales",
    "time_dim",
    "warehouse",
    "web_page",
    "web_returns",
    "web_sales",
    "web_site",
)


@lru_cache(maxsize=1)
def tpcds_table_schema() -> dict[str, tuple[tuple[str, str], ...]]:
    """Canonical TPC-DS ``(column_name, DuckDB type)`` per table, from the DuckDB
    ``tpcds`` extension.

    ``CALL dsdgen(sf=0)`` creates the 24 tables empty — full schema, zero rows —
    so ``DESCRIBE`` yields the authoritative names + types with no external DDL
    file and no data generation. DSB shares this exact schema (only the data
    distribution differs). Cached: the extension install + schema read run once.
    """
    con = duckdb.connect(":memory:")
    try:
        con.execute("INSTALL tpcds; LOAD tpcds;")
        con.execute("CALL dsdgen(sf=0)")
        # DESCRIBE rows are (column_name, column_type, null, key, default, extra).
        return {
            table: tuple((row[0], row[1]) for row in con.execute(f"DESCRIBE {table}").fetchall())
            for table in TPCDS_TABLES
        }
    finally:
        con.close()


def existing_table_counts(output_dir: Path) -> dict[str, int] | None:
    """Row counts if every TPC-DS table parquet is already present, else None.

    Lets generation short-circuit when a scale's data already exists, so
    re-running the pipeline doesn't redo the (slow) generation. Counts are read
    from parquet footers — no full scan. Shared by TPC-DS and DSB (same table
    set + ``<table>.parquet`` layout).
    """
    counts: dict[str, int] = {}
    for table in TPCDS_TABLES:
        parquet = output_dir / f"{table}.parquet"
        if not parquet.exists():
            return None
        counts[table] = pq.ParquetFile(parquet).metadata.num_rows
    return counts


def generate_tpcds_sf(
    scale_factor: int, output_dir: Path, *, force: bool = False
) -> dict[str, int]:
    """Generate TPC-DS data at a scale factor and write one Parquet per table.

    Idempotent: if a complete set of table parquets is already present, returns
    their counts without regenerating unless ``force=True``.

    Args:
        scale_factor: 1, 10, 100, or 1000.
        output_dir: Directory to write `<table>.parquet` files to.
        force: Regenerate even when data already exists.

    Returns:
        Mapping of table name -> actual row count.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    if not force:
        existing = existing_table_counts(output_dir)
        if existing is not None:
            logger.info(
                "TPC-DS SF%d already present (%d tables) — skipping regeneration "
                "(pass force=True / --force to overwrite)",
                scale_factor,
                len(existing),
            )
            return existing

    con = duckdb.connect(":memory:")
    con.execute("INSTALL tpcds; LOAD tpcds;")
    # In-memory DuckDB cannot spill unless temp_directory is set, and
    # dsdgen(sf=1000) materializes tables far beyond RAM (SF100 alone nearly
    # fills 128 GB). Spill next to the output (same volume as the parquet
    # target) and cap memory below physical so the OS survives.
    tmp_dir = output_dir / ".duckdb_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory='{tmp_dir.as_posix()}'")
    limit_gb = max(4, int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") * 0.8 / 1e9))
    con.execute(f"SET memory_limit='{limit_gb}GB'")
    logger.info("dsdgen(sf=%d) starting", scale_factor)
    con.execute(f"CALL dsdgen(sf={scale_factor});")
    logger.info("dsdgen(sf=%d) complete; exporting %d tables", scale_factor, len(TPCDS_TABLES))

    counts: dict[str, int] = {}
    for table in TPCDS_TABLES:
        target = output_dir / f"{table}.parquet"
        con.execute(f"COPY {table} TO '{target.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        row_count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        counts[table] = row_count
        logger.info(
            "exported %s (%d rows, %.1f MiB)",
            table,
            row_count,
            target.stat().st_size / (1024 * 1024),
        )

    con.close()
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return counts


def write_expected_counts_manifest(
    counts_by_scale: dict[int, dict[str, int]],
    manifest_path: Path,
) -> None:
    """Write the canonical row-count manifest consumed by ``validate-loads``.

    Schema:
        {"1": {"call_center": 6, ...}, "10": {...}, ...}

    Existing entries for scale factors not in `counts_by_scale` are preserved
    so we can append SF1 without clobbering later SF10/SF100 runs.
    """
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict[str, int]] = {}
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))

    for sf, counts in counts_by_scale.items():
        existing[str(sf)] = counts

    # Trailing newline so regenerating doesn't churn a no-op diff vs the
    # committed manifest (POSIX-friendly, matches the version-controlled file).
    manifest_path.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    logger.info(
        "expected_counts.json written: %s scales -> %s",
        sorted(int(k) for k in existing),
        manifest_path,
    )

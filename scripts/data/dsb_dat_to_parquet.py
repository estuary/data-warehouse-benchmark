"""Convert microsoft/dsb's pipe-delimited .dat output to Parquet.

dsdgen writes each row in TPC-DS format: `col1|col2|...|colN|` (trailing pipe).
We load via DuckDB using the explicit DDL from
`data/generators/dsb/scripts/create_tables.sql` so types are correct, then
COPY each table to `data/dsb/sf1/<table>.parquet` (ZSTD-compressed).

The `dbgen_version` "table" is skipped — it's metadata, not part of the
benchmark schema. The 24 real TPC-DS tables are emitted.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import duckdb

logger = logging.getLogger("benchmark.scripts.dsb_dat_to_parquet")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DDL_PATH = REPO_ROOT / "data" / "generators" / "dsb" / "scripts" / "create_tables.sql"


def _paths_for_scale(scale_factor: int) -> tuple[Path, Path]:
    """Return (dat_dir, out_dir) for a scale factor.

    dsdgen writes ``data/dsb/sf{N}_dat/<table>.dat``; the converter emits
    ``data/dsb/sf{N}/<table>.parquet``.
    """
    dat_dir = REPO_ROOT / "data" / "dsb" / f"sf{scale_factor}_dat"
    out_dir = REPO_ROOT / "data" / "dsb" / f"sf{scale_factor}"
    return dat_dir, out_dir


# 24 TPC-DS tables to emit (dbgen_version excluded — DSB metadata, not data).
TABLES: tuple[str, ...] = (
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


def _split_ddl(ddl_text: str) -> dict[str, str]:
    """Pull each `create table <name> ( ... );` block out of create_tables.sql."""
    out: dict[str, str] = {}
    pattern = re.compile(r"create\s+table\s+(\w+)\s*\((.*?)\)\s*;", re.IGNORECASE | re.DOTALL)
    for match in pattern.finditer(ddl_text):
        table = match.group(1).lower()
        out[table] = match.group(0)
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    scale_factor = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    dat_dir, out_dir = _paths_for_scale(scale_factor)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not DDL_PATH.exists():
        raise FileNotFoundError(f"DSB DDL missing: {DDL_PATH}")
    if not dat_dir.exists():
        raise FileNotFoundError(f"DSB .dat dir missing: {dat_dir}")

    ddl_text = DDL_PATH.read_text(encoding="utf-8")
    ddl_by_table = _split_ddl(ddl_text)

    con = duckdb.connect(":memory:")
    counts: dict[str, int] = {}

    for table in TABLES:
        ddl = ddl_by_table.get(table)
        if ddl is None:
            raise RuntimeError(f"no DDL for {table} in {DDL_PATH}")
        dat = dat_dir / f"{table}.dat"
        if not dat.exists():
            logger.warning("missing .dat for %s; skipping", table)
            continue

        con.execute(f"DROP TABLE IF EXISTS {table}")
        # `primary key (...)` blocks in the DDL are advisory; DuckDB accepts
        # them but won't enforce on COPY FROM. Leave them in.
        con.execute(ddl)

        # dsdgen writes a trailing `|`, which DuckDB treats as an extra empty
        # column. read_csv's `null_padding=true` accepts row-shorter-than-schema
        # but here we have row-LONGER-than-schema. The workaround: enumerate
        # an extra dummy column and select it away.
        n_cols = len(con.execute(f"PRAGMA table_info('{table}')").fetchall())
        # Use read_csv with explicit columns; let DuckDB cast on INSERT.
        select_cols = ", ".join(f"column{i:02d}" for i in range(n_cols))
        col_defs = ", ".join(f"'column{i:02d}': 'VARCHAR'" for i in range(n_cols + 1))

        sql = f"""
        INSERT INTO {table}
        SELECT {select_cols}
        FROM read_csv(?, delim='|', header=false, quote='', escape='',
                      strict_mode=false,
                      columns={{{col_defs}}})
        """
        con.execute(sql, [str(dat).replace("\\", "/")])

        count_row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        row_count = count_row[0] if count_row else 0
        counts[table] = row_count

        target = out_dir / f"{table}.parquet"
        con.execute(f"COPY {table} TO '{target.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        size_mib = target.stat().st_size / (1024 * 1024)
        logger.info("exported %s (%d rows, %.1f MiB)", table, row_count, size_mib)

    con.close()
    total = sum(counts.values())
    logger.info(
        "DSB SF%d conversion complete: %d tables, %d total rows",
        scale_factor,
        len(counts),
        total,
    )


if __name__ == "__main__":
    main()

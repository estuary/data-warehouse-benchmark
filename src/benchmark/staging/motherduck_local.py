"""Direct local-Parquet -> MotherDuck loader.

Estuary is the canonical loading path (estuary/captures + materializations).
This module is a MotherDuck-only fast path used when only MOTHERDUCK_TOKEN
is configured and S3/Estuary credentials aren't wired up yet: it loads
`data/tpcds/sf1/*.parquet` straight into `benchmark_db.tpcds_sf1` via the
DuckDB SDK — no S3, no flowctl.

Re-running is idempotent: each table is CREATE OR REPLACE.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from ..datagen.tpcds import TPCDS_TABLES
from ..exceptions import ConnectorError
from ..settings import get_settings

logger = logging.getLogger("benchmark.staging.motherduck_local")


def load_sf1_to_motherduck(
    parquet_dir: Path = Path("data/tpcds/sf1"),
    database: str = "benchmark_db",
    schema: str = "tpcds_sf1",
) -> dict[str, int]:
    """Create `database.schema` and CREATE OR REPLACE each TPC-DS table.

    Returns mapping of table -> row count loaded.
    """
    md_token = get_settings().MOTHERDUCK.TOKEN
    token = md_token.get_secret_value() if md_token else None
    if not token:
        raise ConnectorError("MOTHERDUCK_TOKEN missing: set it in env/motherduck.env")
    if not parquet_dir.exists():
        raise FileNotFoundError(f"Parquet dir missing: {parquet_dir}")

    con = duckdb.connect(f"md:?motherduck_token={token}")
    try:
        con.execute(f"CREATE DATABASE IF NOT EXISTS {database}")
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {database}.{schema}")
        logger.info("MotherDuck %s.%s ready", database, schema)

        counts: dict[str, int] = {}
        for table in TPCDS_TABLES:
            parquet = parquet_dir / f"{table}.parquet"
            if not parquet.exists():
                logger.warning("missing parquet for %s; skipping", table)
                continue
            posix = parquet.resolve().as_posix()
            con.execute(
                f"CREATE OR REPLACE TABLE {database}.{schema}.{table} AS "
                f"SELECT * FROM read_parquet('{posix}')"
            )
            n = con.execute(f"SELECT COUNT(*) FROM {database}.{schema}.{table}").fetchone()[0]
            counts[table] = n
            logger.info(
                "loaded %s.%s.%s (%d rows)",
                database,
                schema,
                table,
                n,
                extra={"platform": "motherduck", "table": table, "row_count": n},
            )
        return counts
    finally:
        con.close()

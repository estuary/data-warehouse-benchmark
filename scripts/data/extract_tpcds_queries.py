"""Extract the 99 canonical TPC-DS queries from the DuckDB `tpcds` extension.

Writes one file per query to queries/duckdb/tpcds/query_NN.sql, zero-padded.
Idempotent: overwrites if already present.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

logger = logging.getLogger("benchmark.scripts.extract_tpcds_queries")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO_ROOT / "queries" / "duckdb" / "tpcds"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute("INSTALL tpcds; LOAD tpcds;")
    rows = con.execute("SELECT query_nr, query FROM tpcds_queries() ORDER BY query_nr").fetchall()
    con.close()

    if len(rows) != 99:
        raise RuntimeError(f"expected 99 TPC-DS queries from extension, got {len(rows)}")

    for query_nr, query_sql in rows:
        out_path = OUT_DIR / f"query_{query_nr:02d}.sql"
        out_path.write_text(query_sql.strip() + "\n", encoding="utf-8")
        logger.info("Wrote %s (%d chars)", out_path.relative_to(REPO_ROOT), len(query_sql))

    logger.info("Wrote %d TPC-DS queries to %s", len(rows), OUT_DIR.relative_to(REPO_ROOT))


if __name__ == "__main__":
    main()

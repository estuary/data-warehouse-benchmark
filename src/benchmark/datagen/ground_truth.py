"""DuckDB ground-truth hashing.

Loads the per-suite Parquet files into an in-memory DuckDB, runs every
canonical query in the registry, and SHA-256s the result set. The hashes
land in `ground_truth_hashes`; ``validate-sf1`` then compares each engine's
per-query result against this canonical reference.

Suite isolation: TPC-DS and DSB share table schemas but have different data.
Each suite is hashed against its own in-memory database (TPC-DS queries run
against TPC-DS data; DSB queries run against DSB data), so the hashes
diverge when DSB introduces skew on the returns tables.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import duckdb

from ..queries.registry import QueryRegistry
from ..results import ResultsDB
from ..sql_benchmark.validator import hash_query_result, serialize_columns, serialize_rows
from .tpcds import TPCDS_TABLES

logger = logging.getLogger("benchmark.datagen.ground_truth")


def _load_tables_into_duckdb(
    con: duckdb.DuckDBPyConnection, parquet_dir: Path, tables: tuple[str, ...]
) -> None:
    """Load each Parquet under ``parquet_dir`` into a table with the unqualified
    name. The canonical queries reference tables as bare names (e.g.
    ``store_sales``, not ``tpcds_store_sales``), so we register them in the
    default schema.
    """
    for table in tables:
        path = parquet_dir / f"{table}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"Parquet missing: {path}")
        con.execute(
            f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_parquet(?)",
            [str(path)],
        )


def hash_ground_truth(
    parquet_dir: Path,
    results_db: ResultsDB,
    scale_factor: int = 1,
    suite: str = "tpcds",
) -> dict[str, dict]:
    """Run every registered query for ``suite`` against the given Parquet dir.

    Args:
        parquet_dir: Directory containing the suite's <table>.parquet files.
        results_db: Where ground_truth_hashes lives.
        scale_factor: Scale factor label to record alongside each hash.
        suite: "tpcds" or "dsb" — controls which block of the registry runs.
            ClickBench is hashed via the separate
            ``hash_clickbench_ground_truth`` flow because it has only one
            table (``hits``) instead of the 24 TPC-DS-shaped tables.

    Returns:
        Per-query dict of {result_hash, row_count, execution_ms, error}.
    """
    if suite not in {"tpcds", "dsb"}:
        raise ValueError(f"unsupported suite '{suite}'; expected 'tpcds' or 'dsb'")

    registry = QueryRegistry()
    queries = [q for q in registry.all() if q.block == suite]
    logger.info("hashing %d %s queries against SF%d", len(queries), suite, scale_factor)

    duck = duckdb.connect(":memory:")
    try:
        _load_tables_into_duckdb(duck, parquet_dir, TPCDS_TABLES)
        duckdb_version = duck.execute("SELECT version()").fetchone()[0]
        summary: dict[str, dict] = {}
        rows_to_insert: list[tuple] = []

        for q in queries:
            start = time.perf_counter()
            err: str | None = None
            result_hash: str | None = None
            row_count: int | None = None
            serialized_rows: str | None = None
            serialized_columns: str | None = None
            try:
                cur = duck.execute(q.sql)
                result = cur.fetchall()
                columns = [d[0] for d in cur.description] if cur.description else []
                row_count = len(result)
                result_hash = hash_query_result(result, q.sql)
                serialized_rows = serialize_rows(result)
                serialized_columns = serialize_columns(columns)
            except Exception as e:
                err = str(e)
                logger.error(
                    "ground-truth query failed",
                    extra={"query_id": q.id, "block": q.block},
                    exc_info=True,
                )
            execution_ms = (time.perf_counter() - start) * 1000

            summary[q.id] = {
                "result_hash": result_hash,
                "row_count": row_count,
                "execution_ms": execution_ms,
                "error_message": err,
            }
            rows_to_insert.append(
                (
                    q.id,
                    q.block,
                    scale_factor,
                    result_hash,
                    row_count,
                    execution_ms,
                    err,
                    duckdb_version,
                    serialized_rows,
                    serialized_columns,
                )
            )

        results_db.record_ground_truth(rows_to_insert)
        ok = sum(1 for s in summary.values() if s["error_message"] is None)
        logger.info(
            "%s ground-truth complete: %d/%d ok, %d failures",
            suite,
            ok,
            len(summary),
            len(summary) - ok,
        )
        return summary
    finally:
        duck.close()

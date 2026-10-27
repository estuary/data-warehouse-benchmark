"""The query catalog, as data.

The results exports name queries by id (``tpcds_q07``) and nothing else. A
reader browsing per-query results wants to know what a query is: which suite it
belongs to, which pattern tags the analysis gave it, and where its SQL lives for
each engine. This export is one row per canonical query with exactly that, read
from the same :class:`benchmark.queries.registry.QueryRegistry` the pattern
exports tag from, so a query's tags here are the tags the pattern views pool.

``sql_path`` is the repository-relative path of the query file, with
``{platform}`` standing in for the dialect directory (``duckdb`` for the
canonical text, or any engine id), so a consumer can link to any dialect without
this export repeating the path seven times.

``returns_pair`` marks the TPC-DS/DSB pairs (query numbers 1-99, both suites)
whose SQL touches a ``*_returns`` table, where DSB also enlarges the tables
queried. It is :func:`benchmark.report.verifiers.returns_qnums`, the split the
report's skew comparison makes, so a consumer can take the report's clean-pair
median (distribution changed, table sizes held) instead of pooling all pairs.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from ..queries.registry import QueryRegistry
from ..report.verifiers import returns_qnums

logger = logging.getLogger("benchmark.analysis.query_catalog")


def emit_query_catalog(
    output_path: Path = Path("results/exports/query_catalog.parquet"),
) -> Path:
    """Emit one row per canonical benchmark query.

    Args:
        output_path: Parquet file to write.

    Returns:
        The path written.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    registry = QueryRegistry()
    returns = returns_qnums()
    rows = [
        (
            q.id,
            q.block,
            q.number,
            list(q.patterns),
            f"queries/{{platform}}/{q.block}/query_{q.number:02d}.sql",
            q.block in ("tpcds", "dsb") and q.number in returns,
        )
        for q in registry.all()
    ]
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE query_catalog (query_id VARCHAR, block VARCHAR, "
            "number INTEGER, patterns VARCHAR[], sql_path VARCHAR, returns_pair BOOLEAN)"
        )
        con.executemany("INSERT INTO query_catalog VALUES (?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY query_catalog TO '{output_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    logger.info("query catalog emitted to %s (%d queries)", output_path, len(rows))
    return output_path

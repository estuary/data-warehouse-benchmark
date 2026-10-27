"""Recorded skips, for the explorer.

A size missing from a ladder reads as an oversight unless the reader can see that it
was decided. ``scale_skips`` holds those decisions (``benchmark skip-scale``) and the
report prints them; this export gives the explorer the same rows, so a blank panel can
say "skipped: <reason>" instead of nothing.

One row per recorded skip, at the granularity it was decided: scale, suite
(``block``), compute size and run type, each '*' where the skip covers all of them.
``run_type`` reads '*' (every run type) on a database without that column.
``skipped_on`` is the UTC-naive date as text, so the browser never
has to handle a timestamp type.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

logger = logging.getLogger("benchmark.analysis.scale_skips")


def _scale_skips_sql(con: duckdb.DuckDBPyConnection) -> str:
    """The export's SELECT, tolerating a database without the ``run_type`` column."""
    columns = {r[0] for r in con.execute("DESCRIBE scale_skips").fetchall()}
    run_type = "run_type" if "run_type" in columns else "'*'"
    return f"""
    SELECT platform, scale_factor, block, compute_label, {run_type} AS run_type,
           reason, strftime(skipped_at, '%Y-%m-%d') AS skipped_on
    FROM scale_skips
    ORDER BY platform, scale_factor, block, compute_label, run_type
    """


def emit_scale_skips(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/scale_skips.parquet"),
) -> Path:
    """Emit every recorded skip.

    Args:
        db_path: Results database to read.
        output_path: Parquet file to write.

    Returns:
        The path written.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        sql = _scale_skips_sql(con)
        con.execute(f"COPY ({sql}) TO '{output_path.as_posix()}' (FORMAT PARQUET)")
        n = con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0]
    finally:
        con.close()
    logger.info("%d scale skips emitted to %s", n, output_path)
    return output_path

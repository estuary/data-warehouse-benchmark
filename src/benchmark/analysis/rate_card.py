"""The list-price rate card, as data.

Every hourly price the explorer and the report show is
:func:`benchmark.report.compute_rates.effective_usd_per_hour` applied to a
``compute_label`` stamped on a result row. The concurrency grid already carries
that rate per cell, but only for the sizes the concurrency campaign ran; sizes
measured only in the warm pass (MotherDuck Pulse, single-replica ClickHouse) and
the scan-priced BigQuery on-demand model have no grid row. This export lists
every compute size that appears anywhere in the results, with its billing unit
and its rate (NULL where the size has no hourly price), so a reader can see the
whole price list the study used without reading Python.

The billing-unit names are the vendors' own. They describe what the rate is
denominated in; the per-engine collectors in :mod:`benchmark.cost` hold the unit
prices themselves.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from ..report.compute_rates import effective_usd_per_hour

logger = logging.getLogger("benchmark.analysis.rate_card")

#: What each engine's hourly rate is denominated in.
BILLING_UNIT: dict[str, str] = {
    "snowflake": "credits",
    "databricks": "DBUs",
    "bigquery": "slot-hours",
    "redshift": "RPU-hours",
    "fabric": "capacity units",
    "clickhouse": "compute units",
    "motherduck": "Duckling-hours",
}

#: BigQuery on-demand bills by bytes scanned, so it has a unit but no hourly rate.
ON_DEMAND_UNIT = "bytes scanned"


def rate_card_rows(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str, float | None]]:
    """Every (platform, compute label) in the results, priced.

    Args:
        con: A connection to the results database.

    Returns:
        ``(platform, compute_label, billing_unit, usd_per_hour)`` rows, sorted.
    """
    labels = con.execute(
        "SELECT DISTINCT platform, compute_label FROM main.sql_benchmark_results "
        "WHERE compute_label IS NOT NULL ORDER BY 1, 2"
    ).fetchall()
    rows = []
    for platform, label in labels:
        unit = ON_DEMAND_UNIT if label == "on-demand" else BILLING_UNIT.get(platform, "")
        rate = effective_usd_per_hour(platform, label)
        # Rounded to a hundredth of a cent: the float products (credits x rate)
        # otherwise carry 32.400000000000006 into every consumer.
        rows.append((platform, label, unit, None if rate is None else round(rate, 4)))
    return rows


def emit_rate_card(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/rate_card.parquet"),
) -> Path:
    """Emit the list-price rate card for every compute size in the results.

    Args:
        db_path: Results database to read.
        output_path: Parquet file to write.

    Returns:
        The path written.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = rate_card_rows(con)
    finally:
        con.close()
    out = duckdb.connect()
    try:
        out.execute(
            "CREATE TABLE rate_card (platform VARCHAR, compute_label VARCHAR, "
            "billing_unit VARCHAR, usd_per_hour DOUBLE)"
        )
        out.executemany("INSERT INTO rate_card VALUES (?, ?, ?, ?)", rows)
        out.execute(f"COPY rate_card TO '{output_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        out.close()
    logger.info("rate card emitted to %s (%d sizes)", output_path, len(rows))
    return output_path

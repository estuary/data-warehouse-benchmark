"""MotherDuck warm/cold/suite cost views.

Three run-level cost figures, derived from recorded per-query durations + the
query span the connector captures (``start_time``/``end_time`` in each row's
metadata). The model lives in ``cost/motherduck.py``:

    warm  = sum(query_time)             × rate    (best case; no floor)
    cold  = sum(max(query_time, 1 min)) × rate    (each query its own minute)
    suite = (max(end) − min(start))     × rate    (the actual warm-window span)

Computed per (scale_factor, run_type, run_number) over non-errored MotherDuck
queries, then the median across the measured runs → one warm/cold/suite per
(scale_factor, run_type). ``rate`` comes from the captured Duckling tier
(``results/cost_config.json``), falling back to the collector default.

NOTE: ``suite`` needs ``start_time``/``end_time``; a row without them yields a
NULL span (and NULL suite_cost). Such rows are not back-filled; re-run instead.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from ..cost.discovery import DEFAULT_COST_CONFIG_PATH, load_cost_config
from ..cost.motherduck import MotherDuckCostCollector
from ..exceptions import BenchmarkError

logger = logging.getLogger("benchmark.analysis.motherduck_cost")


def _cost_sql(rate_usd_per_second: float) -> str:
    """warm/cold/suite per (scale_factor, run_type); rate is a float literal."""
    return f"""
WITH latest AS (
    -- results_latest dedups on the FULL logical key, concurrency and
    -- compute_label included, so a concurrency pass or a Duckling-tier sweep
    -- keeps its distinct measurements rather than collapsing into one "latest".
    SELECT scale_factor, run_type, run_number, wall_clock_ms,
           TRY_CAST((metadata::JSON)->>'start_time' AS TIMESTAMP) AS q_start,
           TRY_CAST((metadata::JSON)->>'end_time'   AS TIMESTAMP) AS q_end
    FROM results_latest
    WHERE platform = 'motherduck' AND error_message IS NULL
),
per_run AS (
    SELECT scale_factor, run_type, run_number,
           SUM(wall_clock_ms) / 1000.0                  AS warm_s,
           SUM(GREATEST(wall_clock_ms / 1000.0, 60.0))  AS cold_s,
           EXTRACT(EPOCH FROM (MAX(q_end) - MIN(q_start))) AS span_s
    FROM latest
    GROUP BY scale_factor, run_type, run_number
)
SELECT scale_factor, run_type,
       median(warm_s) * {rate_usd_per_second} AS warm_cost_usd,
       median(cold_s) * {rate_usd_per_second} AS cold_cost_usd,
       median(span_s) * {rate_usd_per_second} AS suite_cost_usd,
       {rate_usd_per_second} AS rate_usd_per_second
FROM per_run
GROUP BY scale_factor, run_type
ORDER BY scale_factor, run_type
"""


def emit_motherduck_cost(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/motherduck_cost.parquet"),
    cost_config_path: Path = DEFAULT_COST_CONFIG_PATH,
) -> Path:
    """Emit MotherDuck warm/cold/suite costs to Parquet for the report."""
    tier = "standard"
    try:
        cfg = load_cost_config(cost_config_path)
        tier = cfg.get("platforms", {}).get("motherduck", {}).get("tier") or "standard"
    except BenchmarkError:
        logger.warning("no cost-config file; defaulting MotherDuck tier to 'standard'")

    from ..report.views import install_views

    rate = MotherDuckCostCollector(tier).rate_usd_per_second
    output_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        install_views(con)
        con.execute(f"COPY ({_cost_sql(rate)}) TO '{output_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    logger.info("motherduck warm/cold/suite cost (tier=%s) emitted to %s", tier, output_path)
    return output_path

"""Analysis: cost curves, pattern slicing, skew impact, rankings.

The analysis layer reads the populated ``benchmark.duckdb`` and writes:
  * ``results/exports/cost_curves.parquet``      — per-engine cost vs scale
  * ``results/exports/pattern_summary.parquet``  — per-pattern latency/cost
  * ``results/exports/skew_impact.parquet``      — TPC-DS vs DSB delta (warm)
  * ``results/exports/skew_load.parquet``        — TPC-DS vs DSB ratio per concurrency cell
  * ``results/exports/rankings.parquet``          — composite scoring

All exports are Parquet so the report generator (and any external
notebook) can pull directly without re-running SQL.

Cold-vs-warm is not exported as Parquet; the report renders it inline from
``run_type`` data.
"""

from __future__ import annotations

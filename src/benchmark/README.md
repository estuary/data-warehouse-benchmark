# The Python Harness (`src/benchmark/`)

The benchmark harness. All code runs via `uv run benchmark <command>`.

## Top-Level Modules

| File | Purpose |
|------|---------|
| `__main__.py` | Entry point for `uv run benchmark`; delegates to `cli.app`. |
| `cli.py` | Typer CLI: all commands (`validate-queries`, `sql`, `report`, etc.) |
| `cli_settings.py` | Separate Typer app for `uv run settings show / generate` (typed-config inspection). |
| `settings.py` | Typed pydantic-settings tree (`BenchmarkSettings`, `get_settings()`). All env-var access flows through this: never `os.environ.get()`. |
| `results.py` | DuckDB results database (`results/benchmark.duckdb`): schema init + read/write |
| `translator.py` | sqlglot-based query translation from DuckDB SQL to per-engine dialects; per-engine `_POST_TRANSLATE_PATCHES` for gaps sqlglot misses |
| `optimize_pass.py` | Claude per-engine SQL optimization (idiomatic rewrites; not applied to any engine in the published results) |
| `equivalence.py` | Local execute-and-compare equivalence gate for `optimize` rewrites |
| `physical_design.py` | Clustering/distribution/partition spec generation + application (not applied to any engine) |
| `scales.py` / `run_plan.py` (+ `run_plan.yaml`) | The valid scale factors; per-engine compute size per scale and each engine's size ladders |
| `prepare_scale.py` / `scale_manifest.py` | `prepare-scale` readiness gate (data + compute per platform, `sizing.auto.tfvars.json`); scale-keyed readiness/cost manifest |
| `terraform_outputs.py` | Reads exported `terraform output -json`; records provisioned compute into `engine_environment`; writes the terraform-managed SA-key files |
| `provenance.py` | Run provenance (git SHA, tool versions, executed-query hash) for the `benchmark_run` table |
| `e2e.py` | Ordered driver behind `benchmark e2e` (shells the subcommands) |
| `exceptions.py` | Exception hierarchy (`BenchmarkError` -> `ConnectorError`, `QueryTimeoutError`, etc.) |
| `logging_config.py` | Rich console + JSON audit trail logging (no `print()` allowed) |

## Subpackages

### `connectors/`: Database Connectors
One connector per engine (Snowflake, Databricks, BigQuery, Redshift, Fabric, ClickHouse, MotherDuck) plus abstract `base.py`, `registry.py`, and `connection.py` (connect/reconnect with retry and backoff). All support context managers (`with connector() as conn:`).

### `cost/`: Cost Telemetry
Per-engine cost collectors with confidence tiers: Direct (BigQuery on-demand), Proportional (Databricks, Redshift, Fabric, BigQuery reservations), Estimated (ClickHouse, MotherDuck, Snowflake); the buyer's guide names them Measured, Allocated and Modeled. Same registry pattern as connectors; `discovery.py` builds `results/cost_config.json` (`capture-cost-config`) and picks each run's collector from it.

### `compute/`: Out-of-Band Compute
- `bigquery_reservation.py`: Resizes the BigQuery Enterprise reservation for slot sweeps (`bq-reservation`, `sql --compute-size`)

### `datagen/`: Data Generation
- `tpcds.py` / `dsb.py` / `clickbench.py`: Generate/download benchmark data
- `ground_truth.py`: DuckDB-based result hashing for correctness validation
- `collection_schemas.py`: JSON schemas for the Estuary collections (24 TPC-DS + 24 DSB tables per scale, SF1–SF1000, plus ClickBench)

### `sql_benchmark/`: SQL Benchmark Execution
- `runner.py`: Warm pass executor (1 warmup + 3 measured runs, resumable, atomic recording)
- `cold.py`: Cold pass with per-engine cache flush
- `concurrency.py`: Concurrency levels (pre-warmed sessions released together at a `threading.Barrier`) and the sustained-load pass
- `validator.py`: Result correctness checking for `validate-sf1` (element-wise compare at 1e-4 tolerance; hash compare where only a hash is stored)


### `staging/`: Data Loading & Validation
- `s3.py`: Parquet upload to S3 staging bucket
- `estuary.py`: flowctl wrapper (authenticate, publish captures/materializations); `${VAR}` substitution from `BenchmarkSettings.env_dict()`
- `estuary_metrics.py`: Estuary OpenMetrics client behind `flow-metrics`
- `clickhouse_prepare.py`: Pre-publish step that reads `clickhouse.flow.yaml` and issues `CREATE DATABASE IF NOT EXISTS` for each materialization target (the `materialize-clickhouse` connector creates tables but not databases). Hooked into `flow-publish clickhouse`; also exposed as `clickhouse-prepare-databases`.
- `motherduck_local.py`: Direct Parquet->MotherDuck loader (Estuary bypass)
- `load_check.py`: Row-count validation across engines
- `expected_counts.py` / `checksums.py`: Manifest generation

### `analysis/`: Results Analysis

Most of these emit a Parquet file into `results/exports/` for `benchmark analyze`.

Concurrency, the campaign's main axis:
- `concurrency_shape.py`: Reduces each cell to comparable scalars: the queueing
  exponent, the shed rate, and useful concurrency at an SLA. Read its module
  docstring before quoting any of them; it explains why the exponent must never
  be published as a ranking.
- `concurrency_grid.py`: Per-level export carrying that reducer's
  SLA-independent constants, so a consumer can re-target the SLA itself. Two
  workloads in one grid: TPC-DS + DSB per scale, and ClickBench with its
  campaign tags pooled (one fixed table) on the unified level ladder.
- `pattern_load.py`: Per-pattern latency under concurrency, with the in-cell
  base needed to normalize it across compute sizes.
- `skew_load.py`: TPC-DS-vs-DSB skew ratio under concurrency, per cell

Warm / cross-cutting:
- `engine_time.py`: The one definition of engine time that latency and cost use
- `curves.py`: Cost/latency scaling curves across scale factors
- `size_ladder.py`: Warm results at every compute size
- `patterns.py`: Query pattern slicing (joins, window functions, aggregations)
- `skew.py`: TPC-DS-vs-DSB comparison (quantifies the skew penalty per engine)
- `clickbench.py`: ClickBench per-query summary
- `rankings.py`: Composite rankings (latency + cost rank per engine)
- `agreement.py`: Cross-engine row-count agreement; the correctness evidence above SF1
- `coverage.py`: Run-plan completeness audit (`coverage-audit`): what the plan implies, minus what was measured
- `rate_card.py` / `query_catalog.py` / `scale_skips.py`: Explorer exports of the rate card, the query catalog and the recorded skips

Cost repair:
- `reconcile.py`: Post-run authoritative server time + reconciled cost
- `recost.py`: Re-prices already-recorded rows from their stored metadata
- `motherduck_cost.py`: MotherDuck warm/cold/suite cost views

### `report/`: Report Generation
- `generator.py`: Markdown + chart generation (interactive plotly fragments, plus PNGs when kaleido can render them)
- `html_export.py`: Packages `draft.md` into a self-contained `draft.html`
- `narrative.py`: Composes the authored prose in `report/sections/*.md` at its
  anchors, substituting `{{fact:...}}` values (also used by `render-status`)
- `figures.py` / `verifiers.py`: The number-drift gate (`verify-figures`), which
  recomputes every marked table from the database and fails on drift
- `views.py`: The single latest-wins dedup layer; `install_views` everywhere
- `canonical.py` / `policy.py`: Designated compute tier per scale; which measurements enter cross-engine comparisons
- `compute_rates.py`: List-price USD/hour per compute-size label, per engine
- `display.py` / `verdicts.py`: Engine display names; the Decision Guide's editorial verdicts

### `queries/`: Query Registry
- `registry.py`: Loads all 244 queries with metadata (block, number, pattern tags) for a dialect

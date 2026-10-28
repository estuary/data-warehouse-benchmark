# 2026 Cloud Data Warehouse Benchmark

Get the full report [here](https://estuary.dev/data-warehouse-benchmark-report/).

Run jointly by [Estuary](https://estuary.dev) and [CorrDyn](https://corrdyn.com). CorrDyn designed the benchmark, wrote the code, and ran it end to end. Estuary is the apparatus for data replication across the seven warehouses: one capture fed all seven from the same stored copy of the data. Estuary was not one of the systems measured or compared.

## What This Is

A reproducible, open-source benchmark comparing **7 cloud data warehouse platforms** on SQL performance. The question is **how latency and cost scale as a function of the warehouse (engine + compute size) and the workload type**. Every experiment measures **cost**, **latency**, and **quality**, each reported separately with no composite scores, so readers can apply their own weighting.

The results are a snapshot of Q3 2026, measured between July and October 2026.

### Platforms Benchmarked

Snowflake, Databricks SQL, Google BigQuery, Amazon Redshift Serverless, Microsoft Fabric, ClickHouse Cloud, and MotherDuck.

### SQL Benchmarks

**244 benchmark queries** spanning three query suites:
- **TPC-DS** (99 queries): star-schema business reporting, a large sales table joined to smaller tables of customers, products, stores and dates
- **DSB** (102 queries): the same reports against the same tables with skewed data, plus three queries with no TPC-DS counterpart
- **ClickBench** (43 queries): filtering and counting over one very wide table of web page views, with no joins

Executed at data volumes of 1 GB (SF1), 10 GB (SF10), 100 GB (SF100), and 1 TB (SF1000). Each configuration runs a warm pass (1 warmup + 3 measured runs, median reported) and a concurrency pass (bursts of 1/5/10/20/50/100 simultaneous sessions), with a ten-minute limit per query. Compute-size sweeps cover five or more compute sizes per engine, from at or near each vendor's smallest size up to the sizes a 1 TB workload calls for (Snowflake warehouse tiers, BigQuery slot reservations, Redshift RPU bands, Fabric capacities, MotherDuck Duckling tiers, ClickHouse replica memory), each pinned at one size with automatic scaling switched off. The harness also has a cold pass (`benchmark cold`); the published results do not include one.

> This is not an audited TPC result. The TPC-DS and DSB workloads used here are derived from TPC-DS, and the results are not comparable to published TPC-DS results.

## Key Architectural Decisions

**Per-engine SQL as the canonical query form.** All 244 benchmark queries start in canonical DuckDB SQL. The TPC-DS and DSB queries are translated mechanically via sqlglot to each platform's dialect (MotherDuck runs them untranslated); a few carry small hand-edits, such as rounding or an explicit cast, where a query otherwise returned wrong results. These are corrections, not tuning. The ClickBench queries use the SQL published in the ClickBench project for each engine (Fabric, which has none, uses the translation). The harness includes a per-engine Claude optimization pass (`benchmark optimize`), but it was not applied to any engine in the published results. All engines benchmark against the Estuary-loaded tables as-is: no per-engine physical design (clustering, distribution or sort keys, partitioning, hand-built statistics) is applied. The per-engine SQL files live in `queries/<platform>/` and are what the benchmark executes.

**Estuary for data movement.** Data is generated (TPC-DS/DSB/ClickBench), exported to Parquet, and loaded to all 7 platforms via Estuary Flow only. No native loading tools. This provides a consistent, auditable data path.

**Cost tracking with confidence tiers.** Every engine is priced the same way: the list rate of the compute a query ran on × the time the engine spent on it (including any wait in the engine's queue, excluding network time), with queries that ran together under load splitting the time the compute was busy. Each figure is labeled by how closely the vendor's bill confirms it:
- **Measured**: the charge the vendor billed for that query (BigQuery on-demand)
- **Allocated**: checked against what the vendor billed for the compute, divided among the queries that ran on it (Databricks, Redshift, Fabric, BigQuery reservations)
- **Modeled**: list rate × time, with no vendor bill to check the total against (ClickHouse, MotherDuck, Snowflake)

Each result row stores its tier in the `cost_confidence` field (`direct` / `proportional` / `estimated` respectively) so readers understand methodology.

**Multi-view analysis.** Results are reported by query, by platform, by scale factor, by compute size, and by cost tier, so readers can slice across dimensions relevant to their use case.

## Getting the repo

The results database, the analysis exports and the DuckDB-WASM module ship with
the repo via **Git LFS** (~105 MB). Install LFS before cloning, or the files
arrive as ~130-byte pointer stubs and every tool that reads them fails oddly:

```bash
git lfs install
git clone https://github.com/estuary/data-warehouse-benchmark
```

Already cloned without it? `git lfs install && git lfs pull`.

## Quick Start

Because the results ship with the repo, you can read the whole campaign without
credentials, cloud spend, or a single query run.

**Explore what was measured**, no accounts needed:

```bash
uv sync

# Rebuild the report (markdown + charts + HTML) from the committed database
uv run benchmark report

# Or open the interactive explorer: seven engines, every measured warehouse size,
# 1-100 concurrent sessions. Runs entirely in the browser via DuckDB-WASM.
cd viz && npm install && npm run build && npx serve dist
```

The explorer is also published to GitHub Pages on each push to `main` that touches
the explorer or the exports:
<https://warehouse-benchmark.estuary.dev/>
(built, deployed and smoke-tested by `.github/workflows/viz.yml`; the browser
suite runs locally before a push, or on demand in CI; see `viz/README.md`).

Query the database directly (the `duckdb` Python package is installed by `uv sync`):

```bash
uv run python -c "
import duckdb
duckdb.connect('results/benchmark.duckdb', read_only=True).sql('''
  SELECT platform, run_type, count(*) AS measurements,
         round(median(server_time_ms)) AS median_ms
  FROM sql_benchmark_results GROUP BY 1, 2 ORDER BY 1, 2''').show()"
```

**Run your own benchmark**. This needs cloud credentials and does cost money.
To run the TPC-DS block at SF1 (the smallest scale) against MotherDuck, loading
the data directly rather than through Estuary:

```bash
# 1. Install dependencies and set up environment
uv sync
cp env/motherduck.env.example env/motherduck.env
# Fill in env/motherduck.env with your credentials (at minimum: MOTHERDUCK_TOKEN).
# Each engine reads its own env/<section>.env file; `uv run settings show` lists
# every variable, its default, and where its current value came from.

# Write to your own results DB so the shipped one stays as published.
DB=results/my-run.duckdb

# 2. Generate SF1 TPC-DS data and hash its ground truth in DuckDB (local, no spend)
uv run benchmark generate-tpcds --scales 1
uv run benchmark validate-queries --scale 1 --suites tpcds --results-db $DB

# 3. Load it into MotherDuck (benchmark_db.tpcds_sf1), bypassing Estuary
uv run benchmark load-motherduck-local

# 4. Run the TPC-DS block at SF1 on MotherDuck, then check it against the ground truth
uv run benchmark sql --scale 1 --platforms motherduck --blocks tpcds --results-db $DB
uv run benchmark validate-sf1 --platforms motherduck --blocks tpcds --results-db $DB
```

For a complete benchmark on all 7 platforms, see `docs/OPERATOR_GUIDE.md`.

## Repository Structure

```
data-warehouse-benchmark/
├── LICENSE                            # Apache License 2.0
├── NOTICE                             # Copyright, third-party material and trademarks
├── LICENSES/                          # Third-party license texts (DuckDB, DSB, Inter)
├── .claude/
│   └── CLAUDE.md                      # Claude Code project instructions
├── docs/
│   ├── README.md                     # Index of the docs
│   ├── ARCHITECTURE.md               # How the harness works (start here)
│   ├── OPERATOR_GUIDE.md             # Human operator guide
│   └── E2E_LOADING.md                # Full generate→load→benchmark pipeline
├── terraform/
│   ├── modules/
│   │   ├── snowflake/
│   │   ├── databricks/                # workspace catalog, schemas, SQL warehouse
│   │   ├── databricks_azure/          # the Azure workspace itself
│   │   ├── bigquery/
│   │   ├── redshift/
│   │   ├── fabric/
│   │   ├── clickhouse/
│   │   ├── motherduck/
│   │   ├── shared/                    # S3 staging bucket + Estuary reader IAM
│   │   └── datagen/                   # opt-in EC2 box for SF100+ data generation
│   ├── main.tf                        # root stack, including its outputs
│   ├── variables.tf
│   └── terraform.tfvars.example
├── estuary/                           # Flow capture + per-engine materialization specs
├── pyproject.toml                     # Root Python project (uv)
├── src/benchmark/
│   ├── connectors/                    # Platform-specific SQL adapters
│   │   ├── base.py
│   │   ├── snowflake.py
│   │   ├── databricks.py
│   │   ├── bigquery.py
│   │   ├── redshift.py
│   │   ├── fabric.py
│   │   ├── clickhouse.py
│   │   └── motherduck.py
│   ├── cost/                          # Per-platform cost telemetry
│   ├── sql_benchmark/                 # Query execution harness
│   ├── translator.py                  # sqlglot translation engine
│   ├── cli.py                         # Unified CLI entry point
│   └── __main__.py                    # Entry point for `uv run benchmark`
├── queries/
│   ├── duckdb/                        # Canonical queries (source of truth)
│   │   ├── tpcds/                     # 99 TPC-DS queries
│   │   ├── dsb/                       # 102 DSB queries
│   │   └── clickbench/                # 43 ClickBench queries
│   ├── snowflake/
│   ├── databricks/
│   ├── bigquery/
│   ├── redshift/
│   ├── fabric/
│   ├── clickhouse/
│   ├── motherduck/
│   └── CLICKBENCH_NOTICE.md           # ClickBench queries: CC BY-NC-SA 4.0, not Apache
├── results/
│   ├── benchmark.duckdb               # Central results DB — ships via Git LFS
│   ├── cost_config.json               # Captured cost drivers (required by the measured passes)
│   └── exports/                       # Parquet analysis exports (Git LFS)
├── report/
│   ├── sections/                      # Hand-authored narrative (tracked)
│   └── draft.md, assets/              # Rebuilt by `benchmark report` (not tracked)
├── viz/                               # Client-side explorer (Mosaic + DuckDB-WASM)
│   ├── src/                           # Coordinated views over the Parquet exports
│   └── public/duckdb/                 # DuckDB-WASM module (Git LFS)
├── scripts/                           # Resize, sweep and DB-maintenance utilities
├── tests/                             # unit, scenario and integration tests (pytest)
└── env/                               # Per-section env files (*.env.example templates)
```

## CLI Reference

The benchmark is controlled via the unified CLI: `uv run benchmark <command> [options]`.
Every `--platforms` option takes a comma list of engines or `all`: snowflake,
databricks, bigquery, redshift, fabric, clickhouse and motherduck, in that order
(`reconcile` leaves out ClickHouse, which has no reconciler).

### Common Commands

| Goal                                  | Command                                                                                  |
|---------------------------------------|------------------------------------------------------------------------------------------|
| Dialect gate for all 244 queries      | `uv run benchmark validate-dialects`                                                     |
| Hash DuckDB ground truth (TPC-DS + DSB; needs generated data) | `uv run benchmark validate-queries --scale 1`                  |
| Inspect harness config (typed env)    | `uv run settings show` / `uv run settings show -s SNOWFLAKE --show-secrets`              |
| Re-generate `env/*.env.example`       | `uv run settings generate --dir env`                                                     |
| Run SQL benchmark at a scale          | `uv run benchmark sql --scale 1 --platforms motherduck`                                  |
| Run SQL on all 7 platforms            | `uv run benchmark sql --scale 10 --platforms all`                                        |
| Run concurrency test at SF100         | `uv run benchmark concurrency --scale 100 --platforms <engines>`                         |
| Run cold-pass at SF1000               | `uv run benchmark cold --scale 1000 --platforms <engines>`                               |
| Sweep a scale across compute sizes    | `uv run benchmark sql --scale 10 --platforms snowflake --force --compute-size small`     |
| Skip a scale on a platform            | `uv run benchmark skip-scale --scale 1000 --platforms clickhouse --reason "<text>"`      |
| Show progress / coverage              | `uv run benchmark status`                                                                |
| Generate report draft                 | `uv run benchmark report`                                                                |
| Re-translate canonical -> per-engine SQL (overwrites the committed files, hand patches and imported ClickBench SQL included) | `uv run benchmark translate --platforms <engine>` |
| Validate SF1 correctness vs DuckDB    | `uv run benchmark validate-sf1 --platforms <engine>`                                     |
| Spot-check correctness at another scale | `uv run benchmark validate-queries --scale 100` then `validate-correctness --scale 100 --platforms <engine>` |
| Publish an Estuary materialization    | `uv run benchmark flow-publish <engine>` (every suite × scale task; `--scale N --suites tpcds,dsb` publishes a subset) |
| Validate Estuary loads (row counts)   | `uv run benchmark validate-loads --platforms <engine> --suites tpcds,dsb,clickbench`     |

### Setup

```bash
# 1. Install dependencies via uv
uv sync

# 2. Copy and fill in environment variables (one file per engine/section)
for f in env/*.env.example; do cp "$f" "${f%.example}"; done
# Edit each env/<section>.env with your cloud credentials
# (templates are generated from src/benchmark/settings.py;
#  regenerate after schema changes with `uv run settings generate --dir env`)

# 3. Confirm config + connectivity, then run a stage
uv run settings show              # which env vars are populated
uv run benchmark connectivity-check --platforms all
uv run benchmark validate-dialects # query-suite stage example (no data needed)
```

The warm pass is resumable: each per-query result is recorded atomically to `results/benchmark.duckdb`, so re-running the same `sql` command picks up where it left off (pass `--force` to re-execute already-completed queries). The cold, concurrency and sustained passes run in full each time.

## Pipeline

The benchmark runs as an ordered pipeline: check the query suite's translations,
generate and stage the data and hash its DuckDB ground truth, provision the engines,
load via Estuary, validate (and optionally optimize) the per-engine queries, run the
warm and concurrency passes (and optionally a cold pass), reconcile cost, analyze, and
generate the report. Each stage ends in an explicit validation gate. The stage-by-stage
commands are under "Pipeline stages" in `.claude/CLAUDE.md`, each command's reference is
`docs/ARCHITECTURE.md` §3, and the human runbook is `docs/OPERATOR_GUIDE.md`.

## How to Contribute

This is a community benchmark. We welcome:
- Query additions (canonical DuckDB SQL under `queries/duckdb/`, with ground truth hashed by `benchmark validate-queries`)
- Platform modules (fork -> add `terraform/modules/your-platform/` -> PR)
- Bug reports, and per-engine correctness fixes (a query that returns a wrong result on one engine). Speed-only rewrites are out of scope: every engine runs the same translated SQL.

## License

Copyright 2026 Estuary Technologies, Inc. and Correlation Dynamics, Inc. Licensed
under the [Apache License, Version 2.0](LICENSE).

Third-party material keeps its own license; [`NOTICE`](NOTICE) lists it. In particular,
the ClickBench queries under `queries/*/clickbench/` come from
[ClickBench](https://github.com/ClickHouse/ClickBench) and are licensed under
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/), not Apache 2.0
(see [`queries/CLICKBENCH_NOTICE.md`](queries/CLICKBENCH_NOTICE.md)). The Estuary and
CorrDyn names and logos are trademarks of their owners and are not licensed for reuse.

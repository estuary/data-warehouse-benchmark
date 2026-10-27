# Utilities Beside the CLI (`scripts/`)

Everything here sits outside `uv run benchmark` on purpose, grouped by kind:

```
scripts/
├── data/       Build the query suite / convert a vendor data format. Run once.
├── resize/     Per-engine compute sizing. Long-lived; each wraps a vendor API.
├── sweeps/     Multi-size sweep drivers that resize between cells.
├── ops/        Operational helpers used around a run.
└── flowctl     PATH shim — must stay at this level (see below).
```

`flowctl` sits at the top on purpose: `.mise.toml` puts `./scripts` on `PATH`
(`_.path = ["./scripts"]`), so moving it into a subdirectory would take
`flowctl` off the path for every Estuary command.

Scripts locate the repo root relative to their own file, so they run correctly
from anywhere but **must stay at their current depth**: both idioms in use
(`Path(__file__).resolve().parent.parent.parent` and
`cd "$(dirname "$0")/../.."`) count directory levels.

## Data preparation

| Script | Purpose |
|---|---|
| `extract_tpcds_queries.py` | Extracts the 99 TPC-DS queries from DuckDB's `tpcds` extension -> `queries/duckdb/tpcds/`. Run directly. |
| `build_dsb_queries.py` | Copies TPC-DS queries 1–99 as DSB equivalents and hand-instantiates the 3 DSB-specific queries (100–102) -> `queries/duckdb/dsb/`. Invoked by `benchmark generate-dsb`. |
| `dsb_dat_to_parquet.py` | Converts Microsoft DSB pipe-delimited `.dat` files (`data/dsb/sf<N>_dat/`) to ZSTD Parquet -> `data/dsb/sf<N>/`; the scale is the first argument (default 1). Run directly; `benchmark generate-dsb` does its own conversion. |

## Compute resizing

Each sizes one engine to the rung declared in `src/benchmark/run_plan.yaml`.
All pin the engine to a fixed allocation: benchmarks never run against an
autoscaling target, since a moving compute size makes a measurement
uninterpretable.

| Script | Notes |
|---|---|
| `redshift_resize.py` | Serverless workgroup -> run-plan RPU (max pinned == base). Syncs `sizing.auto.tfvars.json`, then runs the `capture-cost-config` + `prepare-scale` gates. |
| `clickhouse_resize.py` | ClickHouse Cloud service -> a run-plan rung, single replica. At one replica the capacity dial is memory; replicas would change the question being asked. |
| `databricks_resize.py` | Serverless SQL warehouse -> `--size`, pinned to one cluster (`--max-clusters` raises the ceiling for loads only); syncs `sizing.auto.tfvars.json`. |
| `fabric_resize.py` | Fabric capacity -> run-plan SKU; also suspends/resumes, which is how Fabric billing is stopped between passes. |
| `motherduck_resize.py` | Duckling size via the REST API. **Account-level**, not per-connection: it cannot be set from a connection string or Terraform. |

## Sweep drivers

| Script | Purpose |
|---|---|
| `redshift_sweep.py` | Walks scale × RPU band × concurrency level, resizing per band, one `benchmark concurrency` call per level (`--run-type warm` runs one `benchmark sql` pass per cell instead). Ejects a cell once more than `--max-timeouts` queries time out (default 0) and moves up a band. See the operator guide's Redshift band sweep section. |

## Operational utilities

| Script | Purpose |
|---|---|
| `backup_results_db.sh` | Snapshots `results/benchmark.duckdb` (+ `cost_config.json`) into `results/backups/`. Takes a tag: `./scripts/ops/backup_results_db.sh <tag>`. |
| `check_compute_labels.py` | Asserts every row a pass just wrote carries the compute label it was meant to: the guard against a resize that silently did not take. |
| `check_fabric_passes.py` | Checks that every row of a Fabric pass has engine time (and, with `--require-cpu`, CPU time) and that none was served from the result-set cache. |
| `compact_db.py` | Rewrites the results database into a fresh file, verifies it matches, and swaps it in, reclaiming the dead space DuckDB never returns. |
| `set_aside_rows.py` | Marks rows `metadata.set_aside` so the report views skip them without deleting anything. |
| `flowctl` | PATH shim so `flowctl` resolves via mise's `_.path`. Prefers the mise-managed install, falls back to the system binary. |

## Running them

The data-preparation scripts are the only ones with a CLI wrapper, and only one
of them:

```bash
uv run benchmark generate-dsb           # calls build_dsb_queries.py
uv run python scripts/data/extract_tpcds_queries.py   # standalone
uv run python scripts/data/dsb_dat_to_parquet.py 1    # standalone; needs data/dsb/sf1_dat/
```

Resizing and sweep scripts are run directly and need the relevant engine's
credentials in `env/<section>.env`.

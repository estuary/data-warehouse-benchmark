# Estuary Flow Data Pipeline (`estuary/`)

Estuary capture and materialization specs that move benchmark data from S3 into each engine. Every engine writes each suite at each scale into its own schema (database on ClickHouse, dataset on BigQuery): `tpcds_sf<N>` and `dsb_sf<N>` for N = 1, 10, 100, 1000, plus a single `clickbench`. One materialization task per suite and scale (and one for ClickBench) is the base layout; where a single task would backfill the largest fact tables too slowly, the facts get their own tasks writing into the same schema.

## Structure

```
estuary/
├── captures/
│   ├── source-s3.flow.yaml          One S3 capture, 145 bindings: TPC-DS + DSB at SF1/SF10/SF100 + ClickBench
│   └── source-s3-sf1000.flow.yaml   SF1000 only: 24 capture tasks, 48 bindings (alias sf1000-captures)
├── collections/
│   └── clickbench_hits.schema.json  Committed ClickBench collection schema
└── materializations/                 Tasks per engine (one YAML each)
    ├── bigquery.flow.yaml            9  → datasets {tpcds,dsb}_sf<N>, clickbench
    ├── motherduck.flow.yaml          9  → benchmark_db.{tpcds,dsb}_sf<N>, clickbench
    ├── databricks.flow.yaml          15 → <catalog>.{tpcds,dsb}_sf<N>, clickbench
    ├── fabric.flow.yaml              15 → <DW>.{tpcds,dsb}_sf<N>, clickbench
    ├── snowflake.flow.yaml           15 → BENCHMARK_DB.{TPCDS,DSB}_SF<N>, CLICKBENCH
    ├── redshift.flow.yaml            21 → <db>.{tpcds,dsb}_sf<N>, clickbench
    └── clickhouse.flow.yaml          21 → databases {tpcds,dsb}_sf<N>, clickbench
                                           (ClickHouse has no schema-inside-database
                                           concept; each suite and scale is its own database)
```

The 9-task specs carry one task per suite and scale plus ClickBench. The 15-task specs (Databricks, Fabric, Snowflake) add three fact-table tasks per suite at SF1000 (store sales, catalog sales, and web sales with inventory); the 21-task specs (Redshift, ClickHouse) add the same three per suite at SF100 as well. One Estuary task is one sequential pipeline, so the split lets the big facts backfill in parallel; where a suite is split, its main task carries the remaining 20 tables.

## Data Flow

```
S3 bucket (data/)
    │
    ├── source-s3 capture (145 bindings)
    │     ├── 24 TPC-DS tables × SF1/SF10/SF100   → CorrDyn/benchmark/tpcds/sf<N>/<table>
    │     ├── 24 DSB tables    × SF1/SF10/SF100   → CorrDyn/benchmark/dsb/sf<N>/<table>
    │     └── 1 ClickBench hits table             → CorrDyn/benchmark/clickbench/hits
    │
    └── sf1000-part01..24 captures (48 bindings)
          └── 24 TPC-DS + 24 DSB tables at SF1000 → CorrDyn/benchmark/{tpcds,dsb}/sf1000/<table>
    │
    ▼
193 collections → per-engine materializations (9 to 21 tasks per engine, above)
```

`CorrDyn` stands for the `ESTUARY_ACCOUNT` catalog prefix. The SF1000 bindings live only in `source-s3-sf1000.flow.yaml`: if both specs carried them, the shared collections would ingest every document twice. Its tasks are committed with `shards.disable: true`, so publishing it creates them paused; see the spec header.

## Usage

Managed via `flowctl` CLI commands, wrapped by the benchmark harness:

```bash
uv run benchmark flow-auth                                   # uses ESTUARY_REFRESH_TOKEN from env/estuary.env
uv run benchmark flow-publish source-s3                      # alias resolved to captures/source-s3.flow.yaml
uv run benchmark flow-publish sf1000-captures                # alias resolved to captures/source-s3-sf1000.flow.yaml
uv run benchmark flow-publish clickhouse                     # alias resolved to materializations/clickhouse.flow.yaml
# Other aliases: motherduck, redshift, fabric, snowflake, bigquery, databricks
# Or pass a literal path: uv run benchmark flow-publish estuary/materializations/<engine>.flow.yaml
uv run benchmark flow-publish redshift --scale 100 --suites tpcds   # scoped subset; --backfill N re-reads history
uv run benchmark flow-disable redshift --scale 100           # pause before a measured run (--enable resumes)
```

The ClickHouse alias also runs `clickhouse-prepare-databases` first (the `materialize-clickhouse` connector creates tables but not databases, so the harness `CREATE DATABASE IF NOT EXISTS`-es each target up front). `flow-render` writes the substituted spec to `estuary/.applied/` (git-ignored) without publishing; `flow-reset`, `flow-metrics` and `flow-delete` are described in `docs/ARCHITECTURE.md` §3.

## Notes

- YAML files use `${VAR}` placeholders resolved at publish time from `BenchmarkSettings.env_dict()` (every required var must be set in its `env/<section>.env` file or publish fails fast with a missing-vars error).
- The capture's collection schemas are not in the YAML: `flow-publish` injects typed JSON schemas for every capture target (`datagen/collection_schemas.py`; ClickBench from `collections/clickbench_hits.schema.json`), so the materializers create native columns instead of a single JSON document column.
- ClickHouse uses port 9440 (Native TCP-secure) in the materialization spec, different from the harness's HTTPS port 8443, because Estuary's materializer speaks the native protocol.
- Credentials are never stored in YAML: all come from the `env/<section>.env` files via the harness.
- For platforms requiring extra staging credentials (BigQuery GCS, Fabric Azure Storage, Redshift and MotherDuck S3), the harness reads those out of the typed settings tree as well.
- Databricks materializations write through `DATABRICKS_LOADER_SQL_HTTP_PATH` (defaults to the benchmark warehouse); point it at a second warehouse to keep a load off the one being measured.

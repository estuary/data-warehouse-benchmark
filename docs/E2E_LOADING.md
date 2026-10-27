# End-to-End Loading + Benchmark (`benchmark e2e`)

Runs the full production pipeline for one or more engines, in order:

```
generate → ground truth (validate-queries, hash-clickbench-ground-truth; local DuckDB)
→ checksums + expected counts → stage to S3
→ Estuary capture (source-s3, whole spec) → [per engine] materialize (whole spec)
→ validate-loads --wait (row counts in the target)
→ capture-cost-config (only when results/cost_config.json is missing)
→ [per engine] sql (warm) → analyze → report
```

Each spec is published whole. The capture and materializations are declarative and
run continuously, so each scale's data flows as it lands in S3 without a per-run
republish. Whether a given scale's data is present is a *runtime* fact, checked in
the target by `validate-loads` (scoped by `--suites` to what the run touches): not
encoded into the spec. The ground truth is hashed locally (DuckDB, no creds) right
after generation so `validate-sf1` has something to compare against (the `sql`
runners record latency and cost only); `e2e` does not run `validate-sf1` itself.

`benchmark e2e` is a thin, ordered **driver** over the existing subcommands: it
shells out to each, so it runs exactly what you'd run by hand (no separate code
path). The plan is pure + unit-tested (`tests/unit/test_e2e.py`); execution needs
real credentials.

## Prerequisites (real run)

1. **Credentials**: populate each `env/<section>.env` from its `.env.example`
   (AWS, Estuary, and every engine you target). See `.claude/CLAUDE.md`
   -> *Configuration & Settings*.
2. **Infra**: `terraform -chdir=terraform apply` (S3 staging bucket + IAM + the
   warehouses).
3. **Estuary auth**: `flowctl` on PATH + `uv run benchmark flow-auth`
   (uses `ESTUARY_REFRESH_TOKEN`).
4. **Reachability**: `uv run benchmark connectivity-check --platforms all`.
5. **DSB generator**: Docker on macOS; on Linux the harness builds
   `microsoft/dsb`'s generator (git + a C toolchain).

## Run

```bash
uv run benchmark e2e --platforms snowflake --scale 1                 # one engine
uv run benchmark e2e --platforms snowflake,bigquery,clickhouse --scale 1
uv run benchmark e2e --platforms all --scale 1                       # all 7
```

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--platforms` | `all` | comma list or `all` (snowflake, databricks, bigquery, redshift, fabric, clickhouse, motherduck) |
| `--scale` | `1` | scale factor (1/10/100/1000) |
| `--blocks` | `tpcds,dsb,clickbench` | which query blocks to generate/run |
| `--from` | none | resume from a phase key (skips earlier shared steps already done) |
| `--skip` | none | comma list of phase keys to skip |
| `--keep-going` | off | don't stop at the first failed step |
| `--load-timeout` | `3600` | seconds `validate-loads` polls (`--wait`) for the Estuary backfill to catch up after the materialization publish |
| `--recapture-cost-config` | off | run `capture-cost-config` even when `results/cost_config.json` exists (the capture merges into the file) |
| `--verbose` | off | DEBUG logging |

Phase keys: `generate-tpcds`, `generate-dsb`, `download-clickbench`,
`derive-clickbench`, `validate-queries`, `hash-clickbench-gt`, `checksums`,
`expected-counts`, `stage`, `capture`, `materialize`, `validate-loads`,
`cost-config`, `sql`, `analyze`, `report`. A block's steps are only added when it is in `--blocks`:
`generate-tpcds` for `tpcds`, `generate-dsb` for `dsb`, `validate-queries` for
either, and `download-clickbench` / `derive-clickbench` / `hash-clickbench-gt` for
`clickbench`.

## What runs (shared vs per-engine)

- **Shared (once per scale):** generate (tpcds/dsb; clickbench =
  `download-clickbench` -> `derive-clickbench-typed`) -> ground truth
  (`validate-queries --scale N` for tpcds/dsb; `hash-clickbench-ground-truth` for
  clickbench) -> `generate-checksums` -> `generate-expected-counts` (with its
  defaults it rebuilds the TPC-DS manifest; `generate-tpcds` and `generate-dsb`
  each write their own) -> `stage-data --suites <blocks>` (S3) ->
  `flow-publish source-s3` (the Estuary capture: every SF1–SF100 and ClickBench
  binding).
- **Per engine:** `flow-publish estuary/materializations/<engine>.flow.yaml`
  (every materialization task in the file), then later
  `sql --scale N --platforms <engine> --blocks <blocks>`.
- **Shared:** `validate-loads --platforms <engines> --scale N --suites <blocks> --wait <load-timeout>`
  (row counts in the target vs `data/<suite>/expected_counts.json`, ClickBench vs
  its fixed 99,997,497, scoped to the suites the run touches).
- **Shared:** `capture-cost-config --platforms <engines>` (phase key `cost-config`),
  which records each engine's live cost driver in `results/cost_config.json`, the
  file every `sql` step needs. It runs after `validate-loads`, when every engine has
  answered, and only when the file is missing: the summary lists it as `skipped`
  when the file is there. The repository ships a baseline file, which prices the
  compute it was captured on, so on your own infrastructure pass
  `--recapture-cost-config` (the capture merges into the file, keeping engines it
  does not touch).
- **Final (once):** `analyze` -> `report`.

What a publish touches: each engine's materialization file defines one task per
suite × scale (`<engine>-tpcds-sf1`, `<engine>-dsb-sf10`, `<engine>-clickbench`, …,
with the SF1000 fact tables split into their own tasks on most engines). A
`flow-publish` with no scope publishes every task in the file. With `--scale`,
`--suites` or `--materializations` it publishes only the matching tasks: flowctl
creates or updates the named specs and leaves every other task as it is, and
`--backfill N` (scoped only) bumps those tasks' backfill counters to re-read their
collections. `e2e` always publishes whole specs and needs no scoping, because a
running capture and materialization pick up each scale's data as it lands in S3;
whether a scale is loaded yet is verified at runtime by `validate-loads`, not by
pruning the spec. Use a scoped publish to re-backfill or change one scale or suite
without touching the rest (`docs/OPERATOR_GUIDE.md`, "Runtime-v2 publishing rules").

Three steps `e2e` leaves to you:

- **SF1000 capture.** `source-s3` carries no SF1000 bindings; SF1000 is captured by
  the 24 capture tasks of `flow-publish sf1000-captures`, which `e2e` does not run.
- **ClickHouse databases.** `flow-publish clickhouse` (the alias) creates every
  database the spec names before publishing; `e2e` publishes by file path, which
  skips that step, so run `uv run benchmark clickhouse-prepare-databases` first.
- **Pausing the loads.** `e2e` runs `sql` right after `validate-loads` without
  pausing the materializations. For a measured run, pass `--skip sql`, pause them
  with `flow-disable <engine> --scale N`, then run `sql` yourself.

ClickBench specifics. Including `clickbench` in `--blocks` makes e2e run
`download-clickbench` -> `derive-clickbench-typed` (which also persists the `hits`
collection schema to `estuary/collections/clickbench_hits.schema.json`), then
`hash-clickbench-ground-truth`. The committed schema is what keeps ClickBench
define-once: the capture loads that JSON, so it keeps the ClickBench binding even
after the local parquet is deleted to reclaim disk
(`datagen/clickbench.py:free_local_copy_after_stage`; nothing runs it automatically).
The renderer drops the ClickBench binding *only* when neither the committed JSON nor
a local parquet exists: i.e. before ClickBench has ever been derived; TPC-DS/DSB
schemas are DDL-derived and always definable.

ClickBench is a single ~100 GB `hits` table (single-scale; ~14 GiB typed parquet,
~100M rows, and the full Estuary reload runs long). It's recorded under the run's
scale tier (so SF1 exercises ClickBench on the SF1-sized warehouse) while its
ground-truth hash stays at SF100. It's always the **full** corpus: there's no
sampling (a sample doesn't represent the data and would fail both the row-count
check (99,997,497) and the ground-truth hashes, which cover all rows). Download,
derive, and TPC-DS/DSB generation are all skip-if-present, so a re-run only does
the work that is missing.

The run **stops at the first failed step** (loading must succeed before the
benchmark is meaningful). A step skipped because its output already exists
(`capture-cost-config`) is not a failure. Use `--keep-going` to continue past failures, or fix
the issue and resume with `--from <phase>`.

## Examples

```bash
# fast SF1 soundness pass on Snowflake (skips the ~100 GB ClickBench path)
uv run benchmark e2e --platforms snowflake --scale 1 --blocks tpcds,dsb

# SF1 on Snowflake INCLUDING ClickBench (downloads + derives the full ~100 GB
# hits table, loads ~100M rows: long-running; row counts validated)
uv run benchmark e2e --platforms snowflake --scale 1 --blocks tpcds,dsb,clickbench

# data already generated + staged: just (re)load + benchmark Snowflake
uv run benchmark e2e --platforms snowflake --scale 1 --from materialize

# already have hits.parquet — skip the ClickBench download
uv run benchmark e2e --platforms all --scale 1 --skip download-clickbench

# load + validate only (no benchmark/analysis yet)
uv run benchmark e2e --platforms all --scale 1 --skip cost-config,sql,analyze,report

# your own infrastructure: price your compute, not the committed baseline's
uv run benchmark e2e --platforms all --scale 1 --recapture-cost-config
```

## Notes

- SF1000 is large and costly, and it is the maximum scale.
- `e2e` runs only the warm `sql` pass. Translation and `validate-sf1`, the cold,
  concurrency and sustained passes, and `reconcile` are separate commands.
- Some engines report cost on a delay: run `benchmark reconcile --all-cells` after the
  engine's history settles (Snowflake 6–8h; Redshift within the 7-day window).

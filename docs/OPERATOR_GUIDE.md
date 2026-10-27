# Operator Guide: For the Developer/Engineer Overseeing Claude Code

> **This file is for you, the human.** Claude Code's instructions are in `.claude/CLAUDE.md`; how the harness works is in `docs/ARCHITECTURE.md`. This document tells you what you need to do, when you need to do it, and how to unblock Claude Code when it gets stuck.

## Your Role

Claude Code implements the entire benchmark autonomously. Your job is to:
1. **Provide credentials and cloud accounts** before each stage
2. **Approve expensive operations** when Claude Code asks (SF1000+ runs)
3. **Resolve infrastructure/auth issues** that Claude Code can't fix itself
4. **Review the final report** for accuracy and fairness

You do NOT need to write code, review PRs, or debug query translations. Claude Code handles all of that.

## Pre-Requisites Checklist (Before Starting Anything)

- [ ] **Snowflake**: Account created, ACCOUNTADMIN credentials available
- [ ] **Databricks**: Workspace created, PAT token generated, SQL Warehouse HTTP path noted
- [ ] **BigQuery**: GCP project created, BigQuery API enabled, service account key JSON downloaded
- [ ] **Redshift**: AWS account with Redshift Serverless, IAM role configured
- [ ] **Fabric**: Azure tenant with Fabric capacity, service principal with Contributor role, Data Warehouse created
- [ ] **ClickHouse Cloud**: Account created, service credentials available
- [ ] **MotherDuck**: Account created, API token generated
- [ ] **Estuary**: Account active, refresh token available
- [ ] **LLM API (optional)**: Anthropic key only if running `optimize --via sdk` (the default `--via claude-cli` uses Claude Code auth)
- [ ] **Local tools**: Python 3.11+, uv (not pip), Terraform (repo pins 1.15.6 via mise), ODBC Driver 18 for SQL Server, `flowctl`, and for the DSB skewed-data generator either Docker (macOS) or git + a C toolchain (Linux, where the harness builds `microsoft/dsb`'s generator natively)
- [ ] **`env/` files**: Run `uv run settings generate --dir env`, then for each section copy `env/<section>.env.example` -> `env/<section>.env` and fill it in (per-engine: `env/snowflake.env`, `env/aws.env`, …). `uv run settings show -d` lists every variable with its description

## Operator's Role in Orchestration

The benchmark is driven by a **unified CLI** (`uv run benchmark ...`) that orchestrates all execution. You control what runs by specifying:

- `--platforms` (comma list of engines, e.g., `snowflake,motherduck,bigquery`, or `all`)
- `--scale` (scale factor: 1, 10, 100, 1000)
- `--blocks` (`tpcds`, `dsb`, `clickbench`; the measured passes default to `tpcds,dsb`)

Finer scopes exist on single commands: `--queries` on `optimize`, `--compute-size` on `sql` and `concurrency`, `--levels` on `concurrency`, `--suites` on the load and Estuary commands. `sql` has no per-query filter: errors never count as completed, so re-running a pass runs only its failures and whatever it had not reached.

Every command that takes `--platforms` accepts `all`: the seven engines in canonical order (snowflake, databricks, bigquery, redshift, fabric, clickhouse, motherduck), run one after another. `reconcile` keeps the six with a reconciler: ClickHouse records its metrics inline and has none. A misspelled engine fails before anything runs, with a "did you mean" hint. Without `--platforms`, most measured passes run on `motherduck` only.

There is no single `--phase` flag: each stage runs through its own command(s) (see the per-stage sections below).

**Key principles:**

1. **`sql` is resumable.** If a warm pass crashes midway, re-running the same command picks up where it left off: it skips the (query, run number) pairs already recorded without an error for that platform, scale and run type (`--force` re-runs them). `cold`, `concurrency` and `sustained` have no resume: a re-run executes the whole pass again and appends new rows, and the latest row wins in the analysis.
2. **SF1000 is the maximum scale.**
3. **Budget decisions are always yours.** No command projects a run's cost or stops at a spending limit: each one runs what you ask for, at the compute size the engine is set to. Choose the scale, platforms and compute size before you launch it.
4. **Monitor progress with `uv run benchmark status`.** It prints one row per (platform, scale, run type) with the distinct queries and rows recorded. `uv run benchmark coverage-audit` lists what each engine's size ladders still lack.
5. **Per-platform debugging is rare.** One command covers every engine you list, running them one after another (platform, then block). You only run per-platform commands if debugging a specific issue.
6. **Each scale must be made "ready" before you benchmark it.** Switching scales changes two things, the data loaded and the compute provisioned. `uv run benchmark prepare-scale --scale N` checks both axes per platform, writes the per-engine compute sizing to `terraform/sizing.auto.tfvars.json` (you run `terraform apply`), and prints any data-load steps still needed. It also validates the **upstream sources** per suite (shared across engines, reported once): the staged **S3** parquet-footer counts vs the manifest (`--check-s3`), which together with the per-platform data axis (DB vs manifest) confirms **S3 == target-DB** row-for-row, and the **Estuary collections** present + non-empty + freshness (`--check-estuary`, needs flowctl auth). Note the **data** axis reports `? unknown` when `data/<suite>/expected_counts.json` has no entry for that scale, that's a missing manifest entry, not missing data; backfill it with `generate-expected-counts --data-dir data/<s> --output data/<s>/expected_counts.json` (local parquet; the defaults point at `data/tpcds`), or `--from-s3 --suite <s> --scales N --output data/<s>/expected_counts.json` if the data was generated elsewhere and only exists in S3.
7. **`capture-cost-config` is a hard prerequisite for benchmarking.** `benchmark sql`, `cold`, `concurrency` and `sustained` refuse to run without `results/cost_config.json` (each exits 2); run `uv run benchmark capture-cost-config --platforms all` after compute is sized so cost is priced off the actual live compute, not defaults. A working baseline of `results/cost_config.json` is version-controlled (gitignore-exempted) so fresh worktrees aren't blocked: still refresh it before any benchmark pass. Snowflake and Databricks report "no cost-config introspection implemented" by design: Snowflake is priced at its size's list rate × engine time when the pass ends (the `estimated` tier, published as Modeled), with `reconcile` recording its attributed credits as a cross-check, and Databricks' inline estimate is replaced from its billing tables by `reconcile`.
8. **`benchmark e2e` drives the load-and-benchmark path.** It chains generate -> DuckDB ground truth -> stage to S3 -> Estuary capture/materialize -> validate-loads -> `capture-cost-config` -> **warm** `sql` -> analyze -> report (see `docs/E2E_LOADING.md`). The `capture-cost-config` step runs only when `results/cost_config.json` is missing; the repository ships a baseline, so on your own infrastructure pass `--recapture-cost-config` to price your compute. It does **not** cover terraform provisioning, translation and `validate-sf1`, the `optimize`/physical-design pass, pausing the materializations (`flow-disable`) before its `sql` step, `reconcile`, or the cold, concurrency and sustained passes: run those separately.

### Common CLI Commands

| Goal | Command |
|------|---------|
| Build local DuckDB ground truth (TPC-DS + DSB at one scale; needs the generated data) | `uv run benchmark validate-queries --scale 1` |
| Build ClickBench ground truth (43 queries, keyed SF100) | `uv run benchmark hash-clickbench-ground-truth` |
| Check a scale is ready (data + compute) | `uv run benchmark prepare-scale --scale 100 --platforms all` |
| Capture cost drivers (required before `sql`, `cold`, `concurrency`, `sustained`) | `uv run benchmark capture-cost-config --platforms all` |
| Drive generate->load->warm-SQL->report in one run | `uv run benchmark e2e --scale 1 --platforms all` |
| Run SQL benchmark at SF1 | `uv run benchmark sql --scale 1 --platforms all` |
| Run SQL benchmark at SF10 on MotherDuck, ClickBench included | `uv run benchmark sql --scale 10 --platforms motherduck --blocks tpcds,dsb,clickbench` |
| Run the SF100 concurrency pass | `uv run benchmark concurrency --scale 100 --platforms all` |
| Hold a sustained load (N sessions for M minutes) | `uv run benchmark sustained --scale 100 --platforms fabric --sessions 50 --minutes 30` |
| Tell replicate passes of one cell apart | add `--tag cold-r1` (any label) to `concurrency` or `sustained`; it lands in each row's `metadata.tag` |
| Run the SF1000 cold pass | `uv run benchmark cold --scale 1000 --platforms all` |
| Show progress / coverage | `uv run benchmark status` |
| Shrink the results DB before committing it (verifies the copy, keeps `.pre-compact`) | `uv run python scripts/ops/compact_db.py` |
| List what each engine's ladders still lack (and the command to fill each) | `uv run benchmark coverage-audit` |
| Record a deliberate omission (narrow it with `--blocks`, `--compute-size`, `--run-type`) | `uv run benchmark skip-scale --scale 1000 --platforms redshift --compute-size "8 RPU" --run-type warm --reason "<why>"` |
| Generate report draft | `uv run benchmark report` |

## What Happens At Each Stage (And What You Need to Do)

> **Note on commands.** There is no `uv run benchmark --phase N` runner: each
> stage is a sequence of individual commands. The concrete commands per stage are
> below, in the order of "Pipeline stages" in `.claude/CLAUDE.md`; the full command
> reference is `docs/ARCHITECTURE.md` §3.

### Query Suite
**Your action:** `uv run benchmark validate-dialects` (the ≥80%/engine translation gate; no data needed). The DuckDB ground truth is hashed from the generated data, so it is built at the end of Data Generation below.
**Deliverables:** 244 SQL queries (TPC-DS 99 + DSB 102 + ClickBench 43) passing the dialect gate.

### Data Generation
**Your action:** `uv run benchmark generate-tpcds --scales N`, `generate-dsb --scales N`, and `download-clickbench` + `derive-clickbench-typed` to produce data locally. Then hash the ground truth in DuckDB, with no cloud spend: `uv run benchmark validate-queries --scale N` (TPC-DS + DSB against `data/<suite>/sfN`) and `uv run benchmark hash-clickbench-ground-truth` (ClickBench, keyed SF100). Then `stage-data` uploads the Parquet to S3 (Estuary picks it up). This is the order `e2e` runs them in. DSB needs a real skew generator: Docker on macOS, a native build of `microsoft/dsb` elsewhere; `generate-dsb` fails rather than fall back to uniform data. **SF100 and above: generate on the opt-in datagen EC2 box, not a laptop**, upload the repo tarball (`git archive --format=tar.gz -o /tmp/benchmark-repo.tar.gz HEAD && aws s3 cp /tmp/benchmark-repo.tar.gz s3://<staging-bucket>/bootstrap/benchmark-repo.tar.gz`), then set `datagen_enabled = true` in tfvars and `terraform apply` (module `terraform/modules/datagen`, default m8i.8xlarge + gp3 `/data` volume). User-data self-provisions everything, `/data` volume, toolchain, AWS CLI, uv, repo at `/data/repo`, env files, `uv sync`, so generation is immediately runnable; bootstrap log at `/var/log/datagen-bootstrap.log`. Drive it via SSM (`aws ssm start-session` / `send-command`). Laptop generation caps out on disk and the Docker VM's ~100 GB ceiling (a full VM makes dsdgen silently write 0-byte tables), and the S3 push rides your uplink; the box runs dsdgen natively, pushes in-region via its instance role, and tears down by flipping the flag back.
**Deliverables:** Data at SF1, SF10, SF100, SF1000 staged in S3; ground-truth hashes populated. Validation gate: row counts verified at each scale factor.

### Infrastructure Setup
**Your action:** Populate each `env/<section>.env`, run `terraform apply` yourself (the harness writes tfvars but never applies), then:

> **Azure Databricks is a two-stage apply**: `terraform apply -target='module.databricks_azure'` provisions the workspace first, then you set `databricks_host` from the `databricks_azure_workspace_url` output (leave `databricks_token` empty, the provider authenticates off `az login` and mints the harness PAT) and re-apply for the catalog/schemas/warehouse. Full runbook: `terraform/README.md` "Azure Databricks bring-up".

1. `uv run benchmark export-terraform-outputs`: capture `terraform output -json` so the harness can read the provisioned compute.
2. `uv run benchmark connectivity-check --platforms all`: `SELECT 1` through every connector, in parallel. An engine that has not answered within `--timeout` seconds (default 30) is reported as failed.
3. `uv run benchmark prepare-scale --scale <N>`: confirm data + compute readiness for the scale (writes sizing tfvars; re-`apply` if it changes them).
4. `uv run benchmark capture-cost-config --platforms all`: record each engine's cost driver (required before any `sql`/`cold`/`concurrency`/`sustained` run).

Resolve any Terraform permission errors. Common issues:
- Snowflake: ACCOUNTADMIN role needed for warehouse creation
- Databricks: Workspace admin needed for SQL Warehouse provisioning; on Azure, the terraform identity also needs role-assignment rights (Owner / User Access Administrator) for the Unity Catalog storage grant
- BigQuery: `roles/bigquery.admin` on service account
- Redshift: IAM role needs `redshift-serverless:*` permissions
- Fabric: Service principal needs Fabric Contributor role on the capacity
- Fabric: after creating the warehouse, turn its result-set cache off once (`ALTER DATABASE CURRENT SET RESULT_SET_CACHING OFF`, as a warehouse admin). New warehouses can have it on, Terraform cannot set it, and the connector refuses to connect while it is on.
- ClickHouse: Cloud console -> create service credentials

> **Teardown**: when the campaign is over, destroy the engines with the targeted `terraform destroy` in `terraform/README.md` "Teardown (keep the S3 staging bucket)". It excludes `module.shared`, so the 2.5 TB staging bucket and the Estuary reader IAM user survive; a plain `terraform destroy` would try to delete the bucket.

### Data Loading
**Your action:** `uv run benchmark flow-auth`, then `flow-publish` the capture (`source-s3` holds SF1–SF100 and ClickBench; SF1000 is captured by the 24 tasks in `sf1000-captures`) and each engine's materialization (one task per suite × scale; `--scale`/`--suites` publish only the matching tasks), or `e2e` to drive the whole load path, then `uv run benchmark validate-loads --platforms <engines> --scale N` to check row counts. Monitor Estuary pipelines via CLI output; `uv run benchmark flow-metrics --watch 60` shows pipeline-side progress (documents captured/materialized per task × collection, from Estuary's OpenMetrics API, works even for destinations this machine can't reach); for *historical* throughput (e.g. how fast a finished backfill ran), read the ops stats documents: `flowctl raw stats --task <name> --since 6h -o json`, per-transaction `ts` + per-collection `out.docsTotal`/`bytesTotal`; the Estuary dashboard shows per-task status. Note runtime-v2 materializations commit in **rounds with a configured acknowledge delay** (~9 min observed), destination counts holding steady between rounds is normal commit cadence, not a stall.
**Deliverables:** Data loaded at SF1–SF1000 on all 7 platforms. Validation gate: row count match across all platforms.

#### Loading one scale while measuring another (Databricks)

A 1 TB load takes a day or more, and a live materialization must not share the warehouse being measured. For Databricks, create a second serverless SQL warehouse for Estuary (X-Small, one cluster, 10-minute auto-stop), set `DATABRICKS_LOADER_SQL_HTTP_PATH` in `env/databricks.env` to its HTTP path, and republish the loading scales (`flow-publish databricks --scale N`; an endpoint change does not bump backfill). Pause the loaded scales' materializations with `flow-disable`, and the benchmark warehouse then runs only the benchmark. Delete the loader warehouse when the loads are done and unset the variable. Moving a binding to a new materialization (the SF1000 fact-table split) restarts its backfill, and the new task refuses a table that already exists: republish the shrunken original, drop the moved tables, then publish the new tasks. On **Fabric**, give every split task its own endpoint `schema` (e.g. `estuary_tpcds_store_sales_sf1000`, created first with `CREATE SCHEMA`) and put the suite schema on each binding (`resource: { schema: tpcds_sf1000, ... }`): the connector keeps its `flow_checkpoints_v1` table in the endpoint schema, and Fabric detects write conflicts per table, so split tasks sharing one schema abort each other's commits (the YAML is set up this way). Snowflake's split tasks share their schema without trouble: they ingest through Snowpipe Streaming.

#### Runtime-v2 publishing rules

The engine specs and the capture set `shards.flags.enable-runtime-v2: "true"`
in the YAML; the flag takes effect on a task when its spec is **published**,
so engines pick it up whenever their spec is next published. Two rules keep
publishes safe:

1. **Materializations go v2 via scoped publish only**: e.g.
   `uv run benchmark flow-publish snowflake --materializations snowflake-tpcds-sf1 --backfill <N+1>`
   (never a full-spec publish, which re-backfills every suite × scale). The
   backfill bump re-reads the existing journal under the new runtime and is
   verified with `validate-loads --wait`.
2. **The capture's v2 storage schema requires a data-flow reset** to take
   effect in the collections: a materialization backfill only re-reads
   journals *as the v1 capture wrote them*. Use
   `uv run benchmark flow-reset --suites <suite> --scale <N>` (scoped;
   `--dry-run` first) to make the v2 capture re-read S3 and rewrite the
   collections. **A reset auto-bumps the backfill counter on every engine's
   subscribing materialization**: all engines re-load that suite × scale, so
   plan it once per suite × scale, not per engine. Runtime-v1 materializations
   consume v2-written collections fine (validated: motherduck v1
   re-materialized a v2-reset collection with exact counts).

Whether to `flow-reset` the larger suites × scales (ClickBench's journal is
228 GiB) is your decision: a reset reloads that data on every live
engine.

### Query Translation
**Your action:** `uv run benchmark validate-sf1 --platforms <engine>` on each engine, against the committed `queries/<platform>/<block>/` files. Those files are what the benchmark runs: the deterministic sqlglot translation with a few hand-applied correctness patches, and, on every engine but Fabric, the ClickBench project's own ClickBench SQL. `uv run benchmark translate --platforms <engine>` (one engine, a comma list, or `all`) regenerates an engine's files from the canonical DuckDB SQL and overwrites every one of them, those patches and imported ClickBench files included, so after a re-translate review `git diff queries/` and keep them. A failure in `validate-sf1` gets a correctness patch (types, rounding, aggregate semantics), never tuning. The published results ran no `optimize` pass on any engine and no physical design (`design-physical`/`apply-physical-design`): all engines benchmark the translated SQL against the Estuary-loaded tables as-is (ARCHITECTURE §8). To use `optimize --platform <engine>` anyway (default `--via claude-cli` uses Claude Code auth; `--via sdk` needs `BENCHMARK_ANTHROPIC_API_KEY`): its rewrites are equivalence-gated locally, and `validate-sf1 --revert-on-regression` reverts any that fail on the real engine.
**Deliverables:** Per-platform SQL files (`queries/<platform>/<block>/`), version-controlled. No physical design applied. Validation gate: all queries execute correctly at SF1 on all 7 engines.

### SQL Benchmark
**Before each scale:** run `uv run benchmark prepare-scale --scale N` (apply any sizing tfvars it writes) and `uv run benchmark capture-cost-config --platforms all`. `sql`, `cold`, `concurrency` and `sustained` exit with code 2 if `cost_config.json` is missing.

**Pause the materializations first.** Once an engine's load has passed `validate-loads`, pause its Estuary materializations before any measured run on that warehouse: `uv run benchmark flow-disable <engine> --scale N` (add `--suites` to narrow it). A live materialization keeps running commit rounds against the warehouse being measured. Pausing keeps the loaded data; `flow-disable <engine> --scale N --enable` resumes the tasks for the next load.

**Redshift shortcut:** `uv run python scripts/resize/redshift_resize.py --scale N` drives the whole per-scale flow for redshift, resizes the workgroup to the run-plan RPU via the AWS API (retrying while another workgroup operation settles), syncs `redshift_baseline_rpu` into `sizing.auto.tfvars.json` so terraform stays consistent, then runs `capture-cost-config` and the `prepare-scale` readiness report. Add `--compute-only` to skip the slow data sweep.

**Redshift always benchmarks pinned** (`max_capacity == base_capacity`, enforced by both terraform and the resize script): Serverless vertical autoscaling toward a higher max would add scaling noise to every pass. Only non-benchmark work (bulk loads) may raise the ceiling, explicitly: `scripts/resize/redshift_resize.py --scale N --max-rpu 256`; the next resize or `terraform apply` re-pins.

**Redshift compute-size band sweep** (mirrors the Snowflake warehouse-band / MotherDuck tier sweeps; `--compute-size` on `concurrency` stays Snowflake-only because Redshift has a single workgroup). The sweep runs **across all scales**: the grid is scale (1/10/100/1000) × RPU band (8/16/32/64/128) × level (1,5,10,20,50,100). Per (scale S, band B) cell:

```bash
uv run python scripts/resize/redshift_resize.py --scale S --rpu B --compute-only
uv run benchmark concurrency --scale S --platforms redshift
# same day: SYS_SERVERLESS_USAGE keeps 7 days
uv run benchmark reconcile --platforms redshift --scale S --run-type concurrency
```

Rows are stamped with the "`B RPU`" compute label and each resize re-captures a per-size price entry in `cost_config.json`; the results key `(platform, query_id, scale_factor, run_type, run_number, concurrency)` × `compute_label` keeps every grid cell distinct (a concurrency pass has no resume: re-running a cell runs it again). Order the grid cheap->expensive (all bands at SF1, then SF10, SF100, SF1000).

`scripts/sweeps/redshift_sweep.py` orchestrates the whole grid (`--scales`/`--bands`/`--levels` to scope) with a **timeout ejection policy**: each level's rows are checked after the run, and a level with more than `--max-timeouts` timed-out queries (default 0) ejects the cell, higher levels are skipped and the sweep moves up a band (more compute may clear it) or, when the top band ejects, on to the next scale. Ejected cells' completed levels remain valid data; timeouts burn the full 10-minute per-query timeout in RPU-hours, which is what the ejection bounds. After the last cell, back to the run-plan size: `uv run python scripts/resize/redshift_resize.py --scale <S> --compute-only`. Before its first cell the sweep checks that `SYS_QUERY_HISTORY` is exported to S3 Tables (Redshift's system-table integration, so queue and compile time outlive the 7-day window) and that the AWS CLI is 2.35.22 or later, the first release that can read or set it; it exits 2 otherwise. Enable it with `aws redshift-serverless update-namespace --namespace-name <namespace> --log-destination-type s3table --s3-table-action Enable --s3-table-names sys_query_history --s3-table-granularity namespace`; Terraform does not manage it, so a re-created namespace needs it again.

**Your action:** Run `uv run benchmark sql --scale 1 --platforms all` through `--scale 1000 --platforms all` (add `--blocks tpcds,dsb,clickbench` to include ClickBench; the default is `tpcds,dsb`). SF1000 is the maximum scale.

**Optional: sweep a scale across compute sizes (cost/perf frontier):** to see how one
scale performs on different compute, run that scale at each size with `--force`. Each run
auto-labels its rows with the compute size (`compute_label`), so the sizes don't collide;
the report's **Compute Layout & Efficiency** section shows one row per size plus a
sizing-frontier chart. Other report sections stay latest-wins (most recent run).
- **Snowflake:** `--compute-size xsmall|small|medium|large|xlarge` overrides the warehouse
  per run (all sizes are pre-provisioned), e.g.
  `for s in xsmall small medium; do uv run benchmark sql --scale 10 --platforms snowflake --force --compute-size $s; done`.
- **BigQuery:** `--compute-size <slots>` (a multiple of 50) resizes the Enterprise
  reservation before the run and waits until queries route to it; `bq-reservation
  --slots N` / `--teardown` bracket the sweep.
- **Other engines** resize out-of-band, then `--force`: `scripts/resize/<engine>_resize.py`
  for MotherDuck (Duckling size, REST API), Redshift (RPU), Databricks (warehouse size),
  Fabric (capacity SKU) and ClickHouse (replica memory), each pinned at one size.

*MotherDuck note:* its Duckling size (pulse | standard | jumbo | mega | giga) is
account-level, **not** Terraform-managed and not per-connection. Change it in the
MotherDuck UI (Settings -> Ducklings) or the REST API (`PUT
https://api.motherduck.com/v1/users/{user}/instances`, Admin token, body wrapped in
`{"config": …}`; Pulse omits `cooldown_seconds`).
`scripts/resize/motherduck_resize.py --size <tier>` does the PUT and waits until the
new size serves; it needs `MOTHERDUCK_ACCOUNT_USER` (the token owner's username) in
`env/motherduck.env`.

*Cost across sizes (no hand-editing):* after switching any engine's size, just re-run
`uv run benchmark capture-cost-config --platforms <engine>`: it **merges** and records
a per-size entry (keyed by `compute_label`) alongside the others, and runs auto-price by
the size they ran on. So a Standard->Pulse sweep is: capture (Standard) -> switch to Pulse
-> capture (Pulse) -> `sql --force`; both sizes stay in `cost_config.json` and price
correctly (Pulse $0.60/hr vs Standard $2.40/hr in the model).

**Execution plan:**
- SF1 -> SF10 -> SF100 -> SF1000 on all 7 platforms (`benchmark sql`): 1 warmup + 3 measured **warm** runs per query (warmup not recorded).
- Cold pass (`benchmark cold`, default SF1000): a single per-query run with the engine cache flushed first.
- Concurrency pass (`benchmark concurrency`, default SF100; `--scale` for the others): sweeps levels 1, 5, 10, 20, 50, 100 over `tpcds,dsb`.

### Reconcile and Analysis
**Your action:** First run `uv run benchmark reconcile --all-cells --platforms all` to back-fill authoritative server-time and reconciled cost (Databricks/Redshift/Fabric) once their billing/history has settled. Without `--all-cells` it reconciles one cell, `--scale` (default 1000) × `--run-type` (default `warm`). `--platforms all` is every engine with a reconciler, so it leaves out ClickHouse (its metrics are recorded inline); the default `--platforms` also leaves out BigQuery, whose reconcile back-fills queue time only. For Snowflake it records each query's attributed credits beside its engine-time cost as a cross-check (they lag 6–8 h). This also back-fills **MotherDuck server-time** (`server_time_ms` is NULL right after a MotherDuck `sql` run, it's captured in one post-run batch from the durable `MD_QUERY_HISTORY()`, not per query; its cost is re-priced on that engine time). Because `MD_QUERY_HISTORY()` retains 1y+, MotherDuck reconcile is reliable long after the run (unlike the ~1-2 min `recent_queries` window). It also back-fills **queue time** (`queue_ms`) for Databricks, BigQuery and Redshift, whose server time is execution only; the concurrency analysis needs it and withholds a cell's latency until it lands. Databricks keeps a year of history and BigQuery 180 days, but Redshift keeps **7 days**, so reconcile a Redshift concurrency pass within a week of running it (or enable Redshift's system-table export to S3 Tables for `sys_query_history`, which keeps it past that, from the day it is enabled). **Fabric** prices a burst as one capacity bill split by CPU share, so its reconcile back-fills each statement's CPU time from `queryinsights` (30 days) and re-prices the cell: resume the capacity for it (`scripts/resize/fabric_resize.py --resume`, F2 is enough; `--suspend` after) and never suspend straight after a measured pass, which loses the last ~5-15 minutes of that history. Rows older than the history re-price offline with `uv run benchmark recost-capacity --platforms fabric` (`--dry-run` first to see the before/after). Every engine is priced on the time its queries held the compute (engine time, and windows counted only while a query ran; ARCHITECTURE §7). The same command prices ClickHouse and MotherDuck warm rows on engine time and their shared-load rows by window (reconcile re-prices MotherDuck once its engine time lands), re-splits Databricks' billed hours by window time (capped at list rate × the time queries ran), caps Redshift's billed shares at RPU rate × the time queries held the workgroup, prices BigQuery reservation rows on the baseline held, and prices Snowflake like ClickHouse, at list rate × engine time with its attributed credits kept as a cross-check (the `sql`, `cold`, `concurrency`, `sustained` and `reconcile` commands already do it for what they touch). Then `uv run benchmark analyze` (cost curves, patterns, rankings, skew, cross-engine
row-count agreement: **watch its log for `row-count divergence` WARNINGs**: each one is
a correctness defect in an engine or in our translation of that query, and must be
investigated before the report ships, not summarized away).

Snowflake's attributed credits from `QUERY_ATTRIBUTION_HISTORY` lag 6–8 h and fill in
partially per run, so re-run `reconcile --platforms snowflake` later to back-fill the tail.

**Correctness above SF1 is a spot check, and it takes two commands.** The translation-stage
gate is `validate-sf1` (an alias for `validate-correctness --scale 1`). To check a
larger scale, hash ground truth there first: it is keyed `(query_id, scale_factor)`,
so scales coexist, then validate against it:

```bash
uv run benchmark validate-queries --scale 100          # local DuckDB, no cloud spend
uv run benchmark validate-correctness --scale 100 --platforms <engine> --blocks tpcds,dsb
```

ClickBench is excluded from the `--scale` treatment on purpose: it is one fixed
dataset whose ground truth is keyed at SF100 whatever tier it ran on.
`--revert-on-regression` is refused above SF1, because the optimize baselines were
equivalence-gated at SF1 and a divergence elsewhere says nothing about the rewrite.

**After any data change, run `uv run benchmark verify-figures`.** It recomputes every
hand-authored table in `report/sections/` from the results DB and exits 1 if a
published number no longer matches. New data moves published numbers; this is what
tells you *which* sentences and cells to revisit rather than leaving you to re-read
five sections. It also prints coverage findings: rows the data now has that a table
does not show, which is the prompt to extend a table after a campaign lands.
**Deliverables:** Reconciled cost/server-time on all recorded runs; analysis Parquet exports (cost curves, patterns, rankings, skew).

### Report
**Your action:**
1. Run `uv run benchmark report` to generate the draft
2. Review the draft report for accuracy and fairness
3. Resolve any factual corrections or platform characterizations
4. Approve the final report for publication (vendors do not review the results before publication)

Check for:
- Factual accuracy of platform characterizations
- No leaked credentials in any file
- Fairness: no platform unfairly advantaged or disadvantaged
- Limitations section is honest and complete
- Query translation notes are clear (known issues, sqlglot errors, platform-specific idioms)

## Troubleshooting

| Symptom | What to do |
|---|---|
| Snowflake authentication fails | Check `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_BENCHMARK_USER` and `SNOWFLAKE_BENCHMARK_PRIVATE_KEY_FILE` in `env/snowflake.env` (`uv run settings show -s SNOWFLAKE`). The harness authenticates with a key pair: the private key file must match the public half set in terraform's `snowflake_benchmark_user_rsa_public_key`. Verify the account identifier format. |
| "cost-config file not found" / `sql`, `cold`, `concurrency` or `sustained` exits 2 | Run `uv run benchmark capture-cost-config --platforms all` first (cost pricing is a hard prerequisite for `sql`/`cold`/`concurrency`/`sustained`). |
| `prepare-scale` shows a platform off target on data or compute | Apply any sizing tfvars it writes (`terraform apply`), follow its manual steps for out-of-band engines, and load the missing data it lists. |
| prepare-scale data axis `? unknown: expected-counts manifest unavailable` | Missing manifest entry for that scale (not missing data). Backfill from local parquet: `uv run benchmark generate-expected-counts --data-dir data/<tpcds\|dsb> --output data/<tpcds\|dsb>/expected_counts.json`; or, when the data was generated elsewhere, from the staged S3 parquet footers: `--from-s3 --suite <tpcds\|dsb> --scales N --output data/<tpcds\|dsb>/expected_counts.json`. |
| flowctl auth keeps expiring / `JWT expired` / `ESTUARY_REFRESH_TOKEN is an EXPIRED access-token JWT` | `ESTUARY_REFRESH_TOKEN` holds a **short-lived (~1 h) access-token JWT**, not a refresh token: so auth breaks hourly. Estuary **refresh** tokens are opaque `{id,secret}` blobs that never expire: generate one at dashboard.estuary.dev -> Admin -> **CLI-API -> Generate Token** and set it as `ESTUARY_REFRESH_TOKEN` in `env/estuary.env`. (The harness detects the wrong token type and reports exactly this.) |
| BigQuery `connect failed: [Errno 2] No such file … benchmark-runner-sa.json` | The terraform-managed GCP SA key was never written to disk. Run `uv run benchmark export-terraform-outputs`, it materializes `BIGQUERY_GCP_SA_KEY` (and the Estuary BQ-writer key) from the `bigquery_runner_sa_key` / `bigquery_estuary_sa_key` terraform outputs, `0600`. |
| `terraform apply` fails with insufficient permissions | Terraform's error names the resource that failed; add the role or policy it needs (the common ones are listed under Infrastructure Setup). |
| An Estuary materialization looks stuck | First distinguish stall from round delay: `uv run benchmark flow-metrics --task <name> --watch 60` (docs/s > 0 means it's moving) and `flowctl logs --task <name> --since 15m` (look for round commits / "configured delay"). If genuinely stuck: Estuary dashboard -> the materialization -> connector errors. May need to restart pipeline. |
| Materialization published but destination stays at 0 rows | Check the task's operational logs: `flowctl logs --task <name> --since 15m`. A shard crash-loop means the task never commits. If the logs implicate a shard flag, remove it from the spec, then clean-slate the affected materializations: `flow-delete` them -> drop the destination tables -> republish, and raise it with Estuary support. |
| Materialization recreated but still 0 rows (no backfill) | A recreated mat resumes at its old `backfill` generation and tails the collection head instead of re-reading history. Force a re-backfill by bumping the counter, scoped so you don't re-touch other scales/ClickBench: `uv run benchmark flow-publish <spec> --scale N --suites tpcds,dsb --backfill 2`, then `validate-loads --scale N --wait <secs>`. Large scales commit in later rounds, allow several minutes and re-check. |
| Snowflake rows have no attributed credits yet | `QUERY_ATTRIBUTION_HISTORY` lags ~6–8 h. The cost does not wait on it (list rate × engine time, set when the pass ends); run `uv run benchmark reconcile --platforms snowflake` later to record the attributed credits as a cross-check. |
| reconcile error: `Schema 'SNOWFLAKE.ACCOUNT_USAGE' does not exist or not authorized` | `BENCHMARK_ROLE` is missing the `SNOWFLAKE.USAGE_VIEWER` database role (read access to `QUERY_ATTRIBUTION_HISTORY`). The Snowflake terraform module grants it (`snowflake_grant_database_role.usage_viewer`), re-run `terraform apply`. Manual fallback (as `ACCOUNTADMIN`): `GRANT DATABASE ROLE SNOWFLAKE.USAGE_VIEWER TO ROLE BENCHMARK_ROLE;` |
| Redshift reconcile records no cost | `SYS_SERVERLESS_USAGE` keeps 7 days and needs a superuser: run `reconcile --platforms redshift` for each pass within a week of running it. Queue and compile time outlive the window only if `sys_query_history` is exported to S3 Tables (see the band sweep above). |
| Fabric ODBC connection error | Regenerate service principal secret, or verify ODBC Driver 18 is installed (`odbcinst -j`). |
| A query timed out (ClickHouse joins at the larger scales, most often) | The per-query limit is 10 minutes (600 s, set in `sql_benchmark/runner.py`; no flag). The harness records the query as a timeout failure and, in a warm pass, records its remaining runs as errors without running them. Nothing to change: timeouts stay in the results and the report counts them. |
| `uv sync` fails | Run `uv sync` manually to troubleshoot dependency issues. Check Python 3.11+ is available. |
| `validate-sf1` fails at SF1 | It prints each failing query (up to 10 per platform and block) with its reason and exits 1. Fix the translation with a correctness patch in `queries/<platform>/` (types, rounding, aggregate semantics; never tuning) and re-run it. |

## Risk Register

| Risk | Impact | What you may need to do |
|------|--------|------------------------|
| Fabric Terraform breaks | Blocked provisioning | Fall back to Azure Portal / CLI to create resources manually |
| ClickHouse times out on joins | Missing results for join-heavy queries at the larger scales | Nothing: the timeouts are recorded as failures and reported |
| Snowflake attribution delayed | Attributed credits (the cost cross-check) missing for recent runs | Wait ~6–8 h, then run `benchmark reconcile --platforms snowflake` to back-fill |
| Redshift billing data expires | Lost cost data | Run `benchmark reconcile --platforms redshift` within 7 days of each pass |
| Query translation bugs | Incorrect results | `validate-sf1` fails the query; fix it with a correctness patch and re-run `validate-sf1` |

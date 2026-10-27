# CLAUDE.md: Claude Code Project Instructions

> **This file is for Claude Code only.** The human operator guide is at `docs/OPERATOR_GUIDE.md`.

## Project Overview

You are implementing the **2026 Cloud Warehouse Benchmark** for CorrDyn / Estuary. This benchmark evaluates **7 engine configurations** on SQL performance using **244 SQL queries** across three workload blocks. The central question is **how latency and cost scale as a function of the warehouse (engine + compute size) and the workload type**: warm, cold, and concurrency passes across scale factors SF1–SF1000, plus per-engine compute-size sweeps (Snowflake warehouse tiers, Databricks warehouse sizes, BigQuery slot reservations, Redshift RPU bands, Fabric capacities, MotherDuck Duckling tiers, ClickHouse replica configs). Every experiment measures **cost**, **latency**, and **quality** (correctness vs the DuckDB oracle).

### Engines (7)

| ID | Engine | Notes |
|----|--------|-------|
| `snowflake` | Snowflake | Gen2 Standard warehouses; per-scale warehouse tier from `run_plan.yaml`; tier sweeps via `--compute-size`. |
| `databricks` | Databricks SQL | Serverless SQL warehouse on an Azure workspace (Databricks' current default; all-inclusive DBU pricing keeps cost telemetry honest). Size sweeps via `scripts/resize/databricks_resize.py`, pinned to one cluster. |
| `bigquery` | Google BigQuery | Enterprise Edition reservation; slot-count sweeps via `bq-reservation` + `--compute-size`. On-demand denied at SF1000. |
| `redshift` | Amazon Redshift Serverless | RPU-based, always pinned (`max_capacity == base_capacity`); RPU band sweeps via `scripts/resize/redshift_resize.py` / `scripts/sweeps/redshift_sweep.py`. |
| `fabric` | Microsoft Fabric | Runs on the **Data Warehouse** component (columnar/lakehouse over OneLake); T-SQL via `pyodbc` + ODBC Driver 18. `platform = 'fabric'` with a `component` sub-field (`dw`) in results metadata. Capacity SKU sweeps via `scripts/resize/fabric_resize.py`. Max scale: SF1000 (4 TB OneLake capacity cap). |
| `clickhouse` | ClickHouse Cloud | Strong aggregations, weak joins. Replica-memory sweeps out-of-band via the Cloud API (`replicaScaling`, `scripts/resize/clickhouse_resize.py`), pinned per run. |
| `motherduck` | MotherDuck | DuckDB-based; Duckling-tier sweeps (account-level, via REST API, `scripts/resize/motherduck_resize.py`, not Terraform). |

### SQL Query Suite (244 queries, 3 blocks)

| Suite | `block` id | Count | Notes |
|-------|------------|-------|-------|
| TPC-DS | `tpcds` | 99 | Standard decision-support queries on uniform data |
| DSB | `dsb` | 102 | Same TPC-DS schema with skewed distributions: quantifies skew impact |
| ClickBench | `clickbench` | 43 | Web analytics on `hits` table (~100 GB single scale) |

All 244 queries are validated in DuckDB first and translated via sqlglot to 7 engine dialects; every engine runs that deterministic translation plus per-engine correctness patches. A Claude optimization pass (`benchmark optimize`, model set by `BENCHMARK_OPTIMIZE_MODEL`) exists but is deliberately unused: every published result runs the translated SQL.

### Scale Factors

| SF | Size | Usage |
|----|------|-------|
| SF1 | ~1 GB | Validation |
| SF10 | ~10 GB | Small scale |
| SF100 | ~100 GB | Medium scale |
| SF1000 | ~1 TB | Maximum scale |

SF1000 is the maximum scale.

ClickBench runs at ~100 GB single scale (not scaled per tier).

## Documentation

**The docs in `docs/` describe how the codebase works and are the authoritative
reference:**

- `docs/ARCHITECTURE.md`: how the harness works (CLI surface, module map,
  execution / cost / correctness models, results schema). **Ground truth.**
- `docs/OPERATOR_GUIDE.md`: running the benchmark as the human operator.
- `docs/E2E_LOADING.md`: the `benchmark e2e` driver (generate -> load -> warm `sql` -> report).

### Pipeline stages (run in order)

Each stage: a terse headline, then the commands it runs (with notes where they
help). The full per-command reference is `docs/ARCHITECTURE.md` §3. **Never jump
ahead: each stage's validation gate must pass before the next.**

1. **Build the query suite and ground truth.** (Ground-truth hashing reads the
   benchmark data, so it runs *after* stage 2 generates it: the `e2e` driver orders it
   that way. `validate-dialects` has no data dependency.)
   - `benchmark validate-dialects`: sqlglot translation gate (≥80%/engine; no data needed)
   - `benchmark validate-queries --scale N`: hashes the 201 canonical TPC-DS + DSB queries in DuckDB into `ground_truth_hashes` at that scale (builds ground truth despite the name; needs the stage-2 data under `data/<suite>/sfN`)
   - `benchmark hash-clickbench-ground-truth`: the 43 ClickBench queries' ground truth (keyed SF100; needs the typed parquet from stage 2)


2. **Generate and stage the data.**
   - `benchmark generate-tpcds`: TPC-DS via the DuckDB `tpcds` extension
   - `benchmark generate-dsb`: DSB skewed data (Docker/native generator)
   - `benchmark download-clickbench` + `derive-clickbench-typed`: raw -> typed parquet
   - `benchmark generate-checksums` + `generate-expected-counts`: SHA-256 and row-count manifests (`generate-tpcds`/`generate-dsb` write their row counts too)
   - `benchmark stage-data`: upload Parquet to S3


3. **Provision the engines and verify readiness.**
   - `terraform apply`: operator runs it (the harness writes tfvars, never applies)
   - `benchmark export-terraform-outputs`: capture provisioned compute
   - `benchmark connectivity-check`: `SELECT 1` per engine
   - `benchmark prepare-scale`: data + compute readiness for a scale
   - `benchmark capture-cost-config`: cost driver per engine (required before `sql`, `cold`, `concurrency` and `sustained`, which exit 2 without it)


4. **Load the data into every engine.**
   - `benchmark flow-auth` + `flow-publish`: Estuary capture (`source-s3` for SF1–SF100 and ClickBench, `sf1000-captures` for SF1000) + per-engine materializations (one task per suite × scale; `--scale`/`--suites` publish a subset)
   - `benchmark validate-loads`: row counts vs expected
   - `benchmark flow-disable <engine> --scale N`: pause the materializations before any measured run (`--enable` resumes)
   - (or `benchmark e2e` to drive the whole load path, which also runs `capture-cost-config` when `results/cost_config.json` is missing)


5. **Translate, optimize, and validate the queries.**
   - `benchmark translate --platforms <engine>`: DuckDB SQL -> that engine's dialect (sqlglot + post-patches). It rewrites every file under `queries/<engine>/`, including the ClickBench project's imported SQL and the hand-applied correctness patches; review `git diff queries/` and keep those
   - `benchmark optimize`: per-engine Claude rewrite, equivalence-gated (`--via claude-cli` default / `sdk`); `validate-sf1 --revert-on-regression` reverts regressions
   - `benchmark design-physical` + `apply-physical-design`: clustering/distribution/sort/stats
   - `benchmark validate-sf1`: correctness vs ground truth (the real check)


6. **Run the SQL benchmark.**
   - `benchmark sql`: warm runs (1 warmup + 3 measured)
   - `benchmark cold`: cold pass (cache flushed; not part of the published results)
   - `benchmark concurrency`: levels 1/5/10/20/50/100
   - `benchmark sustained`: N sessions held busy for M minutes (run type `sustained`)
   - `benchmark skip-scale`: mark a scale × platform intentionally skipped


7. **Reconcile and analyze.**
   - `benchmark reconcile --all-cells`: back-fill authoritative cost/server-time
   - `benchmark analyze` writes `results/exports/*.parquet`: cost curves,
     patterns (warm) and pattern-load (under concurrency), the concurrency grid,
     skew (warm and under load), ClickBench, rankings, row-count agreement,
     MotherDuck cost, and the explorer's size ladder, rate card, query catalog
     and recorded skips


8. **Generate the report and the explorer.**
   - `benchmark verify-figures`: recompute every hand-authored report table
     from the database; exits 1 on drift
   - `benchmark report`: markdown + charts + HTML into `report/`. Only
     `report/sections/*.md` (the authored prose) is tracked; the rendered output
     is rebuilt from the database. See `report/README.md`.
   - `viz/`: a static client-side explorer over the same Parquet exports
     (Mosaic + DuckDB-WASM, `npm run build`). See `viz/README.md`.
     After new results land, rebuild it and run `npx playwright test` in
     `viz/`: a failing "stale caveat" test names a hand-written chart caveat
     (`viz/src/site/ladder-caveats.js`) the new data has made wrong; remove
     or reword it.


### Shipped artifacts (Git LFS)

`results/benchmark.duckdb`, `results/exports/*.parquet` and the DuckDB-WASM
module ship with the repo via Git LFS, so the analysis and the report are
reproducible from a clone. A clone without `git lfs install` gets ~130-byte
pointer stubs instead; tests that read the database skip with a message saying
so rather than failing obscurely.


## Golden Rules

1. **Never skip a validation gate.** Each stage ends with explicit pass/fail checks. Stop and ask the operator for help if a gate fails after 3 attempts.
2. **Never hardcode credentials.** All secrets go in per-section `env/<section>.env` files (git-ignored). Access them via the typed `BenchmarkSettings` tree in `src/benchmark/settings.py` (`get_settings().MOTHERDUCK.TOKEN.get_secret_value()`); never call `os.environ.get(...)` or `load_dotenv()` in harness code. See "Configuration & Settings" below.
3. **DuckDB is the source of truth for query correctness.** Write all queries in DuckDB SQL first. Translate via sqlglot to each engine dialect; translation issues get per-engine correctness patches (types, rounding, aggregate semantics), never tuning. Every engine executes that translated SQL: the per-engine Claude optimization pass (`benchmark optimize`, equivalence-gated, reverted by `validate-sf1 --revert-on-regression`) exists but is deliberately unused, as is physical design (the `design-physical` / `apply-physical-design` tooling); all engines benchmark against the Estuary-loaded tables as-is. The translated SQL files live in `queries/<platform>/` and are version-controlled and what the benchmark executes.
4. **Record everything.** Every query execution, every failure, every anomaly goes into `results/benchmark.duckdb`. Never silently discard errors.
5. **Continue on failure.** A single query failure should never abort an entire benchmark run. Log the error, record it in the results DB, and move to the next query.
6. **Commit after each stage.** Create a git commit with a descriptive message at the end of each completed stage.
7. **Regenerate the report draft after every stage that produces data.** From stage 6 (the SQL benchmark) onward, every stage that writes results to the DB must also regenerate the report draft and charts. The report is always a working snapshot.
8. **Server-side time is the primary metric.** Capture both wall-clock time from Python harness AND server-side execution time from each engine's query history/profile. Report server-side time. Network latency is documented but excluded from performance comparison.
9. **Disable result caching on all engines where possible.** Document which engines allow cache bypass and which do not.
10. **Keep docs in sync with code; no change ships alone.** Any change to behavior, the CLI surface, commands/flags, the results schema, config, or cost/correctness logic MUST be accompanied in the same change by updates to the relevant docs (`docs/ARCHITECTURE.md`, `docs/OPERATOR_GUIDE.md`), code comments, and docstrings. If a change makes a doc, comment, or docstring wrong, fix it in the same commit. When code and docs diverge, the code is ground truth, but the fix is to update the docs, not to leave the drift; `docs/` is the living record and must not drift.

## When to Ask the Operator for Help

**STOP and ask the operator** in these situations:

- **Credential issues**: Any authentication failure after verifying env vars are set.
- **Terraform failures**: If `terraform apply` fails on resource creation (not just transient errors).
- **Estuary pipeline failures**: If materializations fail or data doesn't arrive.
- **Data mismatches**: If row counts don't match across engines after loading.
- **Validation gate failures**: After 3 retry attempts on any gate.

**DO NOT ask the operator** for:
- Code bugs you can fix yourself
- Missing Python packages (install them with `uv add <package>`)
- Query translation issues (fix manually and document)
- Transient network errors (retry with backoff)

## Dependency Management (uv)

This project uses **uv** exclusively for Python dependency management. Never use pip.

**Never edit the dependency lists in `pyproject.toml` by hand.** Always add, remove, or
upgrade dependencies with `uv` commands (`uv add <pkg>`, `uv add --group dev <pkg>`,
`uv remove <pkg>`) so `uv.lock` stays in sync: only edit `pyproject.toml` dependencies
directly if a `uv` error makes the command impossible, and then run `uv lock` afterward.

```bash
# Initial setup
uv sync                          # Install all deps from uv.lock
uv run benchmark --help          # Run the CLI

# Adding packages
uv add <package>                 # Add a dependency
uv add --group dev <package>     # Add a dev dependency

# Running commands
uv run benchmark sql ...         # All CLI commands go through uv run
uv run pytest                    # Tests
```

### pyproject.toml

Dependencies, the `benchmark`/`settings` console scripts, dev groups, and the ruff
lint/format configuration all live in the repo's `pyproject.toml`. **Read the current
`pyproject.toml` before adding dependencies or writing code, and conform to the
linting and formatting rules declared there**: don't reproduce or assume them from
memory.

## Code Standards

All harness code must follow these conventions. Claude Code should apply them when writing any Python in this project.

### Logging (No Print Statements)

**Never use `print()` in harness code** (enforced by ruff `T20`): all output goes
through the `logging` module. Logging is configured in `src/benchmark/logging_config.py`
(`configure_logging()` installs a Rich console handler + a JSON audit-trail file under
`results/logs/`); call it once at startup, and read that module rather than
reconstructing it.

Conventions when emitting logs:
- One logger per module: `logger = logging.getLogger("benchmark.<module>")`.
- Attach structured context with `extra={...}` (`platform`, `query_id`, `scale_factor`, `run_number`, …): the JSON formatter promotes those keys.
- Log errors with `exc_info=True`; use `%s` lazy formatting in log calls, not f-strings.

### Long-Running & Polling Tasks (Live Progress, Bounded Logs)

For long-running or polling work: backfill waits, multi-step loads, anything
that loops and re-emits status: **prefer a live `rich` progress display with a
bounded scrolling log window** over plain repeated log lines. Repeated per-poll
log lines grow the scrollback without bound and bury the signal; a live display
keeps the state legible.

The pattern (reference implementation: `validate-loads`'s `--wait` mode,
`cli.py:_validate_loads_live` driving `staging/load_check.py:validate_engine_loads`'s
`on_table` / `on_poll` callbacks):

- **Pin progress, scroll a fixed window of logs.** Use `rich.live.Live` rendering
  a `Group(Panel(recent_logs), Progress(...))`. Keep the most-recent N log lines
  in a `collections.deque(maxlen=N)` (a ring buffer) rendered in a fixed-height
  panel above the bars, so on-screen log volume is bounded no matter how long the
  task runs. Group the bars at a sensible granularity (here, one per platform,
  ≤7 bars, not one per suite or per table) and push the finer dimensions into the
  bar's label (the current dataset + a m/M counter) and into the scrolling log
  (per-table row counts), rather than spawning a bar per item. Keep the bar's
  columns fixed-width: a variable-width column steals space from the flexible
  `BarColumn`, so the bar visibly resizes ("bounces") every refresh.
- **Keep library code presentation-free.** The worker (e.g. `validate_engine_loads`)
  stays `rich`-free and exposes callbacks emitting **structured** records: a
  coarse `on_poll(records, ...)` per round and a fine `on_table(tick)` per item:
  while the CLI layer owns the display. Non-TTY / piped / CI runs fall back to
  plain log lines (gate on `sys.stderr.isatty()`).
- **Capture logs into the window without changing levels.** During the `Live`,
  detach the console `RichHandler` and attach a small handler that appends
  formatted records to the ring buffer; restore on exit. **Never raise/lower
  logging level thresholds to control on-screen volume**: bound it with the ring
  buffer instead. The JSON file handler stays attached so the full audit trail is
  always written.

### Linting & Formatting

Ruff lint + format rules are declared in `pyproject.toml` (`[tool.ruff]`): read them
there and conform; don't assume the rule set from memory. The one to keep in mind:
`print()` is banned (the `T20` rule): all output goes through the `logging` module.

You don't need to run the checks manually. `prek` (the Git pre-commit hooks) runs ruff
check + format on commit and rejects non-conforming code, so just write code that
matches the configured rules.

### Type Hints

All function signatures must include type annotations. Use modern Python 3.11+ syntax:

```python
# Good
def execute_query(
    self,
    connector: WarehouseConnector,
    query: BenchmarkQuery,
    run_number: int,
    timeout_ms: int = 600_000,
) -> QueryResult:
    ...

# Bad — no type hints
def execute_query(self, connector, query, run_number, timeout_ms=600000):
    ...
```

### Data Models & Interfaces

Pick the construct that matches the intent:

- **Data containers**: configuration, query metadata, result/score records, anything
  that is validated or serialized: use Pydantic `BaseModel`. You get validation,
  defaults, and JSON round-tripping for free. Prefer it for any new data-carrying type.
- **Behavior interfaces**: a contract that several implementations must satisfy
  (connectors, cost collectors, scorers): use `abc.ABC` with `@abstractmethod`, or a
  `typing.Protocol` for structural/duck-typed contracts. The codebase already does this
  well, e.g. `WarehouseConnector(ABC)` and the cost-collector base.

Reality check: this is **not** uniformly applied. Some data containers are plain
`@dataclass` (e.g. the connector-level `QueryResult` in `connectors/base.py`) rather
than `BaseModel`. Treat those as tech debt to migrate toward `BaseModel` when you touch
them, not a pattern to copy. When in doubt: behavior -> ABC/Protocol; data -> BaseModel;
reach for a bare `@dataclass` only for a trivial internal value with no validation or
serialization needs.

### Exception Hierarchy

Define a project-specific exception hierarchy. Never catch bare `Exception` without re-raising or logging:

```python
# src/benchmark/exceptions.py

class BenchmarkError(Exception):
    """Base exception for all benchmark errors."""

class ConnectorError(BenchmarkError):
    """Failed to connect to or communicate with an engine."""

class QueryTimeoutError(BenchmarkError):
    """Query exceeded the configured timeout."""

class QueryExecutionError(BenchmarkError):
    """Query failed during execution (syntax error, OOM, etc.)."""

class ValidationError(BenchmarkError):
    """Result validation failed (row count mismatch, hash mismatch)."""

class NotSupportedError(BenchmarkError):
    """Engine does not support the requested capability."""

# The file also defines DataGenerationError and UnlabelledComputeError.
```

### Context Managers for Database Connections

Always use context managers for connectors. Never leave connections open. The
contract is `WarehouseConnector` in `src/benchmark/connectors/base.py`:

```python
class WarehouseConnector(ABC):
    """Abstract base for all engine connectors."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    def __enter__(self) -> WarehouseConnector:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()

# Usage
with create_connector("snowflake", config={"schema": "tpcds_sf1"}) as conn:
    result = conn.execute_query(sql)
```

### Additional Conventions

- **Docstrings**: Google-style docstrings on all public classes and functions.
- **Constants**: Use `UPPER_SNAKE_CASE` for module-level constants, never magic numbers inline.
- **Imports**: Sorted by ruff (`I` rule). Standard library -> third-party -> first-party.
- **f-strings**: Prefer f-strings for string formatting. Use `%s` formatting only in `logger` calls (lazy evaluation).
- **Path handling**: Use `pathlib.Path`, not `os.path`.
- **No mutable default arguments**: Use `None` with an inner default instead.

## Unified CLI & Orchestration

The benchmark is driven by a single CLI (`uv run benchmark`) that orchestrates work **across all platforms at each scale factor**. The operator directs the process via CLI flags; the harness ensures reproducibility, atomicity, and that no result is ever lost.

### Design Principles

1. **Scale-first, platform-second.** Each command runs a complete benchmark pass across the platforms named in `--platforms` at a given scale, one platform after another. Every command that takes `--platforms` accepts a comma list or `all`, which expands to the seven engines in canonical order (snowflake, databricks, bigquery, redshift, fabric, clickhouse, motherduck; `reconcile` keeps the six with a reconciler, leaving out ClickHouse). A misspelled engine fails before anything runs, with a "did you mean" hint (`cli._resolve_platforms`). Most measured passes default to `motherduck`.
2. **Resumable by default.** The results DB tracks what's completed. Re-running `sql` skips the (query, run number) pairs already recorded without an error for that platform, scale and run type, unless `--force` is passed, so a crash mid-run loses only the in-flight query. `cold`, `concurrency` and `sustained` have no resume: a re-run executes the whole pass again and appends new rows.
3. **Atomic recording.** Every individual query result is written to `benchmark.duckdb` immediately after execution. There is no batch commit at the end of a run.
4. **Operator control.** The operator can scope runs with `--platforms`, `--blocks` and `--scale`; per-command flags add finer control where it exists (`--queries` on `optimize`, `--compute-size` on `sql`, levels on `concurrency`, see `docs/ARCHITECTURE.md` §3). `sql` has no per-query filter: to re-run failures, rely on resume (errors never count as completed), and `--force` to re-run everything. The harness makes no budget decisions: no command projects a run's cost or caps its spend, so the scale, platforms and compute size of every pass are the operator's call.
5. **Never lose data.** Results are append-only with `created_at` timestamps. Deduplication happens at query time (latest run wins), never by deleting rows. Re-runs create new rows, not overwrites.
6. **Deterministic ordering.** Within a scale factor, queries execute in a fixed order on every platform: blocks in `--blocks` order (default `tpcds,dsb`; add `clickbench` for all 244), query numbers ascending within a block. This makes cross-platform comparison easier and progress tracking predictable.

### CLI Commands

The commands grouped by pipeline stage are listed under "Pipeline stages" above. The
**full per-command reference**: every command, its flags, and what each does
(including the ones whose names mislead): is the table in `docs/ARCHITECTURE.md` §3
("CLI command surface"). Don't maintain a second copy here.

### Orchestration Flow (`sql` example)

When the operator runs `uv run benchmark sql --scale 10 --platforms all`:

1. **Load config**: resolve `--platforms` (`all` is the seven engines in canonical order; an unknown name exits 2 with a "did you mean" hint), open `results/benchmark.duckdb` (creating or migrating the schema), require `results/cost_config.json` (else exit 2), and record one `benchmark_run` provenance row (git SHA, tool versions, executed-query hash) that every result row links to. Credentials resolve per engine through `get_settings()` when each connector is built.
2. **Apply `--compute-size`** if given: a Snowflake tier picks that warehouse; a BigQuery slot count resizes the reservation and waits until queries route to it; other engines are ignored with a warning (they resize out-of-band).
3. **Loop platform -> block**, in the order given: build the platform's cost collector from `cost_config.json` (a platform the file lacks falls back to collector defaults with a warning), then for each block in `--blocks` (default `tpcds,dsb`, 201 queries; `clickbench` adds 43) open a connector on that block's schema (`tpcds_sf10`, `dsb_sf10`, `clickbench`) and call `sql_benchmark/runner.run_sql_benchmark`. There is no data-loaded check; a missing table surfaces as query errors. A connect failure skips that platform/block.
4. **Label the compute**: the runner records the engine's live configuration in `engine_environment` and stamps its compute label on every row; when no label can be derived it raises `UnlabelledComputeError`, which stops the command rather than record unlabelled rows.
5. **Resume**: unless `--force`, skip the (query_id, run_number) pairs already recorded without an error for this platform, scale and run type.
6. **Execute**: per query, 1 warmup (not recorded) + 3 measured runs (`--warmups`, `--runs`), each measured run inserted into the DB as soon as it returns, under the 10-minute per-query timeout. A timeout marks the query's remaining runs as errors without running them; any failure is recorded with `error_message` and the run moves on.
7. **Cost**: each row carries an inline cost; when every platform is done, ClickHouse and Snowflake rows (MotherDuck's once `reconcile` lands its engine time) are re-priced at list rate × engine time and BigQuery reservation rows on the baseline held. Engines whose billing lands later (Snowflake attribution 6–8 h, Redshift within its 7-day window, Databricks, Fabric, MotherDuck engine time) are back-filled by `benchmark reconcile`.
8. **Output**: one completion line; there is no summary table (`benchmark status` prints coverage).
9. **Correctness is not judged here**: `validate-sf1` owns it (see "Results Schema" below).

### Orchestrator architecture

There is no separate orchestrator module. Orchestration lives in the Typer CLI (`src/benchmark/cli.py`): each command (`sql`,
`cold`, `concurrency`, …) loops over platforms × blocks itself and calls the
function-based modules in `src/benchmark/sql_benchmark/` (`runner.run_sql_benchmark`,
`cold`, `concurrency`). See the "SQL execution model" section of
`docs/ARCHITECTURE.md` for the real flow.

### Progress Tracking

The CLI's `status` command prints the coverage recorded so far, one row per
(platform, scale, run type), with distinct queries and total rows:

```
$ uv run benchmark status
platform | scale | run_type | queries | runs
---------+-------+----------+---------+-----
bigquery | 1 | concurrency | 201 | 8442
bigquery | 1 | warm | 244 | 4953
...
```

`benchmark coverage-audit` lists what each engine's size ladders still lack.

## Configuration & Settings

All env-driven configuration is declared in `src/benchmark/settings.py` as a typed
`pydantic-settings` tree. The root `BenchmarkSettings` composes per-engine and
shared sub-models (`AWSSettings`, `EstuarySettings`, `SnowflakeSettings`, …), each
with its own `env_prefix`, field-level defaults, and one-line `description=`.
Access via `get_settings()` (process-cached). The canonical list of fields lives
in `settings.py`; `uv run settings show -d` prints every field with its description.

**Rules:**

- **Never call `os.environ.get(...)` in harness code.** Read from
  `get_settings()`. Example: `get_settings().MOTHERDUCK.TOKEN.get_secret_value()`.
- **Never call `load_dotenv()`.** Each settings model reads its own
  `env/<section>.env` automatically (one file per section: `env/snowflake.env`,
  `env/aws.env`, …, `env/benchmark.env`; override the dir with `BENCHMARK_ENV_DIR`).
- **Adding a new env var:** add a field to the appropriate nested model in
  `settings.py` with a type, default, and `description=`. Field names are ALL
  CAPS to mirror the env var name (ruff's pep8-naming is disabled for
  `settings.py` only).
- **Secrets:** use `SecretStr` for credentials; unwrap with `.get_secret_value()`
  at the call site.
- **Path-typed credentials** (e.g. service-account keys) point at a file on disk.
  Expose a `@property` on the model that reads the file when needed, keeping the
  secret out of memory until then. Example:
  `EstuarySettings.BIGQUERY_SERVICE_ACCOUNT_KEY` (Path) -> `.BIGQUERY_SERVICE_ACCOUNT_JSON`.
- **Per-section `env/<section>.env.example`** templates are regenerated, never
  hand-edited. After any change to `settings.py`, run
  `uv run settings generate --dir env` (writes one `<section>.env.example` per
  section; the real `env/<section>.env` files are git-ignored). This per-section
  split is what keeps `extra="forbid"` typo-detection working on
  pydantic-settings >=2.2 (a single shared `.env` cannot).

**The `settings` console script** (installed by `uv sync` via `[project.scripts]`):

```bash
uv run settings show               # compact: ENV VAR / DEFAULT / CURRENT / SOURCE
uv run settings show -d            # add SECTION + DESCRIPTION columns
uv run settings show -s SNOWFLAKE  # restrict to one section
uv run settings show --show-secrets
uv run settings generate           # combined preview of all sections to stdout
uv run settings generate --dir env # one env/<section>.env.example per section
```

## Results Schema

All results live in `results/benchmark.duckdb`. **The authoritative schema is
`SCHEMA_SQL` in `src/benchmark/results.py`**: one block of `CREATE TABLE IF NOT EXISTS`
plus `ALTER TABLE … ADD COLUMN` migrations, applied once on connect. Read it there for
the current tables/columns; don't trust a copy. Tables: `sql_benchmark_results`,
`ground_truth_hashes`, `scale_skips`, `engine_environment`, `benchmark_run`.

The two tables below carry the core conceptual model: measure **cost, latency, and
quality** per unit of work.

```sql
-- The heart of the SQL benchmark: one row per measured query execution.
CREATE TABLE sql_benchmark_results (
    run_id          UUID DEFAULT uuid(),
    platform        VARCHAR NOT NULL,   -- engine id; 'fabric' + metadata.component
    query_id        VARCHAR NOT NULL,   -- e.g. 'tpcds_q01', 'dsb_q15', 'clickbench_q03'
    block           VARCHAR NOT NULL,   -- 'tpcds' | 'dsb' | 'clickbench'
    scale_factor    INTEGER NOT NULL,
    run_type        VARCHAR NOT NULL,   -- 'warm' | 'cold' | 'concurrency' | 'sustained'
    run_number      INTEGER NOT NULL,
    concurrency     INTEGER,            -- sessions; NULL for warm and cold rows
    wall_clock_ms   DOUBLE,             -- client-side
    server_time_ms  DOUBLE,             -- engine-reported (the primary metric)
    bytes_scanned   BIGINT,
    cost_usd        DOUBLE,
    cost_confidence VARCHAR,            -- 'direct' | 'proportional' | 'estimated'
    row_count       BIGINT,
    -- NOTE: no result_hash / is_correct. The runners measure latency/cost only;
    -- correctness is owned solely by validate-sf1. See docs/ARCHITECTURE.md §6.
    error_message   VARCHAR,
    platform_query_id VARCHAR,          -- engine-side id; reconcile join key
    metadata        JSON,
    created_at      TIMESTAMP DEFAULT now()
    -- plus, via ALTER: bytes_spilled, compile_ms, queue_ms, cache_hit_pct,
    -- provenance_id (-> benchmark_run), compute_label (the size it ran on)
);

-- Correctness oracle: canonical result per query, computed locally in DuckDB.
CREATE TABLE ground_truth_hashes (
    query_id        VARCHAR NOT NULL,
    block           VARCHAR NOT NULL,
    scale_factor    INTEGER NOT NULL,
    result_hash     VARCHAR,
    row_count       BIGINT,
    result_rows     VARCHAR,            -- JSON rows, for 1e-4 element-wise compare
    result_columns  VARCHAR,            -- JSON column names, to align columns by name
    execution_ms    DOUBLE,
    duckdb_version  VARCHAR,
    error_message   VARCHAR,
    created_at      TIMESTAMP DEFAULT now(),
    PRIMARY KEY (query_id, scale_factor)
);
```

Conventions the DDL alone won't tell you:

- **`platform`** holds the engine IDs from the table above. **Fabric** uses
  `platform = 'fabric'` with `component` (`'dw'`) in the `metadata` JSON to
  record which component ran the experiment.
- **Append-only.** Every execution inserts a new row stamped with `created_at`; re-runs
  never overwrite, so a crash loses only the in-flight query.
- **Latest-wins dedup for reporting.** Read the most recent row per logical key via
  `ROW_NUMBER() OVER (PARTITION BY <key> ORDER BY created_at DESC) = 1`: keyed on
  `(platform, query_id, scale_factor, run_type, run_number, concurrency, compute_label)`
  (`LOGICAL_KEY` in `report/views.py`, behind `results_latest`). The cross-engine
  comparison views first restrict each engine to its designated size per scale,
  then dedup without `compute_label`. The analysis/report modules already do this.
- **`cost_confidence`** is `direct` (per-query API: BigQuery on-demand),
  `proportional` (time-weighted billing: Databricks, Redshift, Fabric, BigQuery
  reservations), or `estimated`
  (compute-time × rate: ClickHouse, MotherDuck, Snowflake), see "Cost Telemetry Confidence Tiers" below.

## Per-Engine Compute & Scale Limits

Compute **per engine per scale** is declared in `src/benchmark/run_plan.yaml`: the
single source of truth for sizing. `prepare-scale` writes these targets into tfvars and
validates the live infra against them, recorded cost rates correspond to them, and the
Snowflake connector selects its per-scale warehouse from them. Shape:
`<engine>: { <knob>: { <scale>: <target> } }`, plus each engine's size ladder
(`sweep`, and `load_sweep` where the concurrency ladder differs) that the sizing
sweeps walk and `coverage-audit` checks. (Valid scale factors, 1/10/100/1000
live in `src/benchmark/scales.py`.)

A **missing `(engine, scale)` cell means "skip that engine at that scale"**: that's how
a per-engine ceiling would be expressed, not a hard-coded map. All 7 engines currently
carry cells for every scale (SF1–SF1000):

- **bigquery** carries an `on_demand` allow/deny policy alongside its slot sizing: on-demand
  is denied at SF1000 (a slot reservation is required so a huge ad-hoc scan can't run up a
  giant bill).
- **motherduck** needs the Giga tier at SF1000.

## Cost Telemetry Confidence Tiers

The published buyer's guide names these tiers **Measured**, **Allocated** and **Modeled**
(stored as `direct`, `proportional` and `estimated`).

| Tier | Engines | Method |
|------|---------|--------|
| **Direct** | BigQuery on-demand | Per-query cost from platform APIs |
| **Proportional** | Databricks, Redshift, Fabric, BigQuery reservations | Time-weighted allocation from billing (BigQuery: the baseline held) |
| **Estimated** | ClickHouse, MotherDuck, Snowflake | Engine time × published rates (Snowflake at Standard; attribution kept as a cross-check) |

### Per-Engine Cost Capture

| Engine | Source | Key Columns |
|--------|--------|-------------|
| Snowflake | Engine time × list rate; `QUERY_ATTRIBUTION_HISTORY` as a cross-check | credits_attributed (6–8h latency), kept as `metadata.attributed_credits` |
| Databricks | `system.billing.usage` + `system.query.history` | DBU usage per query |
| BigQuery | Each query job (inline); `INFORMATION_SCHEMA.JOBS_BY_PROJECT` for queue time | `total_bytes_billed`, `slot_millis` |
| Redshift | `SYS_SERVERLESS_USAGE` (superuser) + `SYS_QUERY_HISTORY` | RPU-seconds, execution/queue/compile time (7-day retention) |
| Fabric | `queryinsights.exec_requests_history` (30 days) × the SKU's list rate | CPU time; capacity windows split by CPU share |
| ClickHouse | Per-query response summary × service rate | `elapsed_ns` (engine time), `read_bytes` |
| MotherDuck | `MD_QUERY_HISTORY()` engine time × Duckling tier rate | Tier × engine time |

## Result Comparison Tolerance (SQL Correctness)

- **Numeric columns** (integers included): relative tolerance 1e-4 (0.01%;
  `RELATIVE_TOLERANCE` in `sql_benchmark/validator.py`). Exact match on strings,
  dates and booleans. Columns are aligned to the ground truth by name.
- **Row order**: rows compare as a multiset, with or without ORDER BY, so neither
  unordered results nor tied rows depend on engine order. A `LIMIT` that keeps
  different rows at its cutoff is a mismatch.
- **NULL ordering**: NULLs are normalized before comparison.
- Applied the same way on all 7 engines, with two exceptions that run but are
  not compared: the ClickBench project's own per-engine queries (every engine
  but Fabric) and the queries whose result is engine-arbitrary (`NONDETERMINISTIC_QUERY_IDS`:
  ties at a `LIMIT` cutoff, `RANK()` over floating-point sums).
  Documented in methodology.

## Maximum Scale

SF1000 is the maximum scale.

## Validation Gates

Stage numbers are the "Pipeline stages" above.

| Stage | Gate | Method |
|-------|------|--------|
| 1 | Query suite validated | All 244 queries run in DuckDB and their ground truth is stored (`validate-queries`, `hash-clickbench-ground-truth`) |
| 1 | Dialect gate | sqlglot translation success ≥80% per engine (`validate-dialects`) |
| 2 | Data generated | TPC-DS + DSB at SF1–SF1000; ClickBench hits downloaded |
| 3 | Infra provisioned | `terraform plan` shows no changes; all 7 engines reachable (`connectivity-check`) |
| 4 | Data loaded | Row counts match the manifest on all 7 engines per dataset (`validate-loads`) |
| 5 | Queries translated | sqlglot translation + correctness patches; correctness validated at SF1 on all 7 (`validate-sf1`) |
| 6 | SQL benchmark complete | Warm + concurrency results captured for all engines at all SFs (`status`, `coverage-audit`) |
| 8 | Report complete | `verify-figures` passes, markdown report generated, repo packaged, quick-start validated |

## Error Handling Philosophy

Every operation in the harness must:
1. **Log the attempt** before execution (engine, query, timestamp)
2. **Capture the full error** on failure (stack trace, engine error code, message)
3. **Continue execution**: never abort a full benchmark run for a single query failure
4. **Record the failure** in the results DB with `error_message` populated
5. **Retry transient errors** with exponential backoff: connects and reconnects retry at ~2s, 8s, 32s (jittered), max 3 retries (`connectors/connection.py`). A failed query is not retried within a pass; it is recorded, and the next `sql` invocation re-runs it
6. **Skip gracefully** for capability gaps (`NotSupportedError`)

## Global Query Timeout

- **Default**: 10 minutes per query (`DEFAULT_QUERY_TIMEOUT_S` in `sql_benchmark/runner.py`), enforced by the harness on every pass; native per-engine statement timeouts are not set.
- **Exception**: the runner gives queries listed in `SF1000_TIMEOUT_EXCEPTIONS` 30 minutes at SF1000. The list is empty, so every query runs under the 10-minute limit; add a query there only with its rationale documented.
- Any query still running at timeout is recorded as a timeout failure, not excluded; in a warm pass its remaining runs are recorded as errors without running.

"""Central results database (DuckDB) — schema init and accessors.

CLAUDE.md §Results Schema defines the canonical tables. This module:
  - creates them idempotently in `results/benchmark.duckdb` (or any path)
  - exposes a thin `ResultsDB` wrapper with the read/write methods the rest of
    the harness needs (`record_sql_result`, `record_ground_truth`, etc.)
  - holds the deduplication-by-`created_at` SQL pattern in one place
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

logger = logging.getLogger("benchmark.results")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sql_benchmark_results (
    run_id          UUID DEFAULT uuid(),
    platform        VARCHAR NOT NULL,
    query_id        VARCHAR NOT NULL,
    block           VARCHAR NOT NULL,
    scale_factor    INTEGER NOT NULL,
    run_type        VARCHAR NOT NULL,
    run_number      INTEGER NOT NULL,
    concurrency     INTEGER,
    wall_clock_ms   DOUBLE,
    server_time_ms  DOUBLE,
    bytes_scanned   BIGINT,
    cost_usd        DOUBLE,
    cost_confidence VARCHAR,
    row_count       BIGINT,
    error_message   VARCHAR,
    platform_query_id VARCHAR,
    metadata        JSON,
    created_at      TIMESTAMP DEFAULT now()
);

-- Reconciliation join key; ALTER covers DBs created before the column existed.
ALTER TABLE sql_benchmark_results ADD COLUMN IF NOT EXISTS platform_query_id VARCHAR;
-- The benchmark runners do not judge correctness — that is solely
-- validate-sf1's job (tolerant, column-aligned, ClickBench-aware comparison
-- against ground_truth_hashes). The runners measure latency/cost only, so there
-- are no per-run result_hash / is_correct columns: the DROPs remove them from a
-- DB that carries them and are no-ops on DBs created from the CREATE above.
ALTER TABLE sql_benchmark_results DROP COLUMN IF EXISTS result_hash;
ALTER TABLE sql_benchmark_results DROP COLUMN IF EXISTS is_correct;
-- Engine profiling captured from each query's history row (alongside server time):
--   bytes_spilled   -- bytes spilled to local+remote/disk (right-sizing signal)
--   compile_ms      -- planning/compilation time, separated from execution
--   queue_ms        -- time queued/waiting before execution (queue vs compute)
--   cache_hit_pct   -- % of scan served from cache (validates cache is disabled,
--                      golden rule #9); 0-100. bytes_scanned already exists above.
-- Inline engines (Snowflake, BigQuery) fill these via record_sql_result; the
-- reconciled engine (MotherDuck, from MD_QUERY_HISTORY()) back-fills them.
ALTER TABLE sql_benchmark_results ADD COLUMN IF NOT EXISTS bytes_spilled BIGINT;
ALTER TABLE sql_benchmark_results ADD COLUMN IF NOT EXISTS compile_ms DOUBLE;
ALTER TABLE sql_benchmark_results ADD COLUMN IF NOT EXISTS queue_ms DOUBLE;
ALTER TABLE sql_benchmark_results ADD COLUMN IF NOT EXISTS cache_hit_pct DOUBLE;
-- Run provenance: ties each result row to the exact run that produced it
-- (git SHA, harness/tool versions, executed-query hash, optimize model).
ALTER TABLE sql_benchmark_results ADD COLUMN IF NOT EXISTS provenance_id UUID;
-- Compute-size discriminator (e.g. 'X-Small (Gen2)', 'Standard', '... / 100 slots'),
-- auto-derived from the engine's actual compute at run time. Lets the SAME scale be
-- run at MULTIPLE compute sizes without the runs colliding, so the report can plot a
-- cost/performance-vs-compute frontier. NULL = the run predates this / size unknown
-- (treated as the default size). Not in the main latest-wins dedup key (so main
-- sections show the most recent run); the efficiency section pivots on it.
ALTER TABLE sql_benchmark_results ADD COLUMN IF NOT EXISTS compute_label VARCHAR;

CREATE TABLE IF NOT EXISTS ground_truth_hashes (
    query_id        VARCHAR NOT NULL,
    block           VARCHAR NOT NULL,
    scale_factor    INTEGER NOT NULL,
    result_hash     VARCHAR,
    row_count       BIGINT,
    execution_ms    DOUBLE,
    error_message   VARCHAR,
    duckdb_version  VARCHAR,
    created_at      TIMESTAMP DEFAULT now(),
    PRIMARY KEY (query_id, scale_factor)
);
-- Serialized ground-truth rows (JSON), so validate-sf1 can compare engine
-- output element-wise within the spec's 1e-4 tolerance instead of by hash
-- (hash equality can't express tolerance). Nullable for back-compat with
-- pre-existing rows that only stored a hash.
ALTER TABLE ground_truth_hashes ADD COLUMN IF NOT EXISTS result_rows VARCHAR;
-- Ground-truth column names (JSON array), so validate-sf1 can align an engine's
-- result columns to the reference by NAME before the positional compare. This
-- matters for SELECT * queries: engines materialize columns in different orders
-- (e.g. Estuary stores them alphabetically), so positional comparison would
-- spuriously fail even when every column matches. Nullable for back-compat.
ALTER TABLE ground_truth_hashes ADD COLUMN IF NOT EXISTS result_columns VARCHAR;

-- Deliberate omissions, so an absence in the coverage matrix can be told apart
-- from an oversight. Keyed on BLOCK as well as (scale, platform): a skip is often
-- per-suite rather than whole-scale (ClickHouse's run plan skips DSB at 1 TB while
-- TPC-DS runs there), and a scale-only key cannot express that, so those cells
-- would read as thin coverage instead of as out of scope. '*' means every block.
CREATE TABLE IF NOT EXISTS scale_skips (
    scale_factor    INTEGER NOT NULL,
    platform        VARCHAR NOT NULL,
    block           VARCHAR NOT NULL DEFAULT '*',
    -- Keyed on compute size too, because omissions have that granularity: Redshift
    -- runs 32/64/128 RPU at 1 TB and cannot finish at 8/16, and MotherDuck's small
    -- tiers likewise. Without this column the only way to record those would be to
    -- mark the whole scale skipped, which overstates the omission and makes the coverage
    -- audit choose between permanent noise and a false 'never ran'. '*' = every size.
    compute_label   VARCHAR NOT NULL DEFAULT '*',
    -- Keyed on run type too: a size can be out of scope single-user and measured
    -- under load. Redshift 8 and 16 RPU cannot finish the 1 TB warm suite, and both
    -- ran the 1 TB concurrency grid until it ejected them. '*' = every run type.
    run_type        VARCHAR NOT NULL DEFAULT '*',
    reason          VARCHAR NOT NULL,
    skipped_by      VARCHAR,
    skipped_at      TIMESTAMP DEFAULT now(),
    PRIMARY KEY (scale_factor, platform, block, compute_label, run_type)
);

CREATE TABLE IF NOT EXISTS engine_environment (
    -- Engine compute/runtime config captured once per (platform, scale_factor,
    -- run_type) so results -- notably the ClickBench engine-sizing curve, where
    -- one 100 GB dataset is run across each engine's per-scale compute -- record
    -- which warehouse/RPU/slot/capacity produced the numbers.
    platform        VARCHAR NOT NULL,
    scale_factor    INTEGER NOT NULL,
    run_type        VARCHAR NOT NULL,
    -- Keyed on compute size too. Without it a size SWEEP would overwrite one row per
    -- (platform, scale, run_type), so only the last size would survive and the drift
    -- verdict would compare that arbitrary survivor against the designated tier,
    -- making `compute_validated: false` a false alarm during any sweep and a true
    -- verdict proof of nothing. '-' for captures that predate the column.
    compute_label   VARCHAR NOT NULL DEFAULT '-',
    environment     JSON NOT NULL,
    captured_at     TIMESTAMP DEFAULT now(),
    PRIMARY KEY (platform, scale_factor, run_type, compute_label)
);

CREATE TABLE IF NOT EXISTS benchmark_run (
    -- Run provenance: one row per `benchmark sql/cold/concurrency/sustained` invocation.
    -- sql_benchmark_results.provenance_id references this, so every measured number
    -- is traceable to the exact code + queries + config that produced it.
    provenance_id   UUID DEFAULT uuid(),
    command         VARCHAR NOT NULL,   -- 'sql' | 'cold' | 'concurrency' | 'sustained'
    scale_factor    INTEGER,
    run_type        VARCHAR,
    platforms       VARCHAR,            -- comma-joined platform list requested
    blocks          VARCHAR,            -- comma-joined block list requested
    git_sha         VARCHAR,            -- HEAD commit of the harness repo
    git_dirty       BOOLEAN,            -- uncommitted changes present at run time
    harness_version VARCHAR,            -- installed `benchmark` package version
    optimize_model  VARCHAR,            -- observed optimization, "<platform>:<model>"
                                        -- comma-joined; NULL = no engine in this run
                                        -- ran the pass. NOT the configured setting.
    query_files_hash VARCHAR,           -- hash of the executed queries/<platform>/ files
    tool_versions   JSON,               -- {python, duckdb, sqlglot, ...}
    created_at      TIMESTAMP DEFAULT now(),
    PRIMARY KEY (provenance_id)
);
"""


#: Run type of a sustained-load pass (``sql_benchmark.concurrency.run_sustained_pass``):
#: N sessions held busy for a fixed time, against the bursts every other concurrency
#: row records. Its rows never enter a burst figure: the shared report views leave
#: this run type out and expose it as ``sustained_results``.
SUSTAINED_RUN_TYPE = "sustained"

#: ``metadata`` key that sets a row aside: its value is the reason. A set-aside row stays
#: in the table (append-only) but every shared report view leaves it out before
#: latest-wins, so the rows it would have superseded stay published
#: (``scripts/ops/set_aside_rows.py``; the shipped results set aside Redshift's 1 GB
#: warm cells re-measured as the single-user queue-time check).
SET_ASIDE_KEY = "set_aside"

#: Days of statement history an engine keeps for a ``metadata.cpu_ms`` back-fill
#: (Fabric's ``queryinsights`` holds 30). Older rows missing it can never get it, so
#: they stop being candidates instead of costing every reconcile a history scan.
CPU_HISTORY_DAYS = 30

#: Engines whose reconcile records billing attribution beside a rate-priced cost
#: rather than replacing it: Snowflake is priced at list rate x engine time like the
#: other engines, and ``QUERY_ATTRIBUTION_HISTORY``'s credits are kept on each row as
#: ``metadata.attributed_credits``, a cross-check. For these a row
#: needs reconcile until its credits are recorded, while the view keeps them
#: (:data:`ATTRIBUTION_HISTORY_DAYS`); an ``estimated`` or zero cost is not a gap.
ATTRIBUTION_ONLY_PLATFORMS = ("snowflake",)

#: Days ``QUERY_ATTRIBUTION_HISTORY`` keeps a query's credits.
ATTRIBUTION_HISTORY_DAYS = 365

_ATTRIBUTION_ONLY = ", ".join(f"'{p}'" for p in ATTRIBUTION_ONLY_PLATFORMS)

#: Rows ``reconcile`` still has work for; ``_RECONCILE_CANDIDATE`` takes
#: (platform, scale_factor, run_type, missing_queue, missing_cpu), ``_RECONCILE_NEEDS``
#: just (missing_queue, missing_cpu). One definition for the candidate list, the
#: lookback window that must cover it, and the cells ``reconcile --all-cells`` visits.
_RECONCILE_NEEDS = f"""
    platform_query_id IS NOT NULL
    AND (server_time_ms IS NULL
         OR (platform NOT IN ({_ATTRIBUTION_ONLY})
             AND (cost_confidence = 'estimated'
                  OR ((cost_usd IS NULL OR cost_usd = 0) AND error_message IS NULL)))
         OR (platform IN ({_ATTRIBUTION_ONLY}) AND error_message IS NULL
             AND json_extract(metadata, '$.attributed_credits') IS NULL
             AND created_at >= CAST(now() AS TIMESTAMP) - INTERVAL {ATTRIBUTION_HISTORY_DAYS} DAY)
         OR (? AND queue_ms IS NULL)
         OR (? AND json_extract(metadata, '$.cpu_ms') IS NULL
               AND created_at >= CAST(now() AS TIMESTAMP) - INTERVAL {CPU_HISTORY_DAYS} DAY))
"""
_RECONCILE_CANDIDATE = f"platform = ? AND scale_factor = ? AND run_type = ? AND {_RECONCILE_NEEDS}"


class ResultsDB:
    """Thin wrapper around the central DuckDB results database."""

    def __init__(self, path: Path | str = "results/benchmark.duckdb") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.connect() as con:
            con.execute(SCHEMA_SQL)
            self._migrate_scale_skips(con)
            self._migrate_engine_environment(con)
            self._migrate_scale_skips_compute(con)
            self._migrate_scale_skips_run_type(con)
        logger.info("results DB schema ready at %s", self.path)

    @staticmethod
    def _migrate_scale_skips(con: duckdb.DuckDBPyConnection) -> None:
        """Add ``block`` to a pre-existing ``scale_skips`` and re-key on it.

        ``ADD COLUMN`` cannot change a primary key, so widening the key needs a
        rebuild. Done here rather than in ``SCHEMA_SQL`` because that file is
        declarative and this is conditional: it must run once, on databases created
        before the column existed, and be a no-op afterwards.

        Existing rows become whole-scale skips (``block = '*'``), which is what they
        meant — the key had no way to say otherwise.

        Wrapped in a transaction so a failure leaves the original table intact. The
        table holds a handful of bookkeeping rows, so the rebuild is trivial; the
        transaction is about not corrupting the record if it goes wrong.
        """
        columns = {r[0] for r in con.execute("DESCRIBE scale_skips").fetchall()}
        if "block" in columns:
            return
        logger.info("migrating scale_skips to block granularity")
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("""
                CREATE TABLE scale_skips_new (
                    scale_factor    INTEGER NOT NULL,
                    platform        VARCHAR NOT NULL,
                    block           VARCHAR NOT NULL DEFAULT '*',
                    reason          VARCHAR NOT NULL,
                    skipped_by      VARCHAR,
                    skipped_at      TIMESTAMP DEFAULT now(),
                    PRIMARY KEY (scale_factor, platform, block)
                )
            """)
            con.execute(
                "INSERT INTO scale_skips_new"
                " (scale_factor, platform, block, reason, skipped_by, skipped_at)"
                " SELECT scale_factor, platform, '*', reason, skipped_by, skipped_at"
                " FROM scale_skips"
            )
            con.execute("DROP TABLE scale_skips")
            con.execute("ALTER TABLE scale_skips_new RENAME TO scale_skips")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            logger.error("scale_skips migration failed; table left unchanged", exc_info=True)
            raise

    @staticmethod
    def _migrate_scale_skips_compute(con: duckdb.DuckDBPyConnection) -> None:
        """Add ``compute_label`` to ``scale_skips`` and re-key.

        Third conditional rebuild of this shape in the codebase, for the same reason
        each time: widening a primary key is not an ADD COLUMN. Existing rows take
        '*' (every size), which is what they meant when size could not be expressed.
        """
        columns = {r[0] for r in con.execute("DESCRIBE scale_skips").fetchall()}
        if "compute_label" in columns:
            return
        logger.info("migrating scale_skips to be compute-size aware")
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("""
                CREATE TABLE scale_skips_new (
                    scale_factor    INTEGER NOT NULL,
                    platform        VARCHAR NOT NULL,
                    block           VARCHAR NOT NULL DEFAULT '*',
                    compute_label   VARCHAR NOT NULL DEFAULT '*',
                    reason          VARCHAR NOT NULL,
                    skipped_by      VARCHAR,
                    skipped_at      TIMESTAMP DEFAULT now(),
                    PRIMARY KEY (scale_factor, platform, block, compute_label)
                )
            """)
            con.execute(
                "INSERT INTO scale_skips_new"
                " (scale_factor, platform, block, compute_label, reason, skipped_by, skipped_at)"
                " SELECT scale_factor, platform, block, '*', reason, skipped_by, skipped_at"
                " FROM scale_skips"
            )
            con.execute("DROP TABLE scale_skips")
            con.execute("ALTER TABLE scale_skips_new RENAME TO scale_skips")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            logger.error("scale_skips compute migration failed; table unchanged", exc_info=True)
            raise

    @staticmethod
    def _migrate_scale_skips_run_type(con: duckdb.DuckDBPyConnection) -> None:
        """Add ``run_type`` to ``scale_skips`` and re-key.

        The same conditional rebuild as :meth:`_migrate_scale_skips_compute`: widening
        a primary key is not an ADD COLUMN. Existing rows take '*' (every run type),
        which is what they said when run type could not be expressed; a skip that
        holds for one run type only is narrowed by recording it again with
        ``skip-scale --run-type`` and unmarking the '*' row.
        """
        columns = {r[0] for r in con.execute("DESCRIBE scale_skips").fetchall()}
        if "run_type" in columns:
            return
        logger.info("migrating scale_skips to be run-type aware")
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("""
                CREATE TABLE scale_skips_new (
                    scale_factor    INTEGER NOT NULL,
                    platform        VARCHAR NOT NULL,
                    block           VARCHAR NOT NULL DEFAULT '*',
                    compute_label   VARCHAR NOT NULL DEFAULT '*',
                    run_type        VARCHAR NOT NULL DEFAULT '*',
                    reason          VARCHAR NOT NULL,
                    skipped_by      VARCHAR,
                    skipped_at      TIMESTAMP DEFAULT now(),
                    PRIMARY KEY (scale_factor, platform, block, compute_label, run_type)
                )
            """)
            con.execute(
                "INSERT INTO scale_skips_new (scale_factor, platform, block, compute_label,"
                " run_type, reason, skipped_by, skipped_at)"
                " SELECT scale_factor, platform, block, compute_label, '*', reason,"
                " skipped_by, skipped_at FROM scale_skips"
            )
            con.execute("DROP TABLE scale_skips")
            con.execute("ALTER TABLE scale_skips_new RENAME TO scale_skips")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            logger.error("scale_skips run-type migration failed; table unchanged", exc_info=True)
            raise

    @staticmethod
    def _migrate_engine_environment(con: duckdb.DuckDBPyConnection) -> None:
        """Add ``compute_label`` to a pre-existing ``engine_environment`` and re-key.

        Same conditional rebuild as :meth:`_migrate_scale_skips`, for the same reason:
        widening a primary key is not an ``ADD COLUMN``. Existing rows take ``'-'``,
        which is honest — they are one arbitrary survivor of whatever sizes ran, and
        the old key could not say which.
        """
        columns = {r[0] for r in con.execute("DESCRIBE engine_environment").fetchall()}
        if "compute_label" in columns:
            return
        logger.info("migrating engine_environment to be compute-size aware")
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("""
                CREATE TABLE engine_environment_new (
                    platform        VARCHAR NOT NULL,
                    scale_factor    INTEGER NOT NULL,
                    run_type        VARCHAR NOT NULL,
                    compute_label   VARCHAR NOT NULL DEFAULT '-',
                    environment     JSON NOT NULL,
                    captured_at     TIMESTAMP DEFAULT now(),
                    PRIMARY KEY (platform, scale_factor, run_type, compute_label)
                )
            """)
            con.execute(
                "INSERT INTO engine_environment_new"
                " (platform, scale_factor, run_type, compute_label, environment, captured_at)"
                " SELECT platform, scale_factor, run_type, '-', environment, captured_at"
                " FROM engine_environment"
            )
            con.execute("DROP TABLE engine_environment")
            con.execute("ALTER TABLE engine_environment_new RENAME TO engine_environment")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            logger.error("engine_environment migration failed; table left unchanged", exc_info=True)
            raise

    # DuckDB is single-writer per file: a concurrent process holding even a
    # read-only connection makes duckdb.connect raise IOException ("Could not
    # set lock"). The connect is per-record (atomic recording), so a transient
    # reader (an operator status query, the sweep orchestrator's watermark check)
    # must never cost a recorded result: a failed connect would abort a concurrency
    # level mid-run. Retry with backoff before giving up.
    # Budget sized for a CONCURRENT BENCHMARK SESSION writing to the same DB
    # (two harness processes interleave per-record connects; a concurrency pass
    # can starve a co-resident warm pass past a ~16s budget). Caps at ~2.5 min of patience before giving up.
    _LOCK_RETRIES = 12
    _LOCK_BACKOFF_S = (0.2, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 15.0, 15.0, 30.0, 30.0, 30.0)

    @contextmanager
    def connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        con: duckdb.DuckDBPyConnection | None = None
        for attempt in range(self._LOCK_RETRIES + 1):
            try:
                con = duckdb.connect(str(self.path))
                break
            except duckdb.IOException:
                if attempt >= self._LOCK_RETRIES:
                    raise
                wait = self._LOCK_BACKOFF_S[min(attempt, len(self._LOCK_BACKOFF_S) - 1)]
                logger.warning(
                    "results DB locked by another process; retrying in %.1fs (%d/%d)",
                    wait,
                    attempt + 1,
                    self._LOCK_RETRIES,
                )
                time.sleep(wait)
        assert con is not None  # loop either breaks with a connection or raises
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---- Ground truth ---------------------------------------------------

    def record_ground_truth(
        self,
        rows: list[
            tuple[
                str,
                str,
                int,
                str | None,
                int | None,
                float,
                str | None,
                str,
                str | None,
                str | None,
            ]
        ],
    ) -> None:
        """Upsert ground-truth rows (one per (query_id, scale_factor)).

        Each tuple is (query_id, block, scale_factor, result_hash, row_count,
        execution_ms, error_message, duckdb_version, result_rows, result_columns)
        — ``result_rows`` is the JSON-serialized result set and ``result_columns``
        the JSON-serialized column-name list, both used by the tolerance-aware,
        column-name-aligned comparison.
        """
        with self.connect() as con:
            con.executemany(
                """
                INSERT INTO ground_truth_hashes
                  (query_id, block, scale_factor, result_hash, row_count,
                   execution_ms, error_message, duckdb_version, result_rows, result_columns)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (query_id, scale_factor) DO UPDATE SET
                  result_hash     = excluded.result_hash,
                  row_count       = excluded.row_count,
                  execution_ms    = excluded.execution_ms,
                  error_message   = excluded.error_message,
                  duckdb_version  = excluded.duckdb_version,
                  result_rows     = excluded.result_rows,
                  result_columns  = excluded.result_columns,
                  created_at      = now()
                """,
                rows,
            )

    def get_ground_truth(self, query_id: str, scale_factor: int) -> dict | None:
        with self.connect() as con:
            row = con.execute(
                """
                SELECT result_hash, row_count, error_message, result_rows, result_columns
                FROM ground_truth_hashes
                WHERE query_id = ? AND scale_factor = ?
                """,
                [query_id, scale_factor],
            ).fetchone()
        if row is None:
            return None
        return {
            "result_hash": row[0],
            "row_count": row[1],
            "error_message": row[2],
            "result_rows": row[3],
            "result_columns": row[4],
        }

    # ---- SQL benchmark --------------------------------------------------

    # ---- Engine runtime environment ------------------------------------

    def record_engine_environment(
        self,
        platform: str,
        scale_factor: int,
        run_type: str,
        environment: dict,
        compute_label: str = "-",
    ) -> None:
        """Upsert the engine's compute/runtime config for one
        (platform, scale_factor, run_type, compute_label), captured once per pass.

        ``compute_label`` is part of the key so a size sweep keeps one row per size.
        Without it a sweep would collapse to whichever size ran last, silently
        destroying the evidence the drift check depends on.
        """
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO engine_environment
                  (platform, scale_factor, run_type, compute_label, environment)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (platform, scale_factor, run_type, compute_label) DO UPDATE SET
                  environment = excluded.environment,
                  captured_at = now()
                """,
                [platform, scale_factor, run_type, compute_label or "-", json.dumps(environment)],
            )

    def get_engine_environment(
        self, platform: str, scale_factor: int, run_type: str
    ) -> dict | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT environment FROM engine_environment "
                "WHERE platform = ? AND scale_factor = ? AND run_type = ?",
                [platform, scale_factor, run_type],
            ).fetchone()
        return json.loads(row[0]) if row else None

    def record_sql_result(
        self,
        platform: str,
        query_id: str,
        block: str,
        scale_factor: int,
        run_type: str,
        run_number: int,
        wall_clock_ms: float | None,
        server_time_ms: float | None,
        row_count: int | None,
        cost_usd: float | None,
        cost_confidence: str | None,
        error_message: str | None,
        metadata: dict | None = None,
        concurrency: int | None = None,
        bytes_scanned: int | None = None,
        bytes_spilled: int | None = None,
        compile_ms: float | None = None,
        queue_ms: float | None = None,
        cache_hit_pct: float | None = None,
        platform_query_id: str | None = None,
        provenance_id: str | None = None,
        compute_label: str | None = None,
    ) -> None:
        """Record one measured query execution.

        Performance + cost only: correctness is not judged here (the
        runners do not compare results; validate-sf1 owns correctness).
        ``bytes_spilled`` / ``compile_ms`` / ``queue_ms`` / ``cache_hit_pct`` are
        best-effort engine profiling (NULL where the engine doesn't expose them);
        ``provenance_id`` links the row to its ``benchmark_run`` record.
        """
        meta_json = json.dumps(metadata) if metadata else None
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO sql_benchmark_results
                  (platform, query_id, block, scale_factor, run_type, run_number,
                   concurrency, wall_clock_ms, server_time_ms, bytes_scanned,
                   bytes_spilled, compile_ms, queue_ms, cache_hit_pct,
                   cost_usd, cost_confidence, row_count,
                   error_message, platform_query_id, provenance_id, compute_label,
                   metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    platform,
                    query_id,
                    block,
                    scale_factor,
                    run_type,
                    run_number,
                    concurrency,
                    wall_clock_ms,
                    server_time_ms,
                    bytes_scanned,
                    bytes_spilled,
                    compile_ms,
                    queue_ms,
                    cache_hit_pct,
                    cost_usd,
                    cost_confidence,
                    row_count,
                    error_message,
                    platform_query_id,
                    provenance_id,
                    compute_label,
                    meta_json,
                ],
            )

    def record_run_provenance(
        self,
        *,
        command: str,
        scale_factor: int | None,
        run_type: str | None,
        platforms: str | None,
        blocks: str | None,
        git_sha: str | None,
        git_dirty: bool | None,
        harness_version: str | None,
        optimize_model: str | None,
        query_files_hash: str | None,
        tool_versions: dict | None,
    ) -> str:
        """Insert one ``benchmark_run`` provenance record and return its id.

        Called once at the start of a ``sql``/``cold``/``concurrency`` run; the id
        is threaded into every ``record_sql_result`` so results are reproducible.
        """
        with self.connect() as con:
            row = con.execute(
                """
                INSERT INTO benchmark_run
                  (command, scale_factor, run_type, platforms, blocks,
                   git_sha, git_dirty, harness_version, optimize_model,
                   query_files_hash, tool_versions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING provenance_id
                """,
                [
                    command,
                    scale_factor,
                    run_type,
                    platforms,
                    blocks,
                    git_sha,
                    git_dirty,
                    harness_version,
                    optimize_model,
                    query_files_hash,
                    json.dumps(tool_versions) if tool_versions else None,
                ],
            ).fetchone()
        return str(row[0])

    def unreconciled_cells(
        self, platform: str, *, missing_queue: bool = False, missing_cpu: bool = False
    ) -> list[tuple[int, str]]:
        """Every ``(scale_factor, run_type)`` for ``platform`` still holding rows that
        :meth:`rows_to_reconcile` would return.

        Exists because `reconcile`'s ``--scale``/``--run-type`` default to one cell
        while a campaign writes many. A bare `reconcile --platforms X` after a
        multi-cell campaign would reconcile one cell and look like a success: the
        failure mode is silent, and for an engine that collects no server time
        inline it loses the entire measurement.
        """
        with self.connect() as con:
            return [
                (int(scale), run_type)
                for scale, run_type in con.execute(
                    f"SELECT DISTINCT scale_factor, run_type FROM sql_benchmark_results"
                    f" WHERE platform = ? AND {_RECONCILE_NEEDS}"
                    " ORDER BY scale_factor, run_type",
                    [platform, missing_queue, missing_cpu],
                ).fetchall()
            ]

    def rows_to_reconcile(
        self,
        platform: str,
        scale_factor: int,
        run_type: str = "warm",
        *,
        missing_queue: bool = False,
        missing_cpu: bool = False,
    ) -> list[str]:
        """Distinct captured query ids needing reconciliation for a platform at
        a scale: NULL server time, a non-direct cost, or a **zero/NULL cost on
        a successful run** (history can land after the run records the row).
        Rows whose queries genuinely consumed no billed time remain candidates on
        every pass and simply update 0 — harmless and honest. For
        :data:`ATTRIBUTION_ONLY_PLATFORMS` (Snowflake) the cost is rate-priced and
        never a gap: a successful row is a candidate until reconcile records its
        ``metadata.attributed_credits``, within :data:`ATTRIBUTION_HISTORY_DAYS`.

        ``missing_queue`` also selects rows with no ``queue_ms`` yet, for engines
        whose reconciler back-fills it (``analysis.reconcile.QUEUE_RECONCILERS``).
        A timed-out row with a captured id qualifies through its NULL server time,
        so its queue time is back-filled too.

        ``missing_cpu`` likewise selects rows with no ``metadata.cpu_ms`` yet, for
        engines whose cost is split by CPU share
        (``analysis.reconcile.CPU_RECONCILERS``), but only rows recorded within
        :data:`CPU_HISTORY_DAYS`: older history is gone, so they could never
        update."""
        with self.connect() as con:
            rows = con.execute(
                f"SELECT DISTINCT platform_query_id FROM sql_benchmark_results"
                f" WHERE {_RECONCILE_CANDIDATE}",
                [platform, scale_factor, run_type, missing_queue, missing_cpu],
            ).fetchall()
        return [r[0] for r in rows]

    def oldest_reconcile_candidate(
        self,
        platform: str,
        scale_factor: int,
        run_type: str = "warm",
        *,
        missing_queue: bool = False,
        missing_cpu: bool = False,
    ):
        """Earliest ``created_at`` among rows still needing reconciliation (the same
        filter as :meth:`rows_to_reconcile`, shared as ``_RECONCILE_CANDIDATE``). Lets
        the reconciler auto-widen its history lookback to actually cover the runs it's
        back-filling, instead of a fixed window that silently drops older runs.
        Sharing the filter keeps the two in step: a narrower one here would leave a
        back-fill of weeks-old runs looking only at recent history.
        Returns a ``datetime`` or None."""
        with self.connect() as con:
            row = con.execute(
                f"SELECT MIN(created_at) FROM sql_benchmark_results WHERE {_RECONCILE_CANDIDATE}",
                [platform, scale_factor, run_type, missing_queue, missing_cpu],
            ).fetchone()
        return row[0] if row else None

    def update_reconciled_metrics(
        self,
        platform_query_id: str,
        *,
        server_time_ms: float | None,
        cost_usd: float | None,
        cost_confidence: str | None,
        bytes_scanned: int | None = None,
        bytes_spilled: int | None = None,
        compile_ms: float | None = None,
        queue_ms: float | None = None,
        metadata_extra: dict | None = None,
    ) -> None:
        """Back-fill authoritative server time + reconciled cost for every row
        with this captured query id. COALESCE keeps existing values when a
        reconciled field is NULL (never wipes good data). ``bytes_scanned`` /
        ``bytes_spilled`` / ``compile_ms`` / ``queue_ms`` are back-filled the same
        way (for engines whose profiling lands in history, e.g. MotherDuck).
        ``metadata_extra``, when given, is JSON-merge-patched into each row's
        ``metadata`` (adds/overwrites those keys, e.g. MotherDuck's authoritative
        start/end, keeping the rest)."""
        with self.connect() as con:
            con.execute(
                """
                UPDATE sql_benchmark_results
                SET server_time_ms = COALESCE(?, server_time_ms),
                    cost_usd        = COALESCE(?, cost_usd),
                    cost_confidence = COALESCE(?, cost_confidence),
                    bytes_scanned   = COALESCE(?, bytes_scanned),
                    bytes_spilled   = COALESCE(?, bytes_spilled),
                    compile_ms      = COALESCE(?, compile_ms),
                    queue_ms        = COALESCE(?, queue_ms)
                WHERE platform_query_id = ?
                """,
                [
                    server_time_ms,
                    cost_usd,
                    cost_confidence,
                    bytes_scanned,
                    bytes_spilled,
                    compile_ms,
                    queue_ms,
                    platform_query_id,
                ],
            )
            if metadata_extra:
                con.execute(
                    """
                    UPDATE sql_benchmark_results
                    SET metadata = json_merge_patch(COALESCE(metadata, '{}'), ?)
                    WHERE platform_query_id = ?
                    """,
                    [json.dumps(metadata_extra), platform_query_id],
                )

    def get_completed(
        self, platform: str, scale_factor: int, run_type: str
    ) -> set[tuple[str, int]]:
        """Return (query_id, run_number) tuples that already have results.

        Used by the SQL runner (`sql_benchmark.runner.run_sql_benchmark`) to resume
        after a crash without re-running completed work. Errors do NOT count as
        completed — the runner will retry them on the next invocation.
        """
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT query_id, run_number
                FROM sql_benchmark_results
                WHERE platform = ?
                  AND scale_factor = ?
                  AND run_type = ?
                  AND error_message IS NULL
                """,
                [platform, scale_factor, run_type],
            ).fetchall()
        return {(r[0], r[1]) for r in rows}

    # ---- Scale skips ----------------------------------------------------

    def record_scale_skip(
        self,
        scale_factor: int,
        platform: str,
        reason: str,
        skipped_by: str | None = None,
        block: str = "*",
        compute_label: str = "*",
        run_type: str = "*",
    ) -> None:
        """Record a deliberate omission.

        Args:
            scale_factor: the scale skipped.
            platform: the engine skipped.
            reason: why — this is the whole point of the row, since it is what
                distinguishes a decision from an oversight.
            skipped_by: who or what recorded it.
            block: the suite skipped, or ``'*'`` for every suite at that cell. A
                per-suite skip is common (a run plan may skip DSB at 1 TB while
                TPC-DS runs there), and recording it whole-scale would overstate
                the omission.
            compute_label: the compute size skipped, or ``'*'`` for every size.
                Redshift runs 32/64/128 RPU at 1 TB and cannot finish at 8/16;
                without this the only way to record that was to mark the whole
                scale, which reads as "never ran there".
            run_type: the run type skipped (``warm``, ``concurrency``, ...), or
                ``'*'`` for every run type. Redshift 8/16 RPU are out of scope for
                the 1 TB warm suite and measured under load there.
        """
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO scale_skips
                  (scale_factor, platform, block, compute_label, run_type, reason, skipped_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (scale_factor, platform, block, compute_label, run_type) DO UPDATE SET
                  reason = excluded.reason,
                  skipped_by = excluded.skipped_by,
                  skipped_at = now()
                """,
                [scale_factor, platform, block, compute_label, run_type, reason, skipped_by],
            )

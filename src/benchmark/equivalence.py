"""Equivalence guardrails for the optimization pass (``optimize_pass``).

True SQL equivalence is undecidable statically, and the per-engine optimization
guidance deliberately endorses aggressive idiomatic restructuring (self-join ->
``MAX_BY``, correlated subquery -> window function, ``UNION ALL`` -> ``GROUPING
SETS``, ``sum(if)`` -> ``sumIf``, adding ``TO_DATE(...,'YYYY-MM-DD')`` formats).
Any AST-shape / element-multiset invariant would reject those *valid* rewrites,
so a structural diff is the wrong tool. The trustworthy check is to **execute
both and compare result sets** — exactly the oracle the project already uses for
ground truth.

This module provides two layers, both reused by ``optimize_pass``:

* **Output-arity tripwire** (:func:`arity_mismatch`) — the one structural
  invariant that survives even aggressive idiomatic rewrites with zero false
  positives: an optimization must not change the number of output columns. Near
  free; runs even when execution must be deferred.

* **Local execute-and-compare gate** (:class:`LocalEquivalenceChecker`) — for
  TPC-DS / DSB it transpiles the optimized engine SQL back to DuckDB, runs it
  against the same local SF1 Parquet the ground truth was built from, and
  compares the result set to the stored ground-truth rows with
  :func:`compare_result_sets` (1e-4 relative tolerance, multiset). Engine-only
  idioms that don't round-trip to DuckDB (ClickHouse ``PREWHERE`` / ``SETTINGS``,
  etc.), missing local data, or a missing baseline yield ``deferred`` — meaning
  the live ``validate-sf1`` gate on the real engine is authoritative for that
  query. This catches breakage at optimize time, before any cloud spend.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import duckdb
import sqlglot

from .datagen.clickbench import create_clickbench_hits_view
from .datagen.tpcds import TPCDS_TABLES
from .results import ResultsDB
from .sql_benchmark.validator import compare_result_sets, deserialize_rows

logger = logging.getLogger("benchmark.equivalence")

# Suites we can re-execute locally. TPC-DS / DSB load their 24 SF1 tables;
# ClickBench loads its single typed `hits.parquet` (data/clickbench/hits.parquet)
# as a `hits` view. Its ground truth is keyed at SF100 (the single ClickBench
# scale), so checks look it up there rather than at the harness scale factor.
_LOCAL_SUITES = {"tpcds", "dsb", "clickbench"}
_CLICKBENCH_SCALE = 100

# Equivalence verdicts.
EQUIV_EQUAL = "equal"  # ran locally, result set matches ground truth within tolerance
EQUIV_DIFFER = "differ"  # ran locally, result set diverged — a real regression
EQUIV_DEFERRED = "deferred"  # couldn't verify locally; live validate-sf1 is authoritative
EQUIV_ERROR = "error"  # the checker itself failed (treated as deferred by the caller)


def output_arity(sql: str, dialect: str | None) -> int | None:
    """Number of output columns of ``sql`` in ``dialect`` (None if unparseable).

    Uses ``named_selects`` so ``UNION`` / set operations report the arity of the
    first branch (the output arity of the whole statement).
    """
    if not dialect:
        return None
    try:
        expr = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return None
    if expr is None:
        return None
    try:
        names = expr.named_selects
    except Exception:
        return None
    return len(names) if names else None


def arity_mismatch(original: str, optimized: str, dialect: str | None) -> str | None:
    """Return a message if the two queries differ in output-column count.

    Returns ``None`` when arities match or when either side can't be parsed (so
    a dialect gap never manufactures a false mismatch). An arity change is an
    unambiguous bug — a rewrite that adds or drops a projected column cannot be
    result-equivalent.
    """
    a = output_arity(original, dialect)
    b = output_arity(optimized, dialect)
    if a is None or b is None or a == b:
        return None
    return f"output column count changed: {a} -> {b}"


@dataclass(frozen=True)
class EquivResult:
    """Outcome of a local equivalence check."""

    status: str  # one of EQUIV_*
    detail: str

    @property
    def is_differ(self) -> bool:
        return self.status == EQUIV_DIFFER


class LocalEquivalenceChecker:
    """Execute-and-compare equivalence against the local SF1 ground truth.

    One instance is shared across the optimize pass's worker threads. Each suite
    gets one lazily-loaded in-memory DuckDB (Parquet loaded once) guarded by a
    lock, since DuckDB connections are not safe for concurrent use.
    """

    def __init__(
        self,
        results_db: ResultsDB,
        data_root: Path = Path("data"),
        scale_factor: int = 1,
    ) -> None:
        self._db = results_db
        self._data_root = data_root
        self._scale = scale_factor
        self._lock = threading.Lock()
        # suite -> connection, or None when the suite's data couldn't be loaded
        # (cached so we don't re-attempt the load per query).
        self._cons: dict[str, duckdb.DuckDBPyConnection | None] = {}

    def _gt_scale(self, block: str) -> int:
        """Ground-truth scale factor for a block (ClickBench is keyed at SF100)."""
        return _CLICKBENCH_SCALE if block == "clickbench" else self._scale

    def _suite_con(self, suite: str) -> duckdb.DuckDBPyConnection | None:
        """Lazily load a suite's Parquet into an in-memory DuckDB.

        TPC-DS / DSB load their 24 SF1 tables; ClickBench loads the single typed
        ``hits.parquet`` as a ``hits`` view (same view builder as ground-truth
        generation). Returns the connection, or ``None`` if the data isn't
        present locally (the caller then defers to the live gate). Holds
        ``self._lock`` — callers already hold it.
        """
        if suite in self._cons:
            return self._cons[suite]

        if suite == "clickbench":
            hits = self._data_root / "clickbench" / "hits.parquet"
            if not hits.exists():
                logger.info("equivalence: clickbench hits.parquet missing (%s); deferring", hits)
                self._cons[suite] = None
                return None
            con = duckdb.connect(":memory:")
            try:
                create_clickbench_hits_view(con, hits)
            except Exception:
                logger.warning("equivalence: failed loading clickbench; deferring", exc_info=True)
                con.close()
                self._cons[suite] = None
                return None
            self._cons[suite] = con
            return con

        suite_dir = self._data_root / suite / f"sf{self._scale}"
        if not suite_dir.exists():
            logger.info("equivalence: %s data dir missing (%s); deferring", suite, suite_dir)
            self._cons[suite] = None
            return None
        con = duckdb.connect(":memory:")
        try:
            for table in TPCDS_TABLES:
                path = suite_dir / f"{table}.parquet"
                if not path.exists():
                    logger.info("equivalence: %s missing %s; deferring suite", suite, path.name)
                    con.close()
                    self._cons[suite] = None
                    return None
                con.execute(
                    f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_parquet(?)",
                    [str(path)],
                )
        except Exception:
            logger.warning("equivalence: failed loading %s; deferring", suite, exc_info=True)
            con.close()
            self._cons[suite] = None
            return None
        self._cons[suite] = con
        return con

    def check(self, query_id: str, block: str, optimized_sql: str, dialect: str) -> EquivResult:
        """Verify ``optimized_sql`` is result-equivalent to the ground truth.

        Args:
            query_id: e.g. ``"tpcds_q01"`` — key into ``ground_truth_hashes``.
            block: ``"tpcds"`` | ``"dsb"`` | ``"clickbench"``.
            optimized_sql: the candidate rewrite, in the engine's dialect.
            dialect: the engine's sqlglot dialect (read side of the transpile).

        Returns:
            An :class:`EquivResult`. ``deferred`` means "couldn't decide locally
            — let the live engine gate confirm", never "looks fine".
        """
        if block not in _LOCAL_SUITES:
            return EquivResult(EQUIV_DEFERRED, f"{block}: no local re-execution path")

        truth = self._db.get_ground_truth(query_id, self._gt_scale(block))
        if truth is None or truth.get("result_rows") is None:
            return EquivResult(EQUIV_DEFERRED, "no stored ground-truth rows")
        truth_rows = deserialize_rows(truth["result_rows"])

        # Round-trip the optimized engine SQL back to DuckDB so it runs against
        # the reference data. A transpile failure means an engine-only idiom we
        # can't re-execute locally -> defer, don't reject.
        try:
            duck_sql = sqlglot.transpile(optimized_sql, read=dialect, write="duckdb")
        except Exception as e:
            return EquivResult(EQUIV_DEFERRED, f"not expressible in DuckDB: {str(e)[:120]}")
        if not duck_sql:
            return EquivResult(EQUIV_DEFERRED, "transpile produced no statement")

        with self._lock:
            con = self._suite_con(block)
            if con is None:
                return EquivResult(
                    EQUIV_DEFERRED, f"{block} SF{self._gt_scale(block)} data not local"
                )
            try:
                rows = con.execute(duck_sql[0]).fetchall()
            except Exception as e:
                # Could be a transpile-fidelity gap OR a genuinely broken rewrite.
                # We can't tell locally, so defer — the live gate is authoritative.
                return EquivResult(EQUIV_DEFERRED, f"DuckDB execution failed: {str(e)[:120]}")

        passed, reason = compare_result_sets(rows, truth_rows, optimized_sql)
        if passed:
            return EquivResult(EQUIV_EQUAL, reason)
        return EquivResult(EQUIV_DIFFER, reason)

    def close(self) -> None:
        with self._lock:
            for con in self._cons.values():
                if con is not None:
                    try:
                        con.close()
                    except Exception:
                        logger.debug("equivalence: con close failed", exc_info=True)
            self._cons.clear()

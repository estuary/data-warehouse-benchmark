"""Correctness validator — compares engine results against DuckDB ground truth.

The validate-sf1 gate runs on each engine at SF1 and confirms the result of
every canonical query matches the local-DuckDB ground truth.
This is essentially a one-pass run that:
  1. Executes each query on the target engine (single measured run)
  2. Hashes the result
  3. Looks up `ground_truth_hashes` for the same query at the same scale
  4. Logs pass/fail per query and returns a structured summary
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from ..connectors.base import WarehouseConnector
from ..queries.registry import BenchmarkQuery, QueryRegistry
from ..results import ResultsDB

logger = logging.getLogger("benchmark.sql_benchmark.validator")

# Relative tolerance for numeric comparison — the spec's 1e-4 (CLAUDE.md
# §Result Comparison Tolerance). Used by the element-wise comparator below.
RELATIVE_TOLERANCE = 1e-4

# Queries with no total row order — their result sets legitimately differ across
# engines, so exact-match correctness comparison is meaningless. They are still
# executed (to catch engine errors) and timed, but NOT result-checked. This
# mirrors ClickBench itself, which is purely a timing benchmark and never
# compares results across engines (verified against the ClickBench repo: no
# answer key, no result hashing, no cross-engine equivalence — only that a query
# runs and how fast). All 8 are ClickBench queries that lack a total order:
#   q18  GROUP BY ... LIMIT 10            — no ORDER BY at all (arbitrary 10)
#   q25  ORDER BY EventTime LIMIT 10      — many rows tie on EventTime
#   q32  ORDER BY COUNT(*) DESC LIMIT 10  — WatchID ~unique, mass ties at count=1
#   q33  (same as q32, no WHERE filter)
#   q39  ORDER BY PageViews DESC LIMIT 10 OFFSET 1000   — ties at the offset cutoff
#   q40  (same shape, OFFSET 1000)
#   q41  (same shape, OFFSET 100)
#   q42  (same shape, OFFSET 10000)
# Adding ORDER BY tiebreakers to force determinism is forbidden (it changes the
# canonical query). So these are classified perf-only rather than masked by a
# fuzzy comparator.
# ClickBench ships vendor-approved per-engine query files for every engine
# except Microsoft Fabric. We import those verbatim (queries/<engine>/clickbench/),
# so they are TRUSTED: executed to confirm they run on the loaded table, but NOT
# result-compared — ClickBench itself never compares results across engines, and
# the vendor's own query is authoritative. Fabric has no ClickBench variant, so
# we translate it ourselves and therefore DO validate Fabric's ClickBench results
# against the DuckDB ground truth.
CLICKBENCH_TRANSLATED_PLATFORMS: frozenset[str] = frozenset({"fabric"})


def _clickbench_trusted(platform: str, block: str) -> bool:
    """True when ``platform``'s ClickBench query is the vendor-approved import
    (so it's run-but-not-result-compared); False for Fabric (we translate it)."""
    return block == "clickbench" and platform not in CLICKBENCH_TRANSLATED_PLATFORMS


NONDETERMINISTIC_QUERY_IDS: frozenset[str] = frozenset(
    {
        # q59 (both suites): ORDER BY (s_store_name, s_store_id, d_week_seq)
        # has massive ties — the store dimension carries multiple s_store_sk
        # versions per s_store_id (SCD), so each (name, id, week) key repeats
        # once per version-pair with different ratio values — and LIMIT 100
        # cuts inside a tie group (verified at SF1: tpcds cuts a 49-row group
        # at row 98, dsb a 98-row group). Which tied rows survive the cutoff
        # is engine-arbitrary; full un-LIMITed result sets match within
        # tolerance. tpcds_q59 passes on some engines only because their sorts
        # happen to pick the same rows as DuckDB.
        "tpcds_q59",
        "dsb_q59",
        # q67 (both suites): RANK() OVER (... ORDER BY sumsales DESC) where
        # sumsales = SUM(price * quantity) over DOUBLE PRECISION columns.
        # Groups whose sums are cent-equal tie or split depending on each
        # engine's floating-point summation order: at SF1 DuckDB produced four
        # bit-identical 152312.06 rows (all rank 9) where Redshift's sums
        # differed at the ULP (…060000003 / …06 / …059999997 → ranks 9/9/11/12).
        # The rk column therefore has no engine-independent value; with rk
        # excluded the full result multisets match exactly (verified at SF1 on
        # Redshift). Other engines pass by luck of matching DuckDB's summation
        # bit patterns.
        "tpcds_q67",
        "dsb_q67",
        "clickbench_q18",
        "clickbench_q25",
        "clickbench_q32",
        "clickbench_q33",
        "clickbench_q39",
        "clickbench_q40",
        "clickbench_q41",
        "clickbench_q42",
    }
)

# Floating-point comparison: the spec uses a *relative* tolerance of 1e-4
# (CLAUDE.md §Result Comparison Tolerance). Hash equality can't express a true
# relative tolerance (boundary values always differ), so we round to a fixed
# number of *significant figures* — a relative quantization that absorbs
# low-order FP drift on large aggregates far better than fixed decimal-place
# (absolute) rounding. The exact 1e-4 comparison is element-wise over stored
# ground-truth rows (:func:`compare_result_sets`); the hash is the fallback.
# Ground truth and engine results both pass through this, so it's symmetric.
SIGNIFICANT_FIGURES = 6


def _round_sig(f: float) -> str:
    """Render ``f`` rounded to SIGNIFICANT_FIGURES significant figures."""
    if f == 0.0:
        return "0"
    digits = SIGNIFICANT_FIGURES - 1 - math.floor(math.log10(abs(f)))
    return f"{round(f, digits):.{max(digits, 0)}f}"


def _normalize_value(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, Decimal | float):
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return str(f)
        return _round_sig(f)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, datetime | date | time):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


def _normalize_row(row: tuple) -> str:
    return "|".join(_normalize_value(v) for v in row)


def _hash_result(rows: list[tuple], ordered: bool = True) -> str:
    normalized = [_normalize_row(r) for r in rows]
    if not ordered:
        # Unordered result sets (no ORDER BY) compare as multisets: sort the
        # normalized rows so row order doesn't affect the hash. This also
        # normalizes NULL placement (NULLS FIRST/LAST) for those queries.
        normalized.sort()
    h = hashlib.sha256()
    for line in normalized:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _has_order_by(sql: str) -> bool:
    """Heuristic: does the query impose a deterministic top-level row order?

    Decides ordered vs multiset hashing. A query with ORDER BY is order-
    significant; without one, rows compare as a multiset. (Subquery-only ORDER
    BY is rare in the suite; treating it as unordered is conservative — sorting
    can only let two genuinely-equal result sets match.)
    """
    return "ORDER BY" in sql.upper()


def hash_query_result(rows: list[tuple], sql: str) -> str:
    """Order-aware result hash: ordered when the query has ORDER BY, else a
    multiset hash. Shared by ground-truth generation and validate-sf1 so both
    sides of every comparison normalize identically.
    """
    return _hash_result(rows, ordered=_has_order_by(sql))


# ---------------------------------------------------------------------------
# Tolerance-aware element-wise comparison
#
# Hash equality can't express a relative tolerance (two values 1e-9 apart can
# round to different strings at a boundary), so a hash mismatch over-reports:
# results that are equal within the spec's 1e-4 still fail. The functions below
# compare the actual rows element-wise. They need the ground-truth *rows*, not
# just a hash — so `serialize_rows` stores them and `compare_result_sets` does
# the comparison the spec describes: 1e-4 relative tolerance on numerics, set
# (multiset) semantics, NULL normalization, and column alignment by name. (No
# LIMIT tie-break tolerance: it would be unsound; an ORDER BY ... LIMIT k that
# returns different boundary rows is a real diff.)
# ---------------------------------------------------------------------------


def _cell_to_json(v: object) -> object:
    """Convert one result cell to a JSON-safe, comparison-friendly form.

    Numerics stay numeric (so tolerance applies); temporal/binary become tagged
    so they round-trip without being mistaken for numbers or strings.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return {"__bool__": v}
    if isinstance(v, Decimal | float):
        return float(v)
    if isinstance(v, int):
        return v
    if isinstance(v, datetime | date | time):
        return {"__temporal__": v.isoformat()}
    if isinstance(v, bytes):
        return {"__bytes__": v.hex()}
    return str(v)


def serialize_rows(rows: Sequence[Sequence[object]]) -> str:
    """JSON-serialize result rows for storage in ``ground_truth_hashes``."""
    return json.dumps([[_cell_to_json(c) for c in row] for row in rows])


def deserialize_rows(blob: str) -> list[list]:
    """Inverse of :func:`serialize_rows`."""
    return json.loads(blob)


def serialize_columns(columns: Sequence[str]) -> str:
    """JSON-serialize a result's column-name list for ``ground_truth_hashes``."""
    return json.dumps(list(columns))


def _align_engine_columns(
    engine_rows: Sequence[Sequence[object]],
    engine_columns: Sequence[str] | None,
    truth_columns: Sequence[str] | None,
) -> list[list] | None:
    """Reorder each engine row to the ground truth's column order, by name.

    Returns rows containing exactly the ground-truth columns, in ground-truth
    order, matched case-insensitively — which both reorders columns (engines
    materialize ``SELECT *`` in different orders) and drops engine-only columns
    not present in the reference (e.g. Estuary's ``_meta/file`` key columns).

    Returns ``None`` when alignment isn't possible or needed (missing names,
    duplicate names, or a ground-truth column absent from the engine result), so
    the caller falls back to a positional comparison.
    """
    if not engine_columns or not truth_columns:
        return None
    idx: dict[str, int] = {}
    for i, name in enumerate(engine_columns):
        key = name.lower()
        if key in idx:
            return None  # duplicate column name — can't align unambiguously
        idx[key] = i
    try:
        order = [idx[c.lower()] for c in truth_columns]
    except KeyError:
        return None  # a reference column is missing from the engine result
    return [[row[i] for i in order] for row in engine_rows]


def _cells_equal(a: object, b: object) -> bool:
    """Tolerant equality for one cell pair (already in serialized JSON form)."""
    # Normalize the tagged forms first.
    if isinstance(a, dict) or isinstance(b, dict):
        return a == b
    if a is None or b is None:
        return a is None and b is None
    a_num = isinstance(a, int | float) and not isinstance(a, bool)
    b_num = isinstance(b, int | float) and not isinstance(b, bool)
    if a_num and b_num:
        fa, fb = float(a), float(b)
        if math.isnan(fa) or math.isnan(fb):
            return math.isnan(fa) and math.isnan(fb)
        if math.isinf(fa) or math.isinf(fb):
            return fa == fb
        if fa == fb:
            return True
        return abs(fa - fb) <= RELATIVE_TOLERANCE * max(abs(fa), abs(fb))
    return a == b


def _rows_equal(r1: Sequence[object], r2: Sequence[object]) -> bool:
    return len(r1) == len(r2) and all(_cells_equal(x, y) for x, y in zip(r1, r2, strict=False))


def _multiset_diff(engine: list[list], truth: list[list]) -> tuple[int, list[list], list[list]]:
    """Greedy tolerant multiset match. Returns (matched, unmatched_engine,
    unmatched_truth). O(n*m) — fine for SF1 result sizes (≤ a few thousand)."""
    remaining = list(truth)
    used = [False] * len(remaining)
    unmatched_engine: list[list] = []
    matched = 0
    for er in engine:
        hit = -1
        for i, tr in enumerate(remaining):
            if not used[i] and _rows_equal(er, tr):
                hit = i
                break
        if hit >= 0:
            used[hit] = True
            matched += 1
        else:
            unmatched_engine.append(er)
    unmatched_truth = [tr for i, tr in enumerate(remaining) if not used[i]]
    return matched, unmatched_engine, unmatched_truth


def compare_result_sets(
    engine_rows: Sequence[Sequence[object]],
    truth_rows: list[list],
    sql: str,
    *,
    engine_columns: Sequence[str] | None = None,
    truth_columns: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """Compare engine rows against stored ground-truth rows within tolerance.

    Returns ``(passed, reason)``. Semantics (CLAUDE.md §Result Comparison
    Tolerance): numerics compared with 1e-4 relative tolerance; rows compared as
    a multiset — which is exactly the spec's "unordered → compare as sets" and
    "ORDER BY with ties → compare as sets within tied groups" (order, including
    ambiguous tie ordering, is ignored); NULLs normalized.

    ``sql`` is accepted for interface symmetry with :func:`hash_query_result`
    but intentionally unused: there is no special-casing for ``LIMIT``. An
    ``ORDER BY … LIMIT k`` query that selects genuinely different rows at the
    cutoff is a real difference, not something to wave through — masking it would
    also hide actual defects (e.g. an off-by-one filter on a LIMIT query). Such
    cases surface as a mismatch for human review.
    """
    del sql  # see docstring — no LIMIT/ORDER-BY special-casing
    # Align engine columns to the reference by name when both column lists are
    # known (handles SELECT * column-order differences and drops engine-only
    # columns like Estuary's _meta key). Falls back to positional otherwise.
    aligned = _align_engine_columns(engine_rows, engine_columns, truth_columns)
    source_rows = aligned if aligned is not None else engine_rows
    engine = [[_cell_to_json(c) for c in row] for row in source_rows]
    if len(engine) != len(truth_rows):
        return False, "row count mismatch"

    _matched, only_engine, only_truth = _multiset_diff(engine, truth_rows)
    if not only_engine and not only_truth:
        return True, "match"
    return False, "value mismatch"


@dataclass
class QueryValidationResult:
    query: BenchmarkQuery
    passed: bool
    reason: (
        str  # "match" | "hash mismatch" | "row count mismatch" | "engine error" | "no ground truth"
    )
    engine_row_count: int | None
    truth_row_count: int | None
    error: str | None
    # True for a ClickBench vendor-approved imported query (run, not compared).
    trusted: bool = False
    # True when the query has no total row order (no ORDER BY, or ties at a
    # LIMIT/OFFSET cutoff), so its result set can't be compared exactly across
    # engines. Such a query is executed (to catch engine errors) but its rows
    # are NOT result-checked — it's perf-only, mirroring ClickBench itself, which
    # never compares results across engines. Reported separately, not as a fail.
    nondeterministic: bool = False


def validate_query(
    platform: str,
    connector: WarehouseConnector,
    results_db: ResultsDB,
    scale_factor: int,
    q: BenchmarkQuery,
) -> QueryValidationResult:
    """Validate one query on an already-connected ``connector`` against truth.

    Shared by :func:`validate_platform` and the live ``validate-sf1`` auto-revert
    re-check, so both normalize comparisons identically. The caller owns the
    connection lifecycle (this does not open or close ``connector``).
    """
    # ClickBench vendor-approved imports (every engine but Fabric): execute to
    # confirm the query runs on the loaded table, but don't result-compare.
    if _clickbench_trusted(platform, q.block):
        result = connector.execute_query(q.sql)
        if result.error_message:
            err_short = result.error_message[:200]
            logger.info("%s/%s %s: engine error — %s", platform, q.block, q.id, err_short)
            return QueryValidationResult(
                query=q,
                passed=False,
                reason="engine error",
                engine_row_count=None,
                truth_row_count=None,
                error=err_short,
            )
        logger.info(
            "%s/%s %s: trusted (ClickBench-approved; ran OK, %s rows, not compared)",
            platform,
            q.block,
            q.id,
            result.row_count,
        )
        return QueryValidationResult(
            query=q,
            passed=True,
            reason="trusted (ClickBench-approved query; executed, not result-compared)",
            engine_row_count=result.row_count,
            truth_row_count=None,
            error=None,
            trusted=True,
        )

    truth = results_db.get_ground_truth(q.id, scale_factor)
    if truth is None or truth["result_hash"] is None:
        logger.info("%s/%s %s: no ground truth", platform, q.block, q.id)
        return QueryValidationResult(
            query=q,
            passed=False,
            reason="no ground truth",
            engine_row_count=None,
            truth_row_count=None,
            error=None,
        )

    result = connector.execute_query(q.sql)
    if result.error_message:
        err_short = result.error_message[:200]
        logger.info("%s/%s %s: engine error — %s", platform, q.block, q.id, err_short)
        return QueryValidationResult(
            query=q,
            passed=False,
            reason="engine error",
            engine_row_count=None,
            truth_row_count=truth["row_count"],
            error=err_short,
        )

    # Nondeterministic queries (no total order) executed without error: that is
    # the only check applicable — their rows can't be compared exactly across
    # engines, so skip the comparison and report perf-only (not a failure).
    if q.id in NONDETERMINISTIC_QUERY_IDS:
        logger.info(
            "%s/%s %s: nondeterministic — executed OK, results not compared (%s rows)",
            platform,
            q.block,
            q.id,
            result.row_count,
        )
        return QueryValidationResult(
            query=q,
            passed=True,
            reason="nondeterministic (perf-only; no total order, results not compared)",
            engine_row_count=result.row_count,
            truth_row_count=truth["row_count"],
            error=None,
            nondeterministic=True,
        )

    # Prefer element-wise tolerance comparison against the stored ground-truth
    # rows (the spec's 1e-4 relative tolerance, multiset semantics). Align the
    # engine's columns to the reference by name (when stored) so SELECT *
    # column-order differences and engine-only _meta columns don't false-fail.
    # Fall back to the hash when the ground truth was recorded without rows.
    if truth.get("result_rows") is not None:
        truth_cols = truth.get("result_columns")
        passed, reason = compare_result_sets(
            result.rows,
            deserialize_rows(truth["result_rows"]),
            q.sql,
            engine_columns=result.columns,
            truth_columns=json.loads(truth_cols) if truth_cols else None,
        )
    else:
        passed = hash_query_result(result.rows, q.sql) == truth["result_hash"]
        reason = (
            "match"
            if passed
            else (
                "row count mismatch" if result.row_count != truth["row_count"] else "hash mismatch"
            )
        )

    logger.info(
        "%s/%s %s: %s (engine %s rows, truth %s)",
        platform,
        q.block,
        q.id,
        reason,
        result.row_count,
        truth["row_count"],
    )
    return QueryValidationResult(
        query=q,
        passed=passed,
        reason=reason,
        engine_row_count=result.row_count,
        truth_row_count=truth["row_count"],
        error=None,
    )


def validate_platform(
    platform: str,
    connector: WarehouseConnector,
    results_db: ResultsDB,
    scale_factor: int,
    blocks: list[str] | None = None,
) -> list[QueryValidationResult]:
    """Run every registered query on `platform` once and validate against truth.

    Args:
        blocks: Optional filter; only queries whose block is in this list run.
            None means all blocks (tpcds + dsb + clickbench). Useful when the
            caller wants to validate one suite at a time against a per-suite
            schema (e.g., DSB queries run against benchmark_db.dsb_sf1, not
            benchmark_db.tpcds_sf1).
    """
    registry = QueryRegistry(dialect=platform)
    if blocks is None:
        queries = list(registry.all())
    else:
        block_set = set(blocks)
        queries = [q for q in registry.all() if q.block in block_set]
    summary: list[QueryValidationResult] = []

    with connector:
        for q in queries:
            summary.append(validate_query(platform, connector, results_db, scale_factor, q))

    passed = sum(1 for s in summary if s.passed)
    logger.info(
        "validation complete",
        extra={
            "platform": platform,
            "scale_factor": scale_factor,
            "passed": passed,
            "total": len(summary),
        },
    )
    return summary

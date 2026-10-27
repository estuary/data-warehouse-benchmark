"""Per-engine query optimization pass.

For each translated query, ask Claude to rewrite it to be idiomatic for the
target engine. For MotherDuck this is typically a no-op (the source dialect is
already DuckDB); the codepath still runs so any parse errors surface uniformly.
The published results do not use this pass: every engine runs the translated SQL.

The pass writes an audit log to ``results/optimization_<platform>.json`` (model,
timestamp, per-query status) so a run is always verifiable — Golden Rule #4
("record everything"). It raises :class:`OptimizeNotConfiguredError` rather than
silently no-opping when no Anthropic API key is configured, so an explicitly
requested optimization can never be mistaken for "done".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlglot

from .equivalence import EQUIV_DEFERRED, LocalEquivalenceChecker, arity_mismatch
from .queries.registry import QUERIES_ROOT
from .settings import get_settings
from .translator import ENGINE_DIALECTS

logger = logging.getLogger("benchmark.optimize_pass")

# A rewritten query that doesn't parse in the engine's dialect is fed back to
# the model with the parse error for correction, up to this many total attempts
# (1 initial + retries). After that the original is kept.
DEFAULT_MAX_ATTEMPTS = 3

# Backends for the per-query rewrite. "claude-cli" shells one `claude -p`
# subprocess per query — each is a fresh, isolated context (no bleed between
# queries, no leakage from any surrounding agent context) and uses Claude Code
# auth instead of an API key. "sdk" calls the Anthropic API directly.
OPTIMIZE_BACKENDS = ("claude-cli", "sdk")
DEFAULT_OPTIMIZE_BACKEND = "claude-cli"
DEFAULT_CLI_TIMEOUT_S = 180
DEFAULT_JOBS = 4

# The `claude` CLI's org-token validation is rate-limited under sustained
# headless `claude -p` load — it fails with "could not verify organization …
# this may be a network" and, once tripped, stays failing for longer than a
# short fixed backoff covers. So the claude-cli backend retries the transient
# class with EXPONENTIAL backoff + EQUAL JITTER up to a high ceiling: the long
# ceiling lets a throttled query wait out the cooldown, and the jitter keeps
# retries from re-hitting the validator in a synchronized cadence that would
# keep it hot. Delay for retry attempt i = uniform(c/2, c) where
# c = min(CAP, BASE * FACTOR**i). A non-transient failure (a real CLI usage
# error) is raised immediately rather than wastefully retried.
_CLI_RETRY_MAX = 5  # retries after the initial attempt
_CLI_RETRY_BASE_S = 5.0
_CLI_RETRY_FACTOR = 3.0
_CLI_RETRY_CAP_S = 120.0


def _backoff_delay(attempt: int) -> float:
    """Equal-jitter exponential backoff for retry ``attempt`` (0-based)."""
    ceiling = min(_CLI_RETRY_CAP_S, _CLI_RETRY_BASE_S * _CLI_RETRY_FACTOR**attempt)
    return random.uniform(ceiling / 2, ceiling)


_CLI_TRANSIENT_MARKERS = (
    "verify organization",
    "could not be validated",
    "network",
    "rate limit",
    "overloaded",
    "timed out",
    "timeout",
    "connection",
    "temporarily",
)


def _is_transient_cli_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _CLI_TRANSIENT_MARKERS)


# A rewrite backend: takes the full prompt, returns the model's raw text reply.
Rewriter = Callable[[str], str]

# The translated queries are all SELECT/WITH; a reply that doesn't start with a
# SQL keyword is prose (the model ignored "return only SQL") — treat as a
# failure and keep the original rather than writing garbage.
_SQL_PREFIXES = ("select", "with", "insert", "update", "delete", "create", "(")


# DuckDB reference semantics (the correctness baseline) — verified empirically
# against the local DuckDB the ground truth is built with, NOT from vendor
# summaries (several of which had the DESC NULL default and integer-division
# behaviour wrong):
#   * ORDER BY places NULLs LAST for BOTH ASC and DESC (default_null_order).
#   * `/` is true/fractional division (5/2 = 2.5); `//` is floor division.
#   * String comparison is binary — case-sensitive AND trailing-space-sensitive.
#   * DECIMAL arithmetic is exact; TIMESTAMP is timezone-naive (wall clock).
# Per-engine guidance below records, with a vendor-doc citation, where the
# engine DIVERGES from that baseline (CORRECTNESS) or can run faster without
# changing results (PERFORMANCE). Rules are query-rewrite level only — physical
# design (distribution/sort/cluster keys, partitioning) lives in
# physical_design.json, and pure session settings live in the connector.


@dataclass(frozen=True)
class EngineGuidance:
    """Per-engine optimization guidance, split by intent."""

    correctness: tuple[str, ...] = ()
    performance: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.correctness and not self.performance


ENGINE_GUIDANCE: dict[str, EngineGuidance] = {
    # sources: docs.snowflake.com operators-arithmetic, data-types-numeric,
    # operators-query, constructs/order-by, date-time-input-output,
    # data-types-datetime, collation, functions/approx_count_distinct,
    # user-guide/tables-clustering-micropartitions
    "snowflake": EngineGuidance(
        correctness=(
            # Validated SF1 dsb_q83: chained DECIMAL division lost ~2.7e-4.
            "DECIMAL division sets output scale to max(S1, min(S1+6, 12)) and ROUNDS, "
            "so chained ratios like (a*1.0000)/b/3.0000*100 re-quantize at each step and "
            "drift from the reference. Cast division operands to DOUBLE or a wide "
            "DECIMAL(38,n) (or ROUND at a fixed scale) so the value matches within 1e-4.",
            "Minimize division steps: prefer SUM(x)/SUM(y) over AVG(x/y) and multiply "
            "first / divide last — only when algebraically identical.",
            "Cast FLOAT/DOUBLE columns to NUMBER(p,s) before SUM/AVG over many rows; "
            "float accumulation error grows at SF100+ and can exceed 1e-4.",
            # Validated SF1 dsb_q75: UNION dedup over computed DECIMALs diverged.
            "Keep UNION vs UNION ALL exactly as written, and CAST both branches' "
            "computed DECIMAL columns to one explicit NUMBER(p,s): UNION dedups after a "
            "type unification whose scale rounding can otherwise change which rows dedup.",
            "Snowflake ORDER BY defaults NULLS LAST for ASC but NULLS FIRST for DESC; the "
            "reference is NULLS LAST always — append explicit NULLS LAST to DESC (and any "
            "nullable) sort keys so top-N / LIMIT membership matches.",
            "Give date/timestamp literals explicit formats (TO_DATE('2000-01-01',"
            "'YYYY-MM-DD')); never rely on AUTO parsing, which can misread ambiguous dates.",
            "Keep timestamps as TIMESTAMP_NTZ; do not introduce TIMESTAMP_TZ/LTZ casts — "
            "they apply session-timezone offsets and shift values vs the naive reference.",
            "Do not add COLLATE / UPPER / LOWER / TRIM around keys: the default comparison "
            "is already case- and trailing-space-sensitive like the reference.",
            "Never replace exact COUNT(DISTINCT) with APPROX_COUNT_DISTINCT/HLL (~1.6% "
            "error); likewise no APPROX_PERCENTILE/APPROX_TOP_K.",
        ),
        # perf sources: docs.snowflake.com constructs/qualify, constructs/order-by,
        # user-guide/querying-top-k-pruning-optimization, operators-subquery,
        # functions/in, constructs/group-by-grouping-sets, functions/{max_by,min_by}
        performance=(
            "Keep filter/join predicates sargable — never wrap a filter column in a "
            "function (TO_CHAR/YEAR/CAST); push the cast to the literal side so "
            "micro-partition pruning still applies.",
            "Select only the needed columns (no SELECT *) — columnar storage reads only "
            "referenced columns; critical on the ~100-column ClickBench hits table.",
            "Push the most selective WHERE predicates before joins/aggregation.",
            "Let the cost-based optimizer choose join order; do not hand-pin order or hint "
            "cardinalities (DSB skew is the optimizer's job).",
            "Filter window-function results with QUALIFY instead of an outer subquery+WHERE "
            "(identical rows, one fewer query level to flatten).",
            "Keep top-N eligible for top-K pruning: pair ORDER BY with LIMIT and make the "
            "FIRST sort key a bare column (not an expression); for a nullable DESC key write "
            "DESC NULLS LAST; for grouped/joined top-N order by a GROUP BY column / a column "
            "of the larger table — only where it does not change the produced order.",
            "Drop ORDER BY inside subqueries/CTEs (keep it only at the top level with its "
            "LIMIT), and remove a DISTINCT that a GROUP BY or key already makes unique.",
            "Use EXISTS for existence checks (semi-join) and NOT EXISTS rather than NOT IN "
            "over a nullable subquery; don't bury the subquery under an OR branch.",
            "Collapse UNION ALL of the same table aggregated at different grains into one "
            "GROUP BY GROUPING SETS / ROLLUP (one scan + one grouping operator).",
            "Replace the self-join / correlated-subquery 'value from the row with the "
            "max/min' idiom with MAX_BY/MIN_BY (single pass) when ties are not significant.",
        ),
    ),
    # sources: spark.apache.org sql-ref-syntax-qry-select-orderby, sql-migration-guide;
    # docs.databricks.com sql-ref-datatype-rules, functions/approx_count_distinct,
    # sql-ref-collation, data-types/timestamp(-ntz), optimizations/aqe
    "databricks": EngineGuidance(
        correctness=(
            "Spark ORDER BY defaults NULLS FIRST for ASC and NULLS LAST for DESC — the "
            "OPPOSITE of the reference (NULLS LAST always). Append explicit NULLS LAST to "
            "ASC (and any nullable) sort keys so ORDER BY … LIMIT membership matches.",
            "DECIMAL division can reduce result scale to a minimum of 6 and ROUND when "
            "precision would exceed 38 (allowPrecisionLoss defaults true). For wide "
            "operands, cast to DOUBLE or a deliberate DECIMAL(38,n) to match within 1e-4.",
            "Keep UNION vs UNION ALL exactly, and CAST both branches' computed columns to "
            "the reference type: least-common-type can widen mixed DECIMAL/exact-numeric "
            "branches to DOUBLE, changing rounding.",
            "Keep string comparisons under default UTF8_BINARY (case- and "
            "trailing-space-sensitive, matches reference); do not add COLLATE or "
            "TRIM/UPPER/LOWER.",
            "Do not convert TIMESTAMP <-> TIMESTAMP_NTZ or add timezone functions: "
            "TIMESTAMP is UTC-normalized, TIMESTAMP_NTZ is wall-clock; match the loaded type.",
            "Use ISO date/timestamp literals (yyyy-MM-dd[ HH:mm:ss]); do not introduce "
            "custom to_date/to_timestamp patterns.",
            "Never replace exact COUNT(DISTINCT) with approx_count_distinct (~5% error).",
        ),
        # perf sources: docs.databricks.com sql-ref-syntax-qry-select-qualify, compute/photon,
        # optimizations/{dynamic-file-pruning,aqe}, lakehouse-architecture .../best-practices;
        # spark.apache.org sql-performance-tuning
        performance=(
            "Keep ORDER BY … LIMIT N as one clause (don't wrap LIMIT in a subquery) so "
            "Photon applies bounded top-N instead of a full sort.",
            "Keep predicates on partition/clustering columns sargable (no function "
            "wrapping) so file pruning / data-skipping engages.",
            "Select only needed columns (no SELECT *) for Photon columnar pruning on the "
            "wide hits table; prune columns in intermediate CTEs too, not just the outer SELECT.",
            "Rely on AQE for join strategy and skew (DSB); do not hand-add BROADCAST / "
            "skew-join hints unless a specific query regresses.",
            "Filter window-function results with QUALIFY rather than a subquery+WHERE.",
            "Keep every expression Photon-eligible — never introduce a Python/Scala UDF where "
            "a built-in function exists; a UDF forces a JVM fallback (and serialization) for "
            "that operation.",
            "Preserve a simple equi-join on the bare key between a filtered dimension and the "
            "fact table so Dynamic File Pruning fires, and so AQE can broadcast the small "
            "filtered dimension; don't bury the dimension filter behind non-equi or "
            "expression-wrapped join keys.",
            "Pre-aggregate the fact in a subquery before joining dimensions when the grouping "
            "keys preserve grain (shrinks the join/shuffle input).",
            "Replace correlated scalar subqueries with a join to a pre-aggregated derived "
            "table or a window function; drop ORDER BY inside subqueries and redundant DISTINCT.",
        ),
    ),
    # sources: cloud.google.com/bigquery/docs reference/standard-sql {query-syntax,
    # operators, conversion_rules, conversion_functions, mathematical_functions,
    # approximate_aggregate_functions, collation-concepts, data-types};
    # best-practices-performance-compute, querying-partitioned-tables, clustered-tables
    "bigquery": EngineGuidance(
        correctness=(
            "Write UNION ALL or UNION DISTINCT explicitly — bare UNION is a syntax error; "
            "render the reference's bare UNION as UNION DISTINCT (and INTERSECT/EXCEPT "
            "DISTINCT).",
            "BigQuery ORDER BY defaults NULLS FIRST for ASC and NULLS LAST for DESC; the "
            "reference is NULLS LAST always — append explicit NULLS LAST to ASC (and any "
            "nullable) sort keys.",
            "Keep operand numeric types as the reference used them; do not widen "
            "NUMERIC/INT64 into FLOAT64 (forces approximate IEEE-754). NUMERIC is exact at "
            "scale 9; use BIGNUMERIC (scale 38) when more scale is needed within 1e-4.",
            "INT64/INT64 `/` already returns FLOAT64 (matches the reference's fractional "
            "division); use DIV/FLOOR only where the reference used floor division (//). "
            "For exact decimal division, cast operands to NUMERIC.",
            "Use the bare `/`; only use SAFE_DIVIDE when divide-by-zero must yield NULL; "
            "never IEEE_DIVIDE (coerces to FLOAT64, returns inf/NaN).",
            "Keep DATE/DATETIME/TIMESTAMP distinct and assume UTC; do not interchange them "
            "(TIMESTAMP->DATE truncates as of UTC).",
            "Default string comparison is case-sensitive code-point order; do not add "
            "COLLATE (und:ci) or UPPER/LOWER folding.",
            "Never replace exact COUNT(DISTINCT) with APPROX_COUNT_DISTINCT/HLL sketches.",
        ),
        # perf sources: cloud.google.com/bigquery/docs best-practices-performance-compute,
        # query-insights, reference/standard-sql/{query-syntax,subqueries,operators}
        performance=(
            "Project only needed columns; never SELECT * (BigQuery bills/reads per "
            "referenced column and LIMIT does not reduce bytes for SELECT *). Use "
            "SELECT * EXCEPT(...) when most columns are needed.",
            "Filter the partitioning (date) column directly and join partition predicates "
            "with AND, not OR (OR disables pruning).",
            "Put filters and GROUP BY keys on the leading clustering-column prefix.",
            "Order joins largest table first (promotes broadcast of small dimensions); "
            "filter early and pre-aggregate before joins; order remaining joins so the most "
            "row-reducing ones run first.",
            "Filter window-function results with QUALIFY instead of a subquery+WHERE.",
            "Replace self-joins with window functions (LAG/LEAD/SUM() OVER/RANK) — one pass "
            "instead of re-scanning (year-over-year, running totals).",
            "Replace correlated/per-row scalar subqueries with a window aggregate or a join "
            "to a pre-aggregated table (a correlated subquery re-evaluates per outer row).",
            "GoogleSQL does NOT materialize WITH — a non-recursive CTE referenced N times "
            "runs N times; materialize an expensive multiply-referenced CTE into a TEMP TABLE.",
            "Use NOT EXISTS instead of NOT IN (subquery) for anti-joins (NULL-safe + "
            "semi/anti-join plan) when the subquery column is non-nullable or NULLs filtered.",
            "Mitigate DSB skew: process the few hot key values separately and recombine the "
            "rest with UNION ALL; filter NULL/empty join keys before the JOIN/GROUP BY when "
            "they don't contribute. Drop ORDER BY in subqueries.",
        ),
    ),
    # sources: docs.aws.amazon.com/redshift r_numeric_computations201, r_ORDER_BY_clause,
    # r_Datetime_types, r_patternmatching_condition_like, r_COUNT,
    # t_Sorting_data, c_best-practices-sort-key
    "redshift": EngineGuidance(
        correctness=(
            "INTEGER/INTEGER division returns an INTEGER and truncates; the reference `/` "
            "is fractional — cast one operand (a::DECIMAL(38,6) / b) to keep the fraction. "
            "Do not add a cast where the reference used floor division (//).",
            "DECIMAL division scale = max(4, s1+p2-s2+1), capped at precision 38; cast the "
            "numerator (or both operands) to DECIMAL(38,n) with n>=6 in multi-step ratios "
            "so precision stays within 1e-4 of the reference.",
            "Set operators cap result DECIMAL precision at 19 (narrower than arithmetic); "
            "CAST both UNION branches' computed DECIMALs to one explicit type so dedup and "
            "scale match. Keep UNION vs UNION ALL exactly.",
            "Redshift ORDER BY defaults NULLS LAST for ASC and NULLS FIRST for DESC; the "
            "reference is NULLS LAST always — append explicit NULLS LAST to DESC (and any "
            "nullable) sort keys.",
            "Keep TIMESTAMP (without time zone); do not introduce TIMESTAMPTZ casts, which "
            "apply the session time zone.",
            "Note: Redshift `=`/GROUP BY ignore trailing spaces on CHAR/VARCHAR, but the "
            "reference is trailing-space-sensitive. If a key can carry trailing spaces this "
            "may merge groups/matches — flag it; do not silently swap `=` for LIKE.",
            "Never replace exact COUNT(DISTINCT) with APPROXIMATE COUNT(DISTINCT) (~2% error).",
        ),
        # perf sources: docs.aws.amazon.com/redshift c_designing-queries-best-practices,
        # r_in_condition, r_correlated_subqueries, r_scalar_subqueries;
        # prescriptive-guidance .../best-practices-designing-queries
        performance=(
            "Keep predicates on the sort-key column sargable (no function wrapping) so "
            "zone-map block-skipping works; push casts to the literal side. Write date "
            "filters as an open BETWEEN range on the bare column (d_date >= '1998-01-01' AND "
            "d_date < '1999-01-01') rather than datepart(year,…)=… or col::date=….",
            "Push selective WHERE predicates before joins to cut rows redistributed across "
            "slices (matters most on DSB's skewed joins).",
            "Select only needed columns (no SELECT *).",
            "Collapse multiple filtered passes over the same table into one scan with CASE "
            "inside the aggregate: SUM(CASE WHEN cond THEN x END) instead of separate "
            "queries or self-joins.",
            "Duplicate a selective predicate onto every joined table it can legally apply to "
            "(e.g. a date_dim range filter also on the fact's date column) — Redshift filters "
            "each table before the join; the result set is unchanged.",
            "List GROUP BY columns starting with the leading sort-key column(s) in sort-key "
            "order (enables one-phase aggregation); when both GROUP BY and ORDER BY are "
            "present, list their columns in the same order to avoid a second sort.",
            "Turn a restriction-only subquery returning more than ~200 rows into a JOIN; use "
            "NOT EXISTS instead of NOT IN over a nullable subquery; drop inner ORDER BY and "
            "redundant DISTINCT.",
        ),
    ),
    # sources: learn.microsoft.com t-sql precision-scale-and-length, divide-transact-sql,
    # select-order-by-clause, top-transact-sql, string-comparison-assignment,
    # collations/collation-and-unicode-support; fabric/data-warehouse {collation,
    # data-types, guidelines-warehouse-performance}; t-sql/data-types/datetime2
    "fabric": EngineGuidance(
        correctness=(
            "T-SQL `/` on two integers does INTEGER division (5/2 = 2); the reference `/` "
            "is fractional — cast one operand: CAST(a AS DECIMAL(38,6)) / b (or a * 1.0).",
            "DECIMAL division infers scale max(6, s1+p2+1) capped at 38 and ROUNDS when the "
            "integral part is large; wrap divisions feeding compared values in "
            "CAST(... AS DECIMAL(38,6)) or ROUND(..., n) at a sufficient fixed scale.",
            "Multiplication result precision is p1+p2+1; when it exceeds 38, scale is "
            "reduced or it overflows. Cast intermediate products to a controlled "
            "DECIMAL(38,s) with s>=6 in long price*qty*discount chains.",
            "T-SQL has no NULLS FIRST/LAST and treats NULL as the lowest value (ASC->NULLs "
            "first, DESC->NULLs last); the reference is NULLS LAST always. Emulate with a "
            "leading key: ORDER BY CASE WHEN col IS NULL THEN 1 ELSE 0 END, col.",
            "T-SQL `=`/GROUP BY/DISTINCT/joins ignore trailing spaces (ANSI padding: "
            "'a' = 'a '), but the reference is trailing-space-sensitive — if keys may carry "
            "trailing spaces this merges groups/join matches; flag and normalize consistently.",
            "Translate LIMIT n to `ORDER BY … OFFSET 0 ROWS FETCH NEXT n ROWS ONLY` (or "
            "TOP (n)); carry the reference ORDER BY verbatim — do not add tiebreaker columns.",
            "Coerce UNION/EXCEPT/INTERSECT branch computed columns to one explicit type, and "
            "keep UNION vs UNION ALL exactly.",
            "Use datetime2 (datetime/smalldatetime/datetimeoffset are unsupported) and "
            "ISO-unambiguous date literals; use only Fabric-supported types in casts "
            "(no tinyint/nchar/nvarchar/money/text — map to smallint/varchar/etc.).",
        ),
        # perf sources: learn.microsoft.com fabric/data-warehouse/{guidelines-warehouse-
        # performance,data-clustering}, sql/t-sql/queries/hints-transact-sql-query
        performance=(
            "Filter before joining and push the most selective predicates earliest; do not "
            "rely on PK/UNIQUE constraints (not enforced) as join-quality signals.",
            "Keep join/group/filter key types identical across compared columns; avoid CAST "
            "inside predicates (it blocks rowgroup elimination).",
            "Select only needed columns (no SELECT *); trim each CTE/derived table to only "
            "the columns and rows the outer query consumes (intermediate result size drives "
            "cost).",
            "Keep filter predicates sargable on the bare column (range/equality) so rowgroup "
            "/ file skipping and statistics apply; and filter on decimal columns of precision "
            "<= 18 — predicates on decimal(p>18) are not pushed to storage.",
            "Rewrite single-node 'non-scalable operation' shapes (global sort, final-step "
            "join) by reducing the filtered set first; as a last resort append "
            "OPTION (FORCE DISTRIBUTED PLAN) (result-neutral).",
            "Replace correlated/scalar subqueries with joins to pre-aggregated derived tables "
            "or window functions, and self-joins (ranking/running-total) with "
            "ROW_NUMBER/RANK/SUM() OVER; pre-aggregate the fact before joining dimensions.",
            "Drop ORDER BY in subqueries/CTEs that lack TOP/OFFSET-FETCH, remove a DISTINCT a "
            "GROUP BY already makes unique, and use NOT EXISTS instead of NOT IN over a "
            "nullable subquery.",
        ),
    ),
    # sources: clickhouse.com/docs sql-reference/statements/select/{union,order-by,group-by},
    # aggregate-functions/reference/count, functions/arithmetic-functions,
    # engines/table-engines/special/join, data-types/datetime64; resources query-optimisation
    "clickhouse": EngineGuidance(
        correctness=(
            "Never emit a bare UNION (it does not deduplicate, and errors by default): emit "
            "UNION DISTINCT for the reference's UNION and UNION ALL for UNION ALL.",
            "Outer joins fill unmatched columns with TYPE DEFAULTS (0, '', epoch) instead of "
            "NULL unless join_use_nulls=1. Emit `SETTINGS join_use_nulls = 1` so IS NULL "
            "anti-joins and coalesce over right-side columns match the reference.",
            "Keep plain JOIN (default ALL strictness); never rewrite to ANY … JOIN, which "
            "returns only the first match and changes cardinality.",
            "Every non-aggregated SELECT/HAVING/ORDER BY expression must be in GROUP BY; "
            "never paper over a stray column with any() (returns an arbitrary value).",
            "Keep `/` as floating division; map the reference's floor division (//) to "
            "intDiv(); do not optimize `/` into intDiv.",
            "Keep DECIMAL columns in Decimal arithmetic; do not down-cast to Float64 "
            "mid-query (shifts large-magnitude results beyond 1e-4).",
            "Never replace exact count(DISTINCT) (uniqExact) with uniq/uniqCombined (~2% error).",
            "Use explicit toDate/toDateTime for literals (not parseDateTimeBestEffort), keep "
            "Date vs DateTime grain consistent, and assume UTC.",
        ),
        # perf sources: clickhouse.com/docs sql-reference/aggregate-functions/combinators,
        # guides/joining-tables, sql-reference/functions/date-time-functions; blog
        # choose-the-right-algorithm; resources query-optimisation
        performance=(
            "Move the most selective single-column filter on the wide hits table into "
            "PREWHERE (semantically equivalent to WHERE).",
            "Put the smaller / more-filtered table on the RIGHT of a JOIN — ClickHouse "
            "builds the hash table from the right side.",
            "Align filters and GROUP BY with the MergeTree ORDER BY / primary key for "
            "granule pruning; group on LowCardinality columns where available.",
            "Select only needed columns (no SELECT *); push column pruning into subqueries "
            "feeding joins so intermediate results stay narrow.",
            "Use -If aggregate combinators (sumIf(x,cond), countIf(cond), avgIf) to compute "
            "conditional aggregates in one pass instead of sum(if(...))/CASE or extra "
            "subqueries.",
            "Filter each table inside a subquery/CTE BEFORE the join, not in the outer WHERE "
            "after it — far less data is materialized into the join.",
            "Keep joins to <= 3-4 and pre-aggregate the fact before joining dimensions so the "
            "join sees fewer rows; for large joins that risk memory, pin a spill-capable "
            "algorithm per query with SETTINGS join_algorithm='grace_hash' (result-neutral).",
            "When a join only restricts rows (no columns selected from the right), rewrite it "
            "as WHERE key IN (SELECT key FROM right …).",
            "Use native toStartOf* date-bucketing functions (not formatDateTime/string "
            "truncation) so the bucket expression can match the primary key.",
        ),
    ),
    # MotherDuck IS DuckDB — the reference dialect — so the translated SQL is already
    # idiomatic and correctness rules vs the reference are N/A. Keep guidance minimal.
    "motherduck": EngineGuidance(
        performance=(
            "Select only needed columns (no SELECT *) on the wide ClickBench hits table.",
        ),
    ),
}


def _guidance_block(engine: str) -> str:
    """Render the engine's guidance for the prompt (empty string if none)."""
    g = ENGINE_GUIDANCE.get(engine)
    if g is None or g.is_empty():
        return ""
    parts = [f"\nEngine-specific rules for {engine} (the query is authored in DuckDB):"]
    if g.correctness:
        parts.append("\nCORRECTNESS — these defend result equivalence; apply them:")
        parts.extend(f"- {r}" for r in g.correctness)
    if g.performance:
        parts.append("\nPERFORMANCE — faster with identical results:")
        parts.extend(f"- {r}" for r in g.performance)
    return "\n".join(parts) + "\n"


def _guidance_audit(engine: str) -> dict[str, list[str]]:
    """JSON-serializable record of the guidance applied (for the audit log)."""
    g = ENGINE_GUIDANCE.get(engine)
    if g is None:
        return {"correctness": [], "performance": []}
    return {"correctness": list(g.correctness), "performance": list(g.performance)}


OPTIMIZATION_PROMPT = """\
You are rewriting a SQL query that runs on {engine}. Make it idiomatic for that
engine: use engine-specific syntax where it improves performance, re-order
joins/filters where it helps the optimizer, and use the engine's preferred
date/time/cast functions.

The query was authored in DuckDB, the correctness reference; your rewrite MUST
return the same result within 1e-4 relative tolerance. DuckDB reference
semantics: ORDER BY places NULLs LAST for both ASC and DESC; `/` is true
(fractional) division and `//` is floor division; string comparison is binary
(case- and trailing-space-sensitive); DECIMAL arithmetic is exact; TIMESTAMP is
timezone-naive. The engine's own type/set/ordering rules can silently change
results — follow the engine-specific rules below to prevent that. Do NOT add
ORDER BY tiebreaker columns the original query did not have.
{guidance_block}
Return only the rewritten SQL, no commentary.

Original query (DuckDB dialect, already syntactically translated to {engine}):
```sql
{sql}
```
"""


OPTIMIZATION_RETRY_PROMPT = """\
The SQL below, intended for {engine}, failed to parse. Parser error:

{error}

Return a corrected version: valid {engine} SQL that preserves the original
result (within 1e-4 relative tolerance) and still honors the engine-specific
rules. Return only the corrected SQL, no commentary.

```sql
{sql}
```
"""


OPTIMIZATION_ARITY_PROMPT = """\
The SQL below, intended for {engine}, changed the number of output columns
relative to the original ({error}). A correct optimization MUST return exactly
the same columns, in the same order, as the original query. Return a corrected
version with the original projection restored — valid {engine} SQL that still
honors the engine-specific rules. Return only the corrected SQL, no commentary.

```sql
{sql}
```
"""


OPTIMIZATION_EQUIV_PROMPT = """\
The SQL below, intended for {engine}, parses and runs but produces a DIFFERENT
result set than the DuckDB reference ({error}). The rewrite changed the query's
results, which is not allowed — the output must match the reference within 1e-4
relative tolerance. Re-examine the engine-specific CORRECTNESS rules (set
semantics, NULL ordering, decimal/division scale, type coercion) that can
silently shift results, and return a corrected version that preserves the
original result. Return only the corrected SQL, no commentary.

```sql
{sql}
```
"""


class OptimizeNotConfiguredError(RuntimeError):
    """Raised when the optimization pass is requested but cannot run.

    Covers a missing API key, a missing ``anthropic`` package, or no translated
    queries for the platform. The pass never silently no-ops when invoked.
    """


def _sdk_rewriter(model: str) -> Rewriter:
    """Build a rewriter backed by the Anthropic API (one stateless call/query)."""
    settings = get_settings()
    api_key_secret = settings.ANTHROPIC_API_KEY
    api_key = api_key_secret.get_secret_value() if api_key_secret else None
    if not api_key:
        raise OptimizeNotConfiguredError(
            "BENCHMARK_ANTHROPIC_API_KEY is not set — set it, use --via claude-cli, "
            "or skip `optimize` to run the sqlglot-translated SQL as-is."
        )
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise OptimizeNotConfiguredError("the `anthropic` package is not installed") from e

    client = Anthropic(api_key=api_key)

    def rewrite(prompt: str) -> str:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )

    return rewrite


# Claude Code injects session/auth-routing vars into the shells it spawns. A
# `claude -p` nested under a Claude Code session inherits them and authenticates
# as a *child session* — pinned to the parent's org with a session token it
# can't validate headlessly — which fails with "could not verify organization".
# Stripping any var with these prefixes makes the subprocess authenticate as a
# standalone CLI using the user's persistent ~/.claude login, so the pass works
# identically whether launched from a Claude Code session or a plain terminal.
_CLAUDE_CODE_ENV_PREFIXES = ("CLAUDE_CODE", "CLAUDECODE", "CLAUDE_EFFORT")


def _standalone_cli_env() -> dict[str, str]:
    """The current environment minus Claude Code's session-injection vars.

    Reads ``os.environ`` deliberately: this is process management (handing a
    cleaned environment to a child), not configuration access — there is no
    settings equivalent for "the live process environment".
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not any(k.startswith(p) for p in _CLAUDE_CODE_ENV_PREFIXES)
    }


def _claude_cli_rewriter(model: str | None, timeout_s: int) -> Rewriter:
    """Build a rewriter that shells one ``claude -p`` per query.

    Each call is its own process with a fresh context — no bleed between
    queries, no leakage from any surrounding agent session. Tools are disabled
    (``--tools ""``) so it is a pure text transform, not an agentic run; the
    prompt is fed on stdin to avoid argv-length/quoting limits. The subprocess
    runs with a sanitized environment (see :func:`_standalone_cli_env`) so it
    uses the standalone CLI login even when launched from a Claude Code session.
    """
    exe = shutil.which("claude")
    if not exe:
        raise OptimizeNotConfiguredError(
            "the `claude` CLI is not on PATH — install Claude Code or use --via sdk"
        )
    cmd = [exe, "-p", "--output-format", "text", "--tools", ""]
    if model:
        cmd += ["--model", model]
    child_env = _standalone_cli_env()

    def rewrite(prompt: str) -> str:
        # Fixed argv (no shell), prompt fed on stdin, sanitized env. Transient
        # CLI failures (flaky org validation) are retried with backoff; a
        # non-transient failure is raised at once.
        last_error = "claude -p failed"
        for attempt in range(_CLI_RETRY_MAX + 1):
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=timeout_s,
                    env=child_env,
                )
            except subprocess.TimeoutExpired:
                last_error = f"claude -p timed out after {timeout_s}s"  # transient
            else:
                if proc.returncode == 0:
                    return proc.stdout
                detail = (proc.stderr or proc.stdout or "").strip()[:200]
                last_error = f"claude -p exited {proc.returncode}: {detail}"
                if not _is_transient_cli_error(detail):
                    raise RuntimeError(last_error)
            # Transient: back off (jittered) and retry while attempts remain.
            if attempt < _CLI_RETRY_MAX:
                delay = _backoff_delay(attempt)
                logger.warning(
                    "transient claude -p failure (attempt %d/%d); retrying in %.1fs: %s",
                    attempt + 1,
                    _CLI_RETRY_MAX + 1,
                    delay,
                    last_error,
                )
                time.sleep(delay)
        raise RuntimeError(f"{last_error} (after {_CLI_RETRY_MAX} retries)")

    return rewrite


def _make_rewriter(backend: str, model: str | None, timeout_s: int) -> Rewriter:
    # Both backends pin BENCHMARK_OPTIMIZE_MODEL (Opus 4.8) when no model is
    # given, so the rewrite is reproducible rather than depending on the
    # ambient Claude Code default.
    resolved = model or get_settings().OPTIMIZE_MODEL
    if backend == "sdk":
        return _sdk_rewriter(resolved)
    if backend == "claude-cli":
        return _claude_cli_rewriter(resolved, timeout_s)
    raise OptimizeNotConfiguredError(
        f"unknown optimize backend {backend!r}; use one of {OPTIMIZE_BACKENDS}"
    )


def _looks_like_sql(text: str) -> bool:
    head = text.lstrip().lower()
    return any(head.startswith(p) for p in _SQL_PREFIXES)


def _sql_parse_error(sql: str, dialect: str) -> str | None:
    """Return a parse-error string if ``sql`` doesn't parse in ``dialect``, else None."""
    try:
        sqlglot.parse(sql, read=dialect)
        return None
    except Exception as e:  # ParseError, TokenizeError, and anything else sqlglot raises
        return str(e).strip()[:300]


def _query_identity(sql_path, platform: str) -> tuple[str | None, str | None]:
    """Map ``queries/<platform>/<block>/query_NN.sql`` -> ``(query_id, block)``.

    Mirrors :class:`QueryRegistry`'s id convention so the equivalence checker can
    look the query up in ``ground_truth_hashes``. Returns ``(None, None)`` for a
    path that doesn't match the ``<block>/query_NN.sql`` shape.
    """
    try:
        block = sql_path.parent.name
        number = int(sql_path.stem.replace("query_", ""))
    except (ValueError, AttributeError):
        return None, None
    qid = f"{block}_q{number:02d}" if number < 100 else f"{block}_q{number}"
    return qid, block


# --- resumable progress journal ----------------------------------------------
#
# Each completed query is appended (atomically, under a lock) to a per-platform
# JSONL the instant it finishes — so a run killed mid-way (out of credits,
# Ctrl-C, OOM) leaves a durable record of what's done. A resume reloads it and
# skips any query already optimized whose file still matches the recorded
# output, re-spending LLM calls only on the unfinished/errored remainder. The
# completed-status entries carry ``output_hash`` (sha256 of the on-disk SQL) so
# a file edited or re-translated since the last run is correctly re-optimized.
_COMPLETED_STATUSES = ("rewritten", "unchanged")


def _progress_path(platform: str):
    return QUERIES_ROOT.parent / "results" / f"optimization_{platform}.progress.jsonl"


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _append_progress(platform: str, entry: dict, lock: threading.Lock) -> None:
    """Append one completed-query entry to the progress JSONL (atomic per line)."""
    path = _progress_path(platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry)
    with lock, path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _load_progress(platform: str) -> dict[str, dict]:
    """Read the progress JSONL -> ``{query_id: entry}`` (last line per id wins).

    Tolerates a torn final line from a hard kill mid-append.
    """
    path = _progress_path(platform)
    if not path.exists():
        return {}
    done: dict[str, dict] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue  # torn final line from an abrupt kill — ignore, re-run that query
        qid = entry.get("query_id")
        if qid:
            done[qid] = entry
    return done


def _compact_progress(platform: str, entries: list[dict], lock: threading.Lock) -> None:
    """Rewrite the progress JSONL to one line per query (post-run, bounds growth).

    Merges this run's ``entries`` over the existing journal (by query_id) rather
    than overwriting it — so a scoped run (``--queries``) updates only the
    queries it touched and preserves every other query's recorded state.
    """
    path = _progress_path(platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = _load_progress(platform)
    for e in entries:
        qid = e.get("query_id")
        if qid:
            merged[qid] = e
    with lock:
        lines = [json.dumps(e) for e in merged.values()]
        path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")


def optimize_for_platform(
    platform: str,
    model: str | None = None,
    *,
    backend: str = DEFAULT_OPTIMIZE_BACKEND,
    jobs: int = DEFAULT_JOBS,
    timeout_s: int = DEFAULT_CLI_TIMEOUT_S,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    equiv_checker: LocalEquivalenceChecker | None = None,
    force: bool = False,
    queries: frozenset[str] | None = None,
) -> int:
    """Rewrite ``queries/<platform>/**/*.sql`` in place via the chosen backend.

    Each candidate rewrite passes a sequence of gates before it is accepted:
    parses in the engine's sqlglot dialect, preserves the original output-column
    count, and (when ``equiv_checker`` is supplied) executes locally to a result
    set matching the DuckDB ground truth within tolerance. A failed gate feeds
    the specific error back to the model for correction (up to ``max_attempts``);
    if no candidate passes, the original is kept.

    Resumable: every completed query is journaled to
    ``results/optimization_<platform>.progress.jsonl`` as it finishes, so a run
    killed mid-way (out of credits, Ctrl-C) resumes without re-spending LLM
    calls — already-optimized queries whose file still matches the journal are
    skipped. ``force=True`` discards the journal and re-optimizes everything.

    Args:
        platform: Engine id whose translated queries to optimize.
        model: Model id/alias. Both backends default to ``BENCHMARK_OPTIMIZE_MODEL``.
        backend: ``"claude-cli"`` (default, isolated subprocess per query) or
            ``"sdk"`` (Anthropic API).
        jobs: Concurrent queries (each its own subprocess/API call).
        timeout_s: Per-query timeout for the claude-cli backend.
        max_attempts: Total model attempts per query before keeping the original.
        equiv_checker: Optional local execute-and-compare gate. When present,
            each rewrite must match the SF1 ground truth (or be ``deferred`` to
            the live ``validate-sf1`` gate); a divergence triggers a retry.
        force: Ignore (and clear) the resume journal; re-optimize every query.
        queries: Optional set of query ids (e.g. ``{"dsb_q59"}``); when given,
            only those queries are processed and the journal merge preserves the
            rest. Useful to re-optimize a specific straggler.

    Returns:
        Number of queries actually rewritten in this run (excludes resumed/skipped).

    Raises:
        OptimizeNotConfiguredError: backend unavailable (no key / no `claude` on
            PATH / unknown backend) or no translated queries for the platform.
    """
    target_dir = QUERIES_ROOT / platform
    if not target_dir.exists():
        raise OptimizeNotConfiguredError(
            f"no translated queries at {target_dir}; run `translate` first"
        )
    rewrite = _make_rewriter(backend, model, timeout_s)
    dialect = ENGINE_DIALECTS.get(platform)
    paths = sorted(target_dir.rglob("*.sql"))
    if queries is not None:
        paths = [p for p in paths if _query_identity(p, platform)[0] in queries]
        if not paths:
            raise OptimizeNotConfiguredError(
                f"no queries matched {sorted(queries)} under {target_dir}"
            )
    progress_lock = threading.Lock()

    # Resume: --force re-optimizes regardless of journal status (see
    # _already_done). Only wipe the whole journal for a full forced run — a
    # scoped (--queries) force must preserve the untouched queries' records,
    # which the compaction merge then re-persists. The journal is always loaded
    # so a query counts as done only when its status is completed AND the file
    # still matches the journaled output (a re-translated/edited query re-runs).
    if force and queries is None:
        _progress_path(platform).unlink(missing_ok=True)
    done = _load_progress(platform)

    def _already_done(sql_path, qid: str | None) -> dict | None:
        if force or not qid:
            return None
        entry = done.get(qid)
        if not entry or entry.get("status") not in _COMPLETED_STATUSES:
            return None
        output_hash = entry.get("output_hash")
        if output_hash and _hash_text(sql_path.read_text(encoding="utf-8")) == output_hash:
            return entry
        return None

    def process(sql_path) -> dict[str, str | int]:
        rel = str(sql_path.relative_to(QUERIES_ROOT.parent))
        qid, block = _query_identity(sql_path, platform)

        def _finish(entry: dict[str, str | int]) -> dict[str, str | int]:
            # Journal the moment a query finishes so a later kill can resume.
            if qid:
                entry.setdefault("query_id", qid)
            _append_progress(platform, entry, progress_lock)
            return entry

        original = sql_path.read_text(encoding="utf-8")
        base = original.strip()
        # Only gate on sqlglot when it can parse the original too — otherwise a
        # dialect gap (not a bad rewrite) would reject every optimization. The
        # arity tripwire is likewise skipped when the original doesn't parse.
        gate_parse = bool(dialect) and _sql_parse_error(base, dialect) is None

        candidate: str | None = None
        equiv_status: str | None = None  # verdict for the accepted candidate
        last_error = "non-SQL or empty response"
        # First attempt uses the optimization prompt; retries feed back the
        # specific gate failure against the previous candidate — each call is a
        # fresh, isolated context, so the error is the only carried state.
        prompt = OPTIMIZATION_PROMPT.format(
            engine=platform, guidance_block=_guidance_block(platform), sql=original
        )
        try:
            for attempt in range(1, max_attempts + 1):
                optimized = _strip_code_fence(rewrite(prompt).strip())

                # Gate 1: a SQL-shaped reply (else the model ignored "SQL only").
                if not optimized or not _looks_like_sql(optimized):
                    last_error = "non-SQL or empty response"
                    prompt = OPTIMIZATION_PROMPT.format(
                        engine=platform, guidance_block=_guidance_block(platform), sql=original
                    )
                    continue

                # Gate 2: parses in the engine's dialect.
                if gate_parse and (perr := _sql_parse_error(optimized, dialect)) is not None:
                    last_error = f"sqlglot parse error ({dialect}): {perr}"
                    prompt = OPTIMIZATION_RETRY_PROMPT.format(
                        engine=platform, error=perr, sql=optimized
                    )
                    logger.warning(
                        "invalid SQL for %s (attempt %d/%d); retrying", rel, attempt, max_attempts
                    )
                    continue

                # Gate 3: output-column count preserved (cheap structural tripwire).
                if gate_parse and (amsg := arity_mismatch(base, optimized, dialect)) is not None:
                    last_error = amsg
                    prompt = OPTIMIZATION_ARITY_PROMPT.format(
                        engine=platform, error=amsg, sql=optimized
                    )
                    logger.warning(
                        "arity change for %s (attempt %d/%d): %s",
                        rel,
                        attempt,
                        max_attempts,
                        amsg,
                    )
                    continue

                # Gate 4: local execute-and-compare equivalence vs ground truth.
                if equiv_checker is not None and qid and block:
                    ev = equiv_checker.check(qid, block, optimized, dialect or "")
                    if ev.is_differ:
                        last_error = f"result differs from reference: {ev.detail}"
                        prompt = OPTIMIZATION_EQUIV_PROMPT.format(
                            engine=platform, error=ev.detail, sql=optimized
                        )
                        logger.warning(
                            "equivalence differ for %s (attempt %d/%d): %s",
                            rel,
                            attempt,
                            max_attempts,
                            ev.detail,
                        )
                        continue
                    equiv_status = ev.status  # "equal" or "deferred"

                candidate = optimized
                break
        except Exception as e:  # backend error — recorded per-query; the pass continues
            logger.exception("optimization failed for %s; keeping original", sql_path)
            return _finish({"query": rel, "status": "error", "error": str(e)[:200]})

        if candidate is None:
            logger.error(
                "no valid rewrite for %s after %d attempts; keeping original", rel, max_attempts
            )
            return _finish(
                {
                    "query": rel,
                    "status": "error",
                    "error": f"{last_error} (after {max_attempts} attempts)",
                    "attempts": max_attempts,
                }
            )
        if candidate != base:
            sql_path.write_text(candidate + "\n", encoding="utf-8")
            level = logging.WARNING if equiv_status == EQUIV_DEFERRED else logging.INFO
            logger.log(level, "optimized %s (equivalence: %s)", rel, equiv_status or "unchecked")
            entry: dict[str, str | int] = {"query": rel, "status": "rewritten"}
            if equiv_status:
                entry["equivalence"] = equiv_status
            # Keep the pre-optimization SQL so the live validate-sf1 gate can
            # auto-revert a query that regresses on the real engine; output_hash
            # lets a resume detect this query is already done.
            entry["original_sql"] = base
            entry["output_hash"] = _hash_text(candidate + "\n")
            return _finish(entry)
        logger.debug("no change for %s", rel)
        return _finish({"query": rel, "status": "unchanged", "output_hash": _hash_text(original)})

    resume = {p: _already_done(p, _query_identity(p, platform)[0]) for p in paths}
    carried = [resume[p] for p in paths if resume[p] is not None]
    to_process = [p for p in paths if resume[p] is None]
    if carried:
        logger.info(
            "resume: %d/%d queries already optimized for %s; processing %d",
            len(carried),
            len(paths),
            platform,
            len(to_process),
        )

    if jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            processed = list(pool.map(process, to_process))
    else:
        processed = [process(p) for p in to_process]

    entries = carried + processed
    rewritten = sum(1 for e in processed if e["status"] == "rewritten")
    model_label = model or get_settings().OPTIMIZE_MODEL
    _write_log(platform, model_label, backend, entries, rewritten)
    # Compact the journal to one line per query so it reflects final state and
    # doesn't grow across resumes; a clean rerun then skips everything.
    _compact_progress(platform, entries, progress_lock)
    logger.info(
        "optimization complete for %s via %s: %d rewritten this run, %d resumed, %d total",
        platform,
        backend,
        rewritten,
        len(carried),
        len(entries),
    )
    return rewritten


def _write_log(
    platform: str,
    model: str,
    backend: str,
    entries: list[dict[str, str | int]],
    rewritten: int,
) -> None:
    """Write the per-platform optimization audit log to ``results/``."""
    log_path = QUERIES_ROOT.parent / "results" / f"optimization_{platform}.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "platform": platform,
        "backend": backend,
        "model": model,
        "generated_at": datetime.now(UTC).isoformat(),
        "guidance": _guidance_audit(platform),
        "total": len(entries),
        "rewritten": rewritten,
        "unchanged": sum(1 for e in entries if e["status"] == "unchanged"),
        "errors": sum(1 for e in entries if e["status"] == "error"),
        # Equivalence breakdown over the rewritten queries: how many were locally
        # verified against the SF1 ground truth vs deferred to live validate-sf1.
        "equivalence_verified": sum(1 for e in entries if e.get("equivalence") == "equal"),
        "equivalence_deferred": sum(1 for e in entries if e.get("equivalence") == EQUIV_DEFERRED),
        "queries": entries,
    }
    with log_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    logger.info("optimization log written to %s", log_path)


def load_optimization_baselines(platform: str) -> dict[str, str]:
    """Read ``results/optimization_<platform>.json`` -> ``{query_id: original_sql}``.

    Only queries that were actually rewritten carry an ``original_sql``; this is
    the pre-optimization baseline the live ``validate-sf1`` gate reverts a
    regressing query to. Returns ``{}`` if no log exists.
    """
    log_path = QUERIES_ROOT.parent / "results" / f"optimization_{platform}.json"
    if not log_path.exists():
        return {}
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("could not read optimization log %s", log_path, exc_info=True)
        return {}
    baselines: dict[str, str] = {}
    for entry in data.get("queries", []):
        qid = entry.get("query_id")
        original = entry.get("original_sql")
        if entry.get("status") == "rewritten" and qid and original:
            baselines[qid] = original
    return baselines


def revert_query_file(platform: str, query_id: str, original_sql: str) -> bool:
    """Restore ``query_id``'s file under ``queries/<platform>/`` to ``original_sql``.

    Returns True if the file was found and rewritten. The path is derived from
    the id convention (``tpcds_q01`` -> ``queries/<platform>/tpcds/query_01.sql``).
    """
    block, _, qpart = query_id.partition("_q")
    if not block or not qpart:
        return False
    try:
        number = int(qpart)
    except ValueError:
        return False
    fname = f"query_{number:02d}.sql" if number < 100 else f"query_{number}.sql"
    path = QUERIES_ROOT / platform / block / fname
    if not path.exists():
        logger.warning("revert: file not found for %s (%s)", query_id, path)
        return False
    path.write_text(original_sql.rstrip("\n") + "\n", encoding="utf-8")
    logger.info("reverted %s to pre-optimization baseline", path)
    return True


def _strip_code_fence(text: str) -> str:
    """Pull SQL out of ```sql ... ``` if the model wrapped it."""
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()

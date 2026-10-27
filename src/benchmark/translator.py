"""sqlglot translation engine — DuckDB canonical -> per-engine dialect.

Per-engine translation pass; ``validate-dialects`` gates it on a per-engine
success rate (default 80%). Outputs both the per-engine SQL files
(queries/<engine>/<block>/query_NN.sql) and a machine-readable matrix
(results/translation_matrix.json) for the gate logic.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import sqlglot
from sqlglot import exp

from .queries.registry import QUERIES_ROOT, QueryRegistry

logger = logging.getLogger("benchmark.translator")


# Map our platform IDs to sqlglot dialect names.
ENGINE_DIALECTS: dict[str, str] = {
    "snowflake": "snowflake",
    "databricks": "databricks",
    "bigquery": "bigquery",
    "redshift": "postgres",
    "fabric": "tsql",
    "clickhouse": "clickhouse",
    "motherduck": "duckdb",
}


# Per-engine post-translation regex patches for known sqlglot dialect gaps.
# sqlglot doesn't always recognize DuckDB-specific function aliases (e.g.
# DuckDB STRLEN ↔ standard LENGTH), so it passes them through verbatim and
# the target engine errors. Patches are applied AFTER sqlglot's translation
# so the canonical DuckDB queries stay pristine.
#
# Dialect gaps sqlglot does not translate on its own (QUALIFY, INTERVAL
# syntax, LIMIT vs TOP, NULL ordering, CURRENT_DATE, string func names,
# date arithmetic). Add new patches as real validate-sf1 failures surface
# them — each entry should cite the failing query(ies) for an audit trail.
#
# Convention for function-rename patches: include ``\s*\(`` in the pattern
# and the new opening paren in the replacement so we only rewrite actual
# function calls, never columns or identifiers that happen to share the
# function name.
_POST_TRANSLATE_PATCHES: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "databricks": [
        # STRLEN is DuckDB-specific; Spark SQL / Databricks uses LENGTH (no
        # STRLEN function exists). Affects clickbench_q28/q29.
        (re.compile(r"\bSTRLEN\s*\(", re.IGNORECASE), "LENGTH("),
        # NOTE (unpatched): clickbench_q29's REGEXP_REPLACE uses a `(?:...)`
        # non-capturing group + `\1` backref. Spark uses Java regex, which DOES
        # support `(?:...)` (unlike Snowflake/Redshift), so no group rewrite is
        # needed. Java replacement syntax writes the backref as `$1`, not `\1`.
    ],
    "snowflake": [
        # STRLEN is DuckDB-specific; Snowflake uses LENGTH.
        # Needed by clickbench_q28/q29.
        (re.compile(r"\bSTRLEN\s*\(", re.IGNORECASE), "LENGTH("),
        # Snowflake's regex engine rejects PCRE non-capturing groups `(?:...)`
        # ("no argument for repetition operator: ?"). ClickBench q29 uses
        # `(?:www\.)?` with backref `\1`. Drop `?:` (the www group becomes
        # capture group 1, the domain becomes group 2) and bump the replacement
        # backref `\1` -> `\2`. Snowflake string literals double-escape the
        # backslashes (`\\.`, `\\1`), so match/emit the doubled form. Mirrors
        # the Redshift patch. Needed by clickbench_q29.
        (
            re.compile(
                r"REGEXP_REPLACE\((\w+),\s*"
                r"'\^https\?://\(\?:www\\\\\.\)\?\(\[\^/\]\+\)/\.\*\$',\s*"
                r"'\\\\1'\)"
            ),
            r"REGEXP_REPLACE(\1, '^https?://(www\\\\.)?([^/]+)/.*$', '\\\\2')",
        ),
    ],
    "redshift": [
        # STRLEN is DuckDB-specific; Redshift uses LENGTH.
        # Needed by clickbench_q28/q29.
        (re.compile(r"\bSTRLEN\s*\(", re.IGNORECASE), "LENGTH("),
        # Redshift AVG keeps the argument's type: AVG(BIGINT) truncates to an
        # integer and AVG(DECIMAL(p,s)) keeps scale s, while the DuckDB ground
        # truth computes AVG as DOUBLE. Estuary loads the TPC-DS quantity
        # columns as BIGINT, so e.g. AVG(ss_quantity) returns 50 vs 50.xx.
        # Rewrite the canonical DECIMAL(12,2) casts to DOUBLE PRECISION, then
        # wrap every remaining AVG argument (one paren level allowed, e.g.
        # AVG(SUM(x)); the lookahead skips already-CAST args) in a DOUBLE
        # PRECISION cast. Needed by tpcds/dsb q07/q13/q17/q18/q22/q26/q27/q39.
        (
            re.compile(r"AVG\(CAST\(([^()]+) AS DECIMAL\(\d+,\s*\d+\)\)\)", re.IGNORECASE),
            r"AVG(CAST(\1 AS DOUBLE PRECISION))",
        ),
        (
            re.compile(r"AVG\((?!CAST\()((?:[^()]|\([^()]*\))+)\)", re.IGNORECASE),
            r"AVG(CAST(\1 AS DOUBLE PRECISION))",
        ),
        # Redshift's POSIX regex doesn't support PCRE non-capturing groups
        # `(?:...)`. ClickBench q29 uses `(?:www\.)?` with backref `\1`.
        # Rewrite the whole REGEXP_REPLACE call: drop `?:` (turning the
        # non-capturing group into capture group 1) and bump the
        # replacement backref `\1` → `\2` to point at the original
        # domain-capture group (now group 2).
        # Needed by clickbench_q29.
        (
            re.compile(
                r"REGEXP_REPLACE\((\w+),\s*"
                r"'\^https\?://\(\?:www\\\.\)\?\(\[\^/\]\+\)/\.\*\$',\s*"
                r"'\\1'\)"
            ),
            r"REGEXP_REPLACE(\1, '^https?://(www\.)?([^/]+)/.*$', '\\2')",
        ),
    ],
    "fabric": [
        # T-SQL has no STDDEV_SAMP / STDDEV_POP; the sample/population stddev
        # functions are STDEV / STDEVP. Needed by tpcds_q17/q39.
        (re.compile(r"\bSTDDEV_SAMP\s*\(", re.IGNORECASE), "STDEV("),
        (re.compile(r"\bSTDDEV_POP\s*\(", re.IGNORECASE), "STDEVP("),
        # DuckDB STRLEN counts BYTES; T-SQL DATALENGTH is the byte count under
        # the DW's UTF-8 collation. LEN would count characters (and drop
        # trailing spaces): on ClickBench's Cyrillic-heavy URLs that's ~2x off,
        # so clickbench_q28/q29 would execute but fail the value compare.
        (re.compile(r"\bSTRLEN\s*\(", re.IGNORECASE), "DATALENGTH("),
        # NOTE: clickbench query_29.sql is HAND-MAINTAINED — Fabric DW has no
        # REGEXP_REPLACE at all (unlike Redshift/Snowflake, which only need
        # regex-syntax patches), so the domain extraction is rewritten as a
        # derived table of LIKE/SUBSTRING/CHARINDEX CASEs directly in the
        # query file. A regex post-patch can't express that structural
        # rewrite. Re-running `translate` clobbers the file back to
        # REGEXP_REPLACE; validate-sf1 then fails clickbench_q29 with an
        # engine error ("'REGEXP_REPLACE' is not a recognized built-in
        # function name") — restore the file from git history.
        # T-SQL AVG keeps the argument's type exactly like Redshift's (see the
        # redshift patches above): AVG(INT/BIGINT) truncates to an integer and
        # AVG(DECIMAL(p,s)) keeps scale s, while the DuckDB ground truth
        # computes AVG as DOUBLE. FLOAT is T-SQL's float(53) — its double.
        # Needed by tpcds q07/q13/q22/q26/q27/q39/q67 + dsb q07/q13/q17/q22/q26/
        # q27/q39/q67.
        (
            re.compile(r"AVG\(CAST\(([^()]+) AS DECIMAL\(\d+,\s*\d+\)\)\)", re.IGNORECASE),
            r"AVG(CAST(\1 AS FLOAT))",
        ),
        (
            re.compile(r"AVG\((?!CAST\()((?:[^()]|\([^()]*\))+)\)", re.IGNORECASE),
            r"AVG(CAST(\1 AS FLOAT))",
        ),
        # NOTE: the NULLS-ordering problem — sqlglot wraps ORDER BY terms in
        # `CASE WHEN <alias> IS NULL THEN 1 ELSE 0 END, <alias>`, which T-SQL
        # can't resolve when <alias> is a SELECT alias ("Invalid column name")
        # — is handled by `_fixup_tsql_alias_refs`, an AST pass run AFTER
        # these regex patches. It inlines each ORDER BY alias reference to the
        # expression it aliases, preserving the CASE (and thus NULLS placement)
        # rather than stripping it. Covers tpcds_q03/q12/q43/q52/q86 and similar.
        # T-SQL ROUND requires a length arg; DuckDB allows 1-arg ROUND(x). Add
        # ", 0" to single-arg ROUND calls. The inner alternation allows one
        # level of nested parens in the argument, and the top-level [^(),] guard
        # means a 2-arg ROUND(x, n) won't match (its top-level comma stops it).
        # Needed by tpcds_q54.
        (re.compile(r"ROUND\(((?:[^(),]|\([^()]*\))*)\)"), r"ROUND(\1, 0)"),
        # `catalog` is a T-SQL reserved word; sqlglot leaves it unquoted as a
        # derived-table alias, so `catalog.<col>` fails to bind. Bracket the
        # alias definition and its references. (`\bcatalog\.`/`AS catalog\b`
        # don't touch the `catalog_sales`/`catalog_returns` tables — no dot /
        # word boundary there.) Needed by tpcds_q49.
        # IGNORECASE: the AST round-trip renders the alias definition uppercase
        # (`AS CATALOG`) while the references stay lowercase (`catalog.col`).
        (re.compile(r"\bAS catalog\b", re.IGNORECASE), "AS [catalog]"),
        (re.compile(r"\bcatalog\.", re.IGNORECASE), "[catalog]."),
        # DuckDB date arithmetic `<d_date> +/- <n>` -> T-SQL DATEADD(day, ...).
        # sqlglot leaves the add/sub and only suggests DATEADD in a trailing
        # comment, which the ORDER BY AST round-trip drops, so match off the
        # date column name directly and consume the now-redundant comment. The
        # `\b` after d_date avoids the integer column `d_date_sk`. Needed by
        # tpcds_q72.
        (
            re.compile(r"([\w.]*\bd_date\b)\s*\+\s*(\d+)(?:\s*/\* SQL Server:[^*]*\*/)?"),
            r"DATEADD(day, \2, \1)",
        ),
        (
            re.compile(r"([\w.]*\bd_date\b)\s*-\s*(\d+)(?:\s*/\* SQL Server:[^*]*\*/)?"),
            r"DATEADD(day, -\2, \1)",
        ),
        # T-SQL has no INTERVAL literal arithmetic; convert `<expr> +/- INTERVAL
        # 'N' DAY` to DATEADD. Needed by dsb_q101/q102.
        (
            re.compile(r"([\w.]+)\s*\+\s*INTERVAL '(\d+)' DAY", re.IGNORECASE),
            r"DATEADD(day, \2, \1)",
        ),
        (
            re.compile(r"([\w.]+)\s*-\s*INTERVAL '(\d+)' DAY", re.IGNORECASE),
            r"DATEADD(day, -\2, \1)",
        ),
    ],
    "clickhouse": [
        # STRLEN is DuckDB-specific; ClickHouse uses length(). Needed by
        # clickbench_q28/q29.
        (re.compile(r"\bSTRLEN\s*\(", re.IGNORECASE), "length("),
        # ClickHouse INTERVAL literals don't quote the number:
        # DuckDB: ``INTERVAL '90' DAY`` -> ClickHouse: ``INTERVAL 90 DAY``.
        # Needed by dsb_q101/q102.
        (
            re.compile(r"INTERVAL\s+'(\d+)'\s+(DAY|MONTH|YEAR|HOUR|MINUTE|SECOND)", re.IGNORECASE),
            r"INTERVAL \1 \2",
        ),
        # The Estuary materialize-clickhouse:v1 connector loads
        # Decimal(P,S) source columns as Nullable(Float64) — same constraint
        # the MotherDuck materializer has (no DECIMAL target). The canonical
        # TPC-DS queries CAST literals and intermediate values to
        # Decimal(P,S); ClickHouse's strict type model refuses to find a
        # supertype between Float64 and Decimal ("no supertype for Float64,
        # Decimal(7, 2) — no lossless conversion"). Rewrite Decimal-cast
        # destinations to Float64 to match the loaded types. Needed by
        # tpcds_q05/q18/q49/q61/q75/q90.
        (
            re.compile(r"\bNullable\(\s*Decimal\s*\(\s*\d+\s*,\s*\d+\s*\)\s*\)"),
            "Nullable(Float64)",
        ),
        (
            re.compile(r"\bDecimal\s*\(\s*\d+\s*,\s*\d+\s*\)"),
            "Float64",
        ),
        # `catalog` is treated as an identifier ClickHouse round-trips to
        # UPPERCASE on alias definitions, while the references in the
        # SELECT/WHERE stay lowercase — so ``FROM (...) AS CATALOG`` +
        # ``catalog.item`` fails to bind. Mirror the Fabric ``[catalog]``
        # patch using ClickHouse backtick-quoting (case-preserving identifier).
        # The ``\bAS catalog\b`` / ``\bcatalog\.`` word boundary leaves
        # ``catalog_sales``/``catalog_returns`` table names alone. Needed by
        # tpcds_q49.
        (re.compile(r"\bAS catalog\b", re.IGNORECASE), "AS `catalog`"),
        (re.compile(r"\bcatalog\.", re.IGNORECASE), "`catalog`."),
    ],
}


def _apply_post_translate_patches(engine: str, sql: str) -> str:
    """Apply per-engine post-translation patches for known sqlglot gaps."""
    for pattern, replacement in _POST_TRANSLATE_PATCHES.get(engine, []):
        sql = pattern.sub(replacement, sql)
    return sql


def _fixup_tsql_alias_refs(sql: str) -> str:
    """Resolve SELECT-alias / ordinal references in ORDER BY and GROUP BY (T-SQL).

    DuckDB/standard SQL lets ORDER BY and GROUP BY reference SELECT aliases (and,
    for GROUP BY, ordinal positions); T-SQL allows neither inside expressions.
    This AST pass fixes the cases sqlglot leaves behind:

    * ORDER BY: sqlglot renders NULLS ordering as
      ``CASE WHEN <alias> IS NULL THEN 1 ELSE 0 END, <alias>``. T-SQL can't
      resolve the alias INSIDE the CASE (only as a bare ORDER BY term), so we
      inline alias refs that appear *within an expression* term to the aliased
      expression (``brand_id`` -> ``item.i_brand_id``), preserving the CASE and
      thus NULLS placement. Bare-alias terms are left as-is (T-SQL allows them).
      Under SELECT DISTINCT, ORDER BY items must be selected, so the CASE term
      can't appear at all — those terms are dropped, leaving the bare alias.
    * GROUP BY: ``GROUP BY <alias>`` and ``GROUP BY <n>`` (ordinal) both fail in
      T-SQL — we inline the alias to its expression and expand the ordinal to
      the corresponding selected expression.

    Scoped per SELECT (nested subqueries/CTEs use their own projections). Returns
    the regenerated SQL only if something changed, so untouched queries come back
    byte-for-byte (no reformatting / no regression risk). Falls back to the input
    if it can't be parsed as T-SQL.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="tsql")
    except Exception:
        logger.warning("tsql alias fix-up: parse failed; leaving SQL unchanged")
        return sql
    if tree is None:
        return sql

    changed = False
    for select in tree.find_all(exp.Select):
        projections = list(select.expressions)
        alias_map = {proj.alias: proj.this for proj in projections if isinstance(proj, exp.Alias)}

        # --- ORDER BY ---
        order = select.args.get("order")
        if order is not None:
            if select.args.get("distinct") is not None:
                # DISTINCT: ORDER BY must be a selected item, so drop the
                # null-ordering CASE terms, leaving the bare alias terms.
                kept = [o for o in order.expressions if not isinstance(o.this, exp.Case)]
                if len(kept) != len(order.expressions):
                    select.set("order", None) if not kept else order.set("expressions", kept)
                    changed = True
            elif alias_map:
                # Inline alias refs that sit INSIDE an expression term (e.g. the
                # null-ordering CASE); bare-alias terms are valid T-SQL, leave them.
                for ordered in order.expressions:
                    if isinstance(ordered.this, exp.Column):
                        continue  # bare alias/column term — allowed in T-SQL ORDER BY
                    for col in list(ordered.find_all(exp.Column)):
                        if col.table:
                            continue
                        target = alias_map.get(col.name)
                        if target is not None:
                            col.replace(target.copy())
                            changed = True

        # --- GROUP BY ---  (inline aliases, expand ordinals, drop constant keys)
        group = select.args.get("group")
        if group is not None and projections:
            new_keys: list[exp.Expression] = []
            group_changed = False
            for key in group.expressions:
                if isinstance(key, exp.Literal) and key.is_int:
                    idx = int(key.name) - 1
                    if 0 <= idx < len(projections):
                        proj = projections[idx]
                        expr = proj.this if isinstance(proj, exp.Alias) else proj
                        group_changed = True
                        # Grouping by a constant projection is a no-op and T-SQL
                        # rejects it (ClickBench `SELECT 1, ... GROUP BY 1`); drop it.
                        if not isinstance(expr, exp.Literal):
                            new_keys.append(expr.copy())
                        continue
                if isinstance(key, exp.Column) and not key.table and key.name in alias_map:
                    new_keys.append(alias_map[key.name].copy())  # GROUP BY <alias>
                    group_changed = True
                    continue
                new_keys.append(key)
            if group_changed:
                if new_keys:
                    group.set("expressions", new_keys)
                else:
                    select.set("group", None)
                changed = True

    return tree.sql(dialect="tsql", pretty=True) if changed else sql


def _fixup_clickhouse_self_join_aliases(sql: str) -> str:
    """Add explicit ``AS <col>`` aliases to ``<table>.<col>``-form projections
    in SELECTs whose FROM clause has the same table aliased twice or more
    (i.e., self-joins).

    ClickHouse names a SELECT's output column using the **fully qualified**
    reference (``v1.d_year``) when the FROM clause contains the same table
    name aliased multiple times — this prevents ambiguity between
    ``v1.d_year``, ``v1_lag.d_year``, ``v1_lead.d_year`` in scope. Standard
    SQL (DuckDB, Postgres) drops the prefix and uses the unqualified column
    name (``d_year``). When a downstream SELECT does
    ``WHERE d_year = 1999`` against such a CTE, it fails to resolve
    ``d_year`` on ClickHouse because the column is actually named
    ``v1.d_year``. Adding ``AS d_year`` on the projection fixes the output
    column name.

    Scoped per SELECT; only applies when the SELECT's FROM clause has at
    least one repeated table source (so we don't churn every projection in
    every query). Returns regenerated SQL only if something changed. Falls
    back to the input on parse failure. Needed by tpcds_q47/q57 and their DSB
    twins.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="clickhouse")
    except Exception:
        logger.warning("clickhouse self-join alias fix-up: parse failed; leaving SQL unchanged")
        return sql
    if tree is None:
        return sql

    changed = False
    for select in tree.find_all(exp.Select):
        # sqlglot 26+ stores the FROM-clause under the arg key ``from_``
        # (trailing underscore to avoid shadowing the Python keyword).
        # ``select.args.get("from")`` would silently return None.
        from_clause = select.args.get("from_")
        joins = select.args.get("joins") or []
        if from_clause is None:
            continue
        # Collect every table source referenced by this SELECT's FROM + JOINs.
        sources: list[exp.Table] = []
        for source in [from_clause.this, *(j.this for j in joins)]:
            if isinstance(source, exp.Table):
                sources.append(source)
        # A "self-join" for our purposes: the same underlying table.name
        # appears in two or more sources. Aliased subqueries are skipped.
        names = [t.name for t in sources if t.name]
        repeated = {n for n in names if names.count(n) > 1}
        if not repeated:
            continue

        # First pass: figure out which target alias names would COLLIDE if
        # we added them — happens in queries like ``SELECT item1.i_item_sk,
        # item2.i_item_sk FROM item AS item1, item AS item2`` (real case in
        # dsb_q100). Adding ``AS i_item_sk`` to both produces a
        # duplicate-alias error. Skip the rewrite for those names; the
        # downstream consumer already has to use ``table.col``-form refs.
        prospective: dict[str, int] = {}
        for projection in select.expressions:
            if isinstance(projection, exp.Alias):
                prospective[projection.alias] = prospective.get(projection.alias, 0) + 1
                continue
            if isinstance(projection, exp.Column) and projection.table and projection.name:
                prospective[projection.name] = prospective.get(projection.name, 0) + 1

        for projection in list(select.expressions):
            if isinstance(projection, exp.Alias):
                continue
            if not isinstance(projection, exp.Column):
                continue
            if not projection.table or not projection.name:
                continue
            if prospective.get(projection.name, 0) > 1:
                continue  # would collide; leave as-is
            # Replace the bare Column with ``Column AS Column.name``.
            new_proj = exp.alias_(projection.copy(), projection.name)
            projection.replace(new_proj)
            changed = True

    return tree.sql(dialect="clickhouse", pretty=True) if changed else sql


def translate_one(sql: str, target_dialect: str) -> tuple[str | None, str | None]:
    """Translate a single statement from DuckDB to a target dialect.

    Returns (translated_sql, error_message) where exactly one is non-None.
    Output is pretty-printed so committed per-engine SQL stays diff-friendly.
    Note: this does NOT apply ``_POST_TRANSLATE_PATCHES``; that runs in
    ``translate_all`` where the engine name (not just dialect) is known.
    """
    try:
        parsed = sqlglot.parse_one(sql, dialect="duckdb")
        out = parsed.sql(dialect=target_dialect, pretty=True)
        return out, None
    except Exception as e:
        return None, str(e)


def translate_all(
    engines: list[str] | None = None,
    *,
    write_files: bool = True,
    collect: dict[tuple[str, str], str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Translate every canonical query to each requested engine dialect.

    When ``write_files`` is True (default) writes:
      - queries/<engine>/query_NN.sql for every successful translation
      - results/translation_matrix.json with per-query status per engine
    When False, nothing is written — used by ``validate-dialects`` to compute
    per-engine success rates without overwriting the committed SQL.

    ``collect``, when given, is populated with ``(engine, query_id) -> final SQL``
    for every successful translation. This exposes the pipeline's real output —
    sqlglot's transpilation *plus* the per-engine AST and regex passes — without
    touching the committed files, so a test can fingerprint it and catch a
    dependency upgrade that silently changes what we would generate.
    """
    engines = engines or list(ENGINE_DIALECTS)
    registry = QueryRegistry()
    queries = registry.all()

    matrix: dict[str, dict[str, Any]] = {}
    success_counts: dict[str, int] = {e: 0 for e in engines}

    for q in queries:
        matrix[q.id] = {"block": q.block, "number": q.number, "engines": {}}
        for engine in engines:
            dialect = ENGINE_DIALECTS[engine]

            if engine == "motherduck":
                # DuckDB -> DuckDB: still parse to validate, but write the
                # original SQL verbatim if parse succeeds.
                _, parse_err = translate_one(q.sql, dialect)
                if parse_err:
                    matrix[q.id]["engines"][engine] = {"status": "error", "error": parse_err[:200]}
                    continue
                if write_files:
                    _write_engine_sql(engine, q.block, q.number, q.sql)
                if collect is not None:
                    collect[(engine, q.id)] = q.sql
                matrix[q.id]["engines"][engine] = {"status": "success", "error": None}
                success_counts[engine] += 1
                continue

            translated, err = translate_one(q.sql, dialect)
            if err is not None or translated is None:
                matrix[q.id]["engines"][engine] = {"status": "error", "error": (err or "")[:200]}
                logger.warning(
                    "translation failed",
                    extra={"query_id": q.id, "platform": engine},
                )
                continue
            if engine == "fabric":
                # Resolve T-SQL ORDER BY / GROUP BY alias + ordinal refs at the
                # AST level FIRST (inline alias refs / expand ordinals / drop the
                # null-ordering CASE under SELECT DISTINCT). Doing it before the
                # regex patches means the patches (catalog brackets, etc.) are the
                # final textual step and survive — the AST round-trip would
                # otherwise regenerate them away.
                translated = _fixup_tsql_alias_refs(translated)
            elif engine == "clickhouse":
                # Self-join CTEs: explicitly alias ``<table>.<col>`` projections
                # so ClickHouse uses the unqualified column name in the CTE's
                # output schema. AST pass runs BEFORE the regex patches so the
                # Decimal-cast and catalog-backtick patches survive the AST
                # round-trip (which would otherwise undo them).
                translated = _fixup_clickhouse_self_join_aliases(translated)
            translated = _apply_post_translate_patches(engine, translated)
            if write_files:
                _write_engine_sql(engine, q.block, q.number, translated)
            if collect is not None:
                collect[(engine, q.id)] = translated
            matrix[q.id]["engines"][engine] = {"status": "success", "error": None}
            success_counts[engine] += 1

    total = len(queries)
    for engine in engines:
        rate = 100.0 * success_counts[engine] / max(total, 1)
        logger.info(
            "translation summary",
            extra={
                "platform": engine,
                "success_count": success_counts[engine],
                "total": total,
                "rate_pct": rate,
            },
        )

    if write_files:
        out_path = QUERIES_ROOT.parent / "results" / "translation_matrix.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(matrix, f, indent=2)
        logger.info("translation matrix written to %s", out_path)
    return matrix


def dialect_success_rates(matrix: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Per-engine translation success rate from a ``translate_all`` matrix.

    Returns ``{engine: {"ok", "total", "rate"}}``, the data behind the
    ``validate-dialects`` gate (sqlglot translation success per engine).
    """
    rates: dict[str, dict[str, float]] = {}
    for entry in matrix.values():
        for engine, status in entry.get("engines", {}).items():
            r = rates.setdefault(engine, {"ok": 0, "total": 0})
            r["total"] += 1
            if status.get("status") == "success":
                r["ok"] += 1
    for r in rates.values():
        r["rate"] = r["ok"] / r["total"] if r["total"] else 0.0
    return rates


def _write_engine_sql(engine: str, block: str, number: int, sql: str) -> None:
    target_dir = QUERIES_ROOT / engine / block
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"query_{number:02d}.sql" if number < 100 else f"query_{number}.sql"
    (target_dir / name).write_text(sql.rstrip() + "\n", encoding="utf-8")

# Benchmark SQL Queries (`queries/`)

244 benchmark queries across 3 blocks, stored per-engine dialect. The files under `<engine>/` are what the benchmark executes.

## Structure

```
queries/
├── duckdb/              ← Canonical source of truth
│   ├── tpcds/           99 queries (query_01.sql – query_99.sql)
│   ├── dsb/             102 queries (query_01.sql – query_102.sql)
│   └── clickbench/      43 queries (query_01.sql – query_43.sql)
├── <engine>/            ← Per-engine SQL (snowflake, databricks, bigquery,
│   │                      redshift, fabric, clickhouse, motherduck)
│   ├── tpcds/           `benchmark translate` output + per-engine correctness patches
│   ├── dsb/             same
│   └── clickbench/      the ClickBench project's query file for that engine
│                        (Fabric: `benchmark translate` output)
└── physical_design.json ← Clustering/distribution/partition spec per engine (not applied)
```

## Where each file comes from

- **`duckdb/tpcds/`**: the 99 queries of DuckDB's `tpcds` extension (`scripts/data/extract_tpcds_queries.py`).
- **`duckdb/dsb/`**: TPC-DS queries 1–99 plus the 3 DSB-specific queries 100–102 (`scripts/data/build_dsb_queries.py`, run by `benchmark generate-dsb`).
- **`duckdb/clickbench/`**: the ClickBench project's DuckDB `queries.sql`, one statement per file (`datagen/clickbench.py:download_clickbench_queries` fetches it; no CLI command wraps it).
- **`<engine>/tpcds/`, `<engine>/dsb/`**: written by the translator (`src/benchmark/translator.py`): canonical DuckDB SQL is parsed, re-emitted in the target dialect, and post-processed by per-engine regex patches in `_POST_TRANSLATE_PATCHES` and two AST passes (T-SQL alias/ordinal resolution, ClickHouse self-join aliasing) for the cases sqlglot doesn't handle (function aliases, `INTERVAL` literal syntax, NULL ordering, reserved-word collisions, etc.). About 50 of the committed files (33 on Fabric, the rest on ClickHouse, Databricks, Redshift and Snowflake) differ from what the translator produces today, chiefly hand-applied correctness patches such as a `ROUND` or an explicit cast. MotherDuck runs the canonical SQL unchanged.
- **`<engine>/clickbench/`**: for Snowflake, Databricks, BigQuery, Redshift, ClickHouse and MotherDuck, the ClickBench project's own `queries.sql` for that engine, imported verbatim with only the table reference rewritten to `hits` (MotherDuck's is DuckDB's, identical to the canonical files). These are trusted, not result-compared (`docs/ARCHITECTURE.md` §6). Fabric has no ClickBench variant, so its files are translator output, with `query_29.sql` maintained by hand (see the note in `src/benchmark/translator.py`); Fabric's ClickBench is the only one validated against the DuckDB ground truth.

## Query Blocks

| Block | Source | Count | Schema | Notes |
|-------|--------|-------|--------|-------|
| TPC-DS | DuckDB `tpcds` extension | 99 | 24 tables, retail/e-commerce | Uniform data distribution |
| DSB | TPC-DS queries 1–99 + 3 custom (100–102) | 102 | Same 24 tables | Skewed data distribution |
| ClickBench | ClickBench project (`queries.sql` per engine) | 43 | Single `hits` table | ~100 GB single scale |

## Translation Pipeline

1. **DuckDB canonical**: all queries written/validated in DuckDB SQL first
2. **sqlglot translation**: automated dialect conversion to each engine (TPC-DS, DSB, and Fabric's ClickBench)
3. **Correctness patches**: per-engine fixes where a translation diverges from DuckDB's semantics (types, rounding, aggregates), never tuning. The optional Claude optimization pass (`benchmark optimize`) is not applied to any engine.
4. **SF1 validation**: `benchmark validate-sf1` checks correctness against the DuckDB ground truth

## Translation Status

| Engine | TPC-DS | DSB | ClickBench | Notes |
|---|---|---|---|---|
| DuckDB (canonical) | 99 | 102 | 43 | Source of truth |
| Snowflake | 99 | 102 | 43 | ClickBench from the ClickBench project |
| Databricks | 99 | 102 | 43 | ClickBench from the ClickBench project |
| BigQuery | 99 | 102 | 43 | ClickBench from the ClickBench project |
| Redshift | 99 | 102 | 43 | ClickBench from the ClickBench project |
| Fabric | 99 | 102 | 43 | All translated; hand-maintained ClickBench q29 |
| ClickHouse | 99 | 102 | 43 | ClickBench from the ClickBench project |
| MotherDuck | 99 | 102 | 43 | DuckDB-native; canonical SQL unchanged |

## Re-translating

```bash
uv run benchmark translate --platforms snowflake   # one engine, a comma list or all; default motherduck
git diff queries/snowflake/                        # review before keeping anything
```

`--platforms` takes engine names or `all` (the seven engines). `translate` rewrites every block for each engine it is given, ClickBench included, and also writes `results/translation_matrix.json`. It therefore overwrites committed SQL it would not regenerate: the 43 imported ClickBench files on each of Snowflake, Databricks, BigQuery, Redshift and ClickHouse, Fabric's hand-maintained ClickBench `query_29.sql`, and the hand-patched TPC-DS/DSB files above. Translate one named engine at a time, review the diff, and restore the committed versions of those files. `benchmark validate-dialects` runs the same translation in memory without writing anything.

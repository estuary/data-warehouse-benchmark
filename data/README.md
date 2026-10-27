# Benchmark Datasets (`data/`)

Generated/downloaded benchmark data and row-count manifests. Only the two
`expected_counts.json` manifests are tracked; the Parquet is generated or
downloaded on demand.

## Structure

```
data/
├── tpcds/
│   ├── expected_counts.json    Row counts for 24 TPC-DS tables at SF1-SF1000 (~19.6M rows at SF1)
│   └── sf<N>/                  Generated Parquet files (via `uv run benchmark generate-tpcds`)
├── dsb/
│   ├── expected_counts.json    Row counts for 24 DSB tables at SF1-SF1000 (~20.1M rows at SF1)
│   └── sf<N>/                  Generated Parquet files (dsdgen `.dat` → Parquet, via `uv run benchmark generate-dsb`)
└── (clickbench/)               hits.parquet.raw (~14 GiB download) and the typed hits.parquet derived from it
```

## Data Generation Commands

```bash
uv run benchmark generate-tpcds --scales 1        # DuckDB tpcds extension → Parquet (+ expected_counts.json)
uv run benchmark generate-dsb --scales 1          # microsoft/dsb dsdgen (native or Docker) → Parquet (+ expected_counts.json)
uv run benchmark download-clickbench              # resumable HTTP download → hits.parquet.raw
uv run benchmark derive-clickbench-typed          # raw → typed hits.parquet (what is staged and loaded)
uv run benchmark stage-data                       # Upload Parquet to S3 (--suites to scope)
uv run benchmark generate-expected-counts         # Merge row counts from Parquet metadata into a manifest
uv run benchmark generate-checksums               # SHA-256 manifest
```

`generate-tpcds` and `generate-dsb` skip a scale whose Parquet already exists
unless `--force`. `generate-expected-counts` reads `data/tpcds` by default
(`--data-dir` / `--output` for DSB), or the staged S3 footers with
`--from-s3 --suite <s> --scales <N>`; it merges, so other scales are kept.

## Scale Factors

| SF | Size |
|----|------|
| SF1 | ~1 GB |
| SF10 | ~10 GB |
| SF100 | ~100 GB |
| SF1000 | ~1 TB |

Parquet is generated on demand; the `expected_counts.json` manifests cover every
scale. SF1000 is the maximum scale.

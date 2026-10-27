"""The browser explorer's contract with the analysis exports.

`viz/` reads the Parquet files `benchmark analyze` writes to `results/exports/`,
and its pages read columns by name from the query rows. A column that the
analysis drops or renames therefore comes back `undefined` in the browser and
renders as a blank or "NaN" instead of failing, and a type change can break the
page's arithmetic (DuckDB hands a BIGINT to JavaScript as a BigInt, which throws
when mixed with a number).

`viz/scripts/export-schema.json` records every export's columns and types as the
explorer was written against them. The explorer's build (`npm run build`, via
`viz/scripts/prepare-data.mjs`) refuses to build when they drift, but it only
runs on a push to `main` or a preview branch. This test holds the same file to
the committed exports on every pull request, where an analysis change lands.

A new column is not a failure: it is recorded once the explorer reads it
(`cd viz && npm run data -- --write-schema`).
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

EXPORTS = Path("results/exports")
CONTRACT = Path("viz/scripts/export-schema.json")

#: First bytes of a Git LFS pointer file (the exports ship via LFS).
LFS_POINTER_PREFIX = b"version https://git-lfs"

#: Export file -> {column: DuckDB type}, as the explorer reads them.
EXPECTED: dict[str, dict[str, str]] = json.loads(CONTRACT.read_text())


def _columns(path: Path) -> dict[str, str]:
    """Column name -> DuckDB type of one Parquet file."""
    rows = duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
    return {name: column_type for name, column_type, *_ in rows}


@pytest.mark.parametrize("export", sorted(EXPECTED))
def test_export_keeps_the_columns_the_explorer_reads(export: str) -> None:
    """Every column the explorer was written against is still there, with its type."""
    path = EXPORTS / export
    assert path.exists(), (
        f"{path} is missing: the explorer loads it (regenerate with `uv run benchmark analyze`)"
    )
    with path.open("rb") as fh:
        if fh.read(len(LFS_POINTER_PREFIX)) == LFS_POINTER_PREFIX:
            pytest.skip(f"{path} is an unfetched Git LFS pointer; run `git lfs pull`")

    actual = _columns(path)
    broken = [
        f"{column} ({column_type}) is gone"
        if column not in actual
        else f"{column} is {actual[column]}, was {column_type}"
        for column, column_type in EXPECTED[export].items()
        if actual.get(column) != column_type
    ]
    assert not broken, (
        f"{export} no longer matches the columns the explorer reads ({CONTRACT}): "
        + "; ".join(broken)
        + ". If the change is intended, update the viz/ pages that read these columns, "
        "then record it: cd viz && npm run data -- --write-schema"
    )

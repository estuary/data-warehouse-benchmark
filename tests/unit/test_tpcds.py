"""TPC-DS generation: idempotent skip-if-present (+ shared existing-counts helper).

Generation via the DuckDB ``tpcds`` extension is exercised by the integration
suite; here we cover the cheap harness logic — that a complete scale dir is
detected and regeneration is skipped (no DuckDB call) unless forced.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from benchmark.datagen import tpcds


class _ReachedError(Exception):
    """Raised by the stubbed duckdb.connect to prove generation was attempted."""


def _raise(*_a, **_k):
    raise _ReachedError()


def _write_all_parquets(directory: Path) -> None:
    for table in tpcds.TPCDS_TABLES:
        pq.write_table(pa.table({"x": [1]}), directory / f"{table}.parquet")


def test_existing_table_counts_complete_vs_incomplete(tmp_path: Path):
    assert tpcds.existing_table_counts(tmp_path) is None  # empty dir
    _write_all_parquets(tmp_path)
    counts = tpcds.existing_table_counts(tmp_path)
    assert counts is not None and set(counts) == set(tpcds.TPCDS_TABLES)
    (tmp_path / "store.parquet").unlink()  # missing one table -> incomplete
    assert tpcds.existing_table_counts(tmp_path) is None


def test_generate_skips_when_data_present(tmp_path: Path, monkeypatch):
    _write_all_parquets(tmp_path)
    # If generation were attempted it would hit duckdb.connect (and raise).
    monkeypatch.setattr(tpcds.duckdb, "connect", _raise)

    counts = tpcds.generate_tpcds_sf(1, tmp_path)

    assert set(counts) == set(tpcds.TPCDS_TABLES)  # returned from footers, no gen


def test_force_bypasses_skip(tmp_path: Path, monkeypatch):
    _write_all_parquets(tmp_path)
    monkeypatch.setattr(tpcds.duckdb, "connect", _raise)

    with pytest.raises(_ReachedError):  # force -> proceeds to generate
        tpcds.generate_tpcds_sf(1, tmp_path, force=True)

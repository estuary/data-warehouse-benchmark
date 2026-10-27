"""ClickBench derive: idempotent skip-if-present, force, missing-input.

The real derive is a ~14 GiB DuckDB COPY (exercised by the integration suite);
here we cover the cheap control flow with DuckDB stubbed — skip when a readable
typed parquet exists, --force bypasses the skip, missing raw input raises.
There is no sampling: a sample misrepresents the corpus.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import benchmark.datagen.clickbench as cb


class _ReachedError(Exception):
    """Raised by the stubbed duckdb.connect to prove the derive was attempted."""


def _raise(*_a, **_k):
    raise _ReachedError()


def _write_typed(path, rows: int = 3) -> None:
    pq.write_table(pa.table({"x": list(range(rows))}), path)


def test_derive_skips_when_typed_present(tmp_path, monkeypatch):
    out = tmp_path / "hits.parquet"
    _write_typed(out, rows=3)
    # If the derive ran it would hit duckdb.connect (and raise).
    monkeypatch.setattr(cb.duckdb, "connect", _raise)

    rows = cb.derive_typed_parquet(input_path=tmp_path / "raw.parquet", output_path=out)

    assert rows == 3  # returned from the footer, no derive, raw not even needed


def test_derive_force_reruns(tmp_path, monkeypatch):
    out = tmp_path / "hits.parquet"
    _write_typed(out)
    raw = tmp_path / "raw.parquet"
    raw.write_bytes(b"placeholder")  # exists so the input check passes
    monkeypatch.setattr(cb.duckdb, "connect", _raise)

    with pytest.raises(_ReachedError):  # force -> bypasses skip, attempts derive
        cb.derive_typed_parquet(input_path=raw, output_path=out, force=True)


def test_derive_missing_raw_input_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        cb.derive_typed_parquet(
            input_path=tmp_path / "absent.parquet.raw",
            output_path=tmp_path / "hits.parquet",
        )


def test_derive_signature_has_no_sampling():
    # No sampling (a sample misrepresents the corpus); guard against it creeping in.
    import inspect

    params = set(inspect.signature(cb.derive_typed_parquet).parameters)
    assert "sample_percent" not in params and "sample_seed" not in params

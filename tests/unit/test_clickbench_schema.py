"""ClickBench collection schema: persist-once, then resolve without the parquet.

`free_local_copy_after_stage` deletes the typed `hits.parquet` after staging, so
the capture can't keep introspecting it. `persist_clickbench_schema` commits the
schema to JSON once; `_build_clickbench_schema` then prefers that JSON, keeping
the capture define-once (no churn / dropped ClickBench binding on republish).
"""

from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq

import benchmark.datagen.collection_schemas as cs


def _write_hits(path) -> None:
    """A tiny ClickBench-shaped parquet: one of each interesting column type."""
    tbl = pa.table(
        {
            "WatchID": pa.array([1, 2], type=pa.int64()),
            "EventDate": pa.array([0, 1], type=pa.date32()),
            "EventTime": pa.array(
                ["2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z"], type=pa.string()
            ),
            "Title": pa.array(["a", "b"], type=pa.string()),
        }
    )
    pq.write_table(tbl, path)


def test_persist_writes_typed_schema_json(tmp_path):
    parquet = tmp_path / "hits.parquet"
    out = tmp_path / "clickbench_hits.schema.json"
    _write_hits(parquet)

    cs.persist_clickbench_schema(parquet_path=parquet, output=out)

    assert out.exists()
    data = json.loads(out.read_text())
    assert data["key"] == ["/_meta/file", "/_meta/offset"]
    props = data["schema"]["properties"]
    assert "_meta" in props
    # Temporal overrides applied: EventDate -> date, EventTime -> date-time.
    assert props["EventDate"]["format"] == "date"
    assert props["EventTime"]["format"] == "date-time"
    # Integers carry the integer format hint.
    assert "integer" in props["WatchID"]["type"]


def test_build_prefers_committed_json_even_without_parquet(tmp_path, monkeypatch):
    # The define-once payoff: JSON present, parquet GONE (freed post-stage).
    out = tmp_path / "clickbench_hits.schema.json"
    parquet = tmp_path / "hits.parquet"
    _write_hits(parquet)
    cs.persist_clickbench_schema(parquet_path=parquet, output=out)
    parquet.unlink()  # simulate free_local_copy_after_stage

    monkeypatch.setattr(cs, "CLICKBENCH_SCHEMA_JSON", out)
    monkeypatch.setattr(cs, "CLICKBENCH_PARQUET", parquet)

    schema = cs._build_clickbench_schema()
    assert schema is not None
    assert schema["schema"]["properties"]["EventTime"]["format"] == "date-time"


def test_build_introspects_parquet_when_no_json(tmp_path, monkeypatch):
    parquet = tmp_path / "hits.parquet"
    _write_hits(parquet)
    monkeypatch.setattr(cs, "CLICKBENCH_SCHEMA_JSON", tmp_path / "absent.json")
    monkeypatch.setattr(cs, "CLICKBENCH_PARQUET", parquet)

    schema = cs._build_clickbench_schema()
    assert schema is not None
    assert schema["schema"]["properties"]["EventDate"]["format"] == "date"


def test_build_returns_none_when_neither_present(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CLICKBENCH_SCHEMA_JSON", tmp_path / "absent.json")
    monkeypatch.setattr(cs, "CLICKBENCH_PARQUET", tmp_path / "absent.parquet")
    assert cs._build_clickbench_schema() is None

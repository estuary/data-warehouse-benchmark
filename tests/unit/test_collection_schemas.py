"""build_collection_schemas: builds from the DuckDB tpcds-extension schema.

Table names and types come from tpcds_table_schema(). The extension is mocked
here so the test is hermetic; it verifies the per-scale suite fan-out and the
DuckDB-type -> JSON-schema mapping.
"""

from __future__ import annotations

import benchmark.datagen.collection_schemas as cs


def test_build_collection_schemas_from_extension(monkeypatch):
    # Mock the authoritative schema (no DuckDB tpcds extension needed) and omit
    # ClickBench so the assertion math is just TPC-DS + DSB.
    monkeypatch.setattr(
        cs,
        "tpcds_table_schema",
        lambda: {
            t: (("id_col", "BIGINT"), ("amt", "DECIMAL(7,2)"), ("d", "DATE"), ("name", "VARCHAR"))
            for t in cs.TPCDS_TABLES
        },
    )
    monkeypatch.setattr(cs, "_build_clickbench_schema", lambda: None)

    schemas = cs.build_collection_schemas("Acme", scales=(1, 10))

    # TPC-DS + DSB, each table, each scale (ClickBench omitted via the mock).
    assert len(schemas) == len(cs.TPCDS_TABLES) * 2 * 2
    assert "Acme/benchmark/tpcds/sf1/store_returns" in schemas
    assert "Acme/benchmark/dsb/sf10/store_returns" in schemas

    props = schemas["Acme/benchmark/tpcds/sf1/store_returns"]["schema"]["properties"]
    assert "_meta" in props
    assert props["id_col"]["type"] == ["integer", "null"]  # BIGINT
    assert props["amt"]["format"] == "number"  # DECIMAL(p,s) -> string + number
    assert props["d"]["format"] == "date"  # DATE
    assert props["name"]["type"] == ["string", "null"]  # VARCHAR


def test_build_collection_schemas_raises_on_missing_table(monkeypatch):
    # If the extension schema is missing a table, fail loudly rather than emit a
    # partial capture.
    monkeypatch.setattr(cs, "tpcds_table_schema", lambda: {"store_returns": (("a", "BIGINT"),)})
    monkeypatch.setattr(cs, "_build_clickbench_schema", lambda: None)

    import pytest

    with pytest.raises(RuntimeError, match="missing tables"):
        cs.build_collection_schemas("Acme", scales=(1,))

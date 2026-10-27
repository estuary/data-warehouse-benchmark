"""Generate the JSON Schemas for the 49 tables behind the Estuary collections.

Estuary's materialize-motherduck writes one column per field declared in the
collection's JSON Schema. When the schema only declares ``_meta`` (the
``_passthrough_collection_spec`` default) the materializer falls back to a
single ``flow_document`` JSON-blob column — which breaks every benchmark query
that references native columns like ``ss_sold_date_sk``.

This module produces explicit field-defined schemas:
  - TPC-DS + DSB (24 tables × 2 = 48 collections): same column layout, taken from
    the DuckDB ``tpcds`` extension's authoritative schema
    (``tpcds.tpcds_table_schema`` via ``CALL dsdgen(sf=0)``) — no external DDL
    file. DSB shares the schema; only the data distribution differs.
  - ClickBench (1 collection): introspected from the local
    ``data/clickbench/hits.parquet`` (or the committed schema JSON) via pyarrow.

The output is a mapping of collection name -> JSON Schema dict, suitable for
splicing into the ``collections:`` block of an Estuary catalog YAML.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from .tpcds import tpcds_table_schema

logger = logging.getLogger("benchmark.datagen.collection_schemas")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
CLICKBENCH_PARQUET = REPO_ROOT / "data" / "clickbench" / "hits.parquet"
# Committed ClickBench collection schema. Generated once from the typed parquet
# (``persist_clickbench_schema`` / ``derive-clickbench-typed``) so the capture
# stays define-once: it loads without the parquet, which
# ``free_local_copy_after_stage`` deletes post-staging.
CLICKBENCH_SCHEMA_JSON = REPO_ROOT / "estuary" / "collections" / "clickbench_hits.schema.json"

# 24 TPC-DS tables ship in both tpcds_sf1 and dsb_sf1 datasets.
TPCDS_TABLES: tuple[str, ...] = (
    "call_center",
    "catalog_page",
    "catalog_returns",
    "catalog_sales",
    "customer",
    "customer_address",
    "customer_demographics",
    "date_dim",
    "household_demographics",
    "income_band",
    "inventory",
    "item",
    "promotion",
    "reason",
    "ship_mode",
    "store",
    "store_returns",
    "store_sales",
    "time_dim",
    "warehouse",
    "web_page",
    "web_returns",
    "web_sales",
    "web_site",
)


# ─── SQL type → JSON Schema type ────────────────────────────────────────────


def _sql_type_to_jsonschema(sql_type: str) -> dict[str, Any]:
    """Translate a single SQL type expression to its JSON Schema form.

    Estuary collections are JSON-typed; the materializer maps schema types to
    the engine's native types when creating tables (so JSON ``integer`` becomes
    ``BIGINT`` in DuckDB, JSON ``string`` with format ``date`` becomes ``DATE``,
    etc.). Coverage here is intentionally narrow — only the types used in the
    TPC-DS DDL.
    """
    t = sql_type.strip().lower()
    # Strip the (N) or (N,M) parameter list.
    base = re.match(r"^([a-zA-Z_]+)", t)
    base = base.group(1) if base else t

    if base in ("integer", "int", "bigint", "smallint"):
        # TPC-DS surrogate keys + sequence counters all fit in 64-bit signed,
        # and the parser emits them as native JSON ints. Strict integer makes
        # the materializer pick BIGINT in MotherDuck; a union that also
        # accepts strings gets the oversized HUGEINT.
        return {"type": ["integer", "null"]}
    if base in ("decimal", "numeric"):
        # Parquet preserves DECIMAL precision as strings (JSON has no precise
        # decimal type). ``format: number`` is Estuary's hint that this
        # string is a numeric literal — the materializer creates a DECIMAL
        # column instead of falling back to DOUBLE.
        return {"type": ["string", "null"], "format": "number"}
    if base in ("double", "float", "real"):
        # Floats fit cleanly in JSON number; no string wrapping needed.
        return {"type": ["number", "null"]}
    if base in ("char", "varchar", "text", "string"):
        return {"type": ["string", "null"]}
    if base == "date":
        return {"type": ["string", "null"], "format": "date"}
    if base == "time":
        return {"type": ["string", "null"], "format": "time"}
    if base in ("timestamp", "datetime"):
        return {"type": ["string", "null"], "format": "date-time"}
    if base == "boolean":
        return {"type": ["boolean", "null"]}
    # Fallback for anything we didn't enumerate — keep it parseable, let the
    # materializer pick a sensible type. Logged so we notice in CI.
    logger.warning("unknown SQL type %r; defaulting to JSON 'string'", sql_type)
    return {"type": ["string", "null"]}


# ─── ClickBench hits schema from the parquet itself ────────────────────────


_INT_SCHEMA: dict[str, Any] = {
    "type": ["string", "integer", "null"],
    "format": "integer",
}
_NUM_SCHEMA: dict[str, Any] = {
    "type": ["string", "number", "null"],
    "format": "number",
}

_ARROW_TYPE_MAP: dict[str, dict[str, Any]] = {
    "int8": _INT_SCHEMA,
    "int16": _INT_SCHEMA,
    "int32": _INT_SCHEMA,
    "int64": _INT_SCHEMA,
    "uint8": _INT_SCHEMA,
    "uint16": _INT_SCHEMA,
    "uint32": _INT_SCHEMA,
    "uint64": _INT_SCHEMA,
    "float": _NUM_SCHEMA,
    "double": _NUM_SCHEMA,
    "bool": {"type": ["boolean", "null"]},
    "string": {"type": ["string", "null"]},
    "binary": {"type": ["string", "null"]},
}


def _arrow_type_to_jsonschema(arrow_type: str) -> dict[str, Any]:
    name = arrow_type.lower()
    for prefix, schema in _ARROW_TYPE_MAP.items():
        if name.startswith(prefix):
            return dict(schema)
    # JSON-Schema's `format: date` requires `YYYY-MM-DD`; `format: date-time`
    # requires full RFC 3339. The materializer uses these hints to pick DATE
    # vs TIMESTAMP WITH TIME ZONE columns at the destination.
    if name.startswith("date"):
        return {"type": ["string", "null"], "format": "date"}
    if name.startswith("timestamp"):
        return {"type": ["string", "null"], "format": "date-time"}
    logger.warning("unknown Arrow type %r; defaulting to JSON 'string'", arrow_type)
    return {"type": ["string", "null"]}


# Per-column overrides for ClickBench's hits table. The derived typed parquet
# (produced by `benchmark derive-clickbench-typed`) stores EventTime,
# ClientEventTime, and LocalEventTime as VARCHAR holding RFC 3339 strings
# (because source-s3 serializes parquet Timestamp arrow columns in a format
# Estuary's date-time validator rejects; see ``clickbench.derive_typed_parquet``).
# The arrow-type auto-detection would map these to plain `string`; we need explicit
# `format: date-time` to drive the materializer to TIMESTAMP WITH TIME ZONE.
# EventDate is already Date32 in the parquet, so it's handled by
# _arrow_type_to_jsonschema — but we list it here too for explicit intent.
_CLICKBENCH_COL_OVERRIDES: dict[str, dict[str, Any]] = {
    "EventDate": {"type": ["string", "null"], "format": "date"},
    "EventTime": {"type": ["string", "null"], "format": "date-time"},
    "ClientEventTime": {"type": ["string", "null"], "format": "date-time"},
    "LocalEventTime": {"type": ["string", "null"], "format": "date-time"},
}


def _clickbench_columns(parquet_path: Path = CLICKBENCH_PARQUET) -> list[tuple[str, str]]:
    """Read the hits.parquet header and return [(col, arrow_type), ...]."""
    if not parquet_path.exists():
        raise FileNotFoundError(f"ClickBench parquet missing: {parquet_path}")
    schema = pq.ParquetFile(parquet_path).schema_arrow
    return [(field.name, str(field.type)) for field in schema]


# ─── Public API: collection-name -> JSON Schema mapping ────────────────────


def _schema_with_meta(cols: list[tuple[str, str]], type_mapper) -> dict[str, Any]:
    """Build a Flow collection schema with explicit data columns + the
    ``_meta`` envelope source-s3 emits. The composite key on
    ``/_meta/file`` + ``/_meta/offset`` keeps multi-file collections distinct
    even if the source parquet rows aren't deduped on a primary key.
    """
    properties: dict[str, Any] = {
        "_meta": {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "offset": {"type": "integer"},
            },
            "required": ["file", "offset"],
        },
    }
    for name, sql_type in cols:
        properties[name] = type_mapper(sql_type)
    return {
        "schema": {
            "type": "object",
            "properties": properties,
            "required": ["_meta"],
        },
        "key": ["/_meta/file", "/_meta/offset"],
    }


# ─── ClickBench schema: persist-once, then load without the parquet ─────────


def _introspect_clickbench_schema(parquet_path: Path = CLICKBENCH_PARQUET) -> dict[str, Any]:
    """Build the ClickBench ``hits`` collection schema from the typed parquet.

    Applies the temporal-column overrides: the 3 ``*EventTime`` columns are
    stored as VARCHAR (RFC 3339 strings) in the derived parquet, so plain
    arrow-type mapping would emit ``string`` with no format hint — we force
    ``format: date-time`` so the materializer lands TIMESTAMP WITH TIME ZONE.
    """
    cb_cols = _clickbench_columns(parquet_path)
    cb_schema = _schema_with_meta(cb_cols, _arrow_type_to_jsonschema)
    for col, override in _CLICKBENCH_COL_OVERRIDES.items():
        if col in cb_schema["schema"]["properties"]:
            cb_schema["schema"]["properties"][col] = dict(override)
    return cb_schema


def persist_clickbench_schema(
    parquet_path: Path = CLICKBENCH_PARQUET, output: Path = CLICKBENCH_SCHEMA_JSON
) -> Path:
    """Introspect the typed ClickBench parquet and commit its collection schema
    to ``output`` (JSON). Called by ``derive-clickbench-typed`` so the capture
    stays define-once: once committed the schema loads without the parquet, which
    ``free_local_copy_after_stage`` deletes post-staging.
    """
    cb_schema = _introspect_clickbench_schema(parquet_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(cb_schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.info("persisted ClickBench collection schema to %s", output)
    return output


def _build_clickbench_schema() -> dict[str, Any] | None:
    """Resolve the ClickBench ``hits`` collection schema for capture publishing.

    Define-once resolution order:
      1. Committed JSON (``CLICKBENCH_SCHEMA_JSON``) — the stable source once
         generated; survives the local parquet being freed.
      2. Local typed parquet — introspect (first run, before the JSON exists).
      3. Neither — ``None``; the caller omits the binding (renderer then drops
         it so the rest of the capture still publishes).
    """
    if CLICKBENCH_SCHEMA_JSON.exists():
        logger.info("using committed ClickBench collection schema %s", CLICKBENCH_SCHEMA_JSON)
        return json.loads(CLICKBENCH_SCHEMA_JSON.read_text(encoding="utf-8"))
    if CLICKBENCH_PARQUET.exists():
        logger.info(
            "introspecting ClickBench schema from %s (not yet committed — run "
            "derive-clickbench-typed to persist it)",
            CLICKBENCH_PARQUET,
        )
        return _introspect_clickbench_schema(CLICKBENCH_PARQUET)
    return None


# Scale factors the source-s3 capture carries bindings for. TPC-DS and DSB
# share one column layout across every scale (only row counts / distributions
# differ), so the same typed schema is emitted per scale. Kept in sync with
# the capture bindings in estuary/captures/.
CAPTURE_SCALES: tuple[int, ...] = (1, 10, 100, 1000)


def build_collection_schemas(
    account: str, scales: tuple[int, ...] = CAPTURE_SCALES, namespace: str = "benchmark"
) -> dict[str, dict[str, Any]]:
    """Build the collection-name → schema mapping for the source-s3 capture.

    Args:
        account: ESTUARY_ACCOUNT value (e.g. ``"CorrDyn"``). The collection
            names are templated as ``{account}/{namespace}/<suite>/sf<scale>/<table>``.
        scales: TPC-DS/DSB scale factors to emit collections for. Defaults to
            every scale the capture binds (``CAPTURE_SCALES``); the column
            layout is identical across scales.
        namespace: The catalog namespace segment after ``account`` (default
            ``"benchmark"`` — the production pipeline). Alternate pipelines pass
            their own namespace so their collections carry the same typed column
            schemas rather than falling back to a ``flow_document`` blob.

    Returns:
        Dict of ``(24 TPC-DS + 24 DSB) × len(scales)`` entries, plus 1
        ClickBench entry resolved by :func:`_build_clickbench_schema` (committed
        JSON → local parquet → omit). If ClickBench can't be resolved the entry
        is omitted (with a warning) rather than raising — the capture still
        publishes for the TPC-DS/DSB scales, and the renderer drops the
        now-undefined ClickBench binding.
    """
    # Column names + types come from the DuckDB tpcds extension (authoritative,
    # no external DDL file). DSB shares the TPC-DS schema. _sql_type_to_jsonschema
    # maps the DuckDB type strings (BIGINT / DECIMAL(p,s) / DATE / VARCHAR / …).
    tpcds_schema = tpcds_table_schema()
    missing = [t for t in TPCDS_TABLES if t not in tpcds_schema]
    if missing:
        raise RuntimeError(f"tpcds extension schema missing tables: {missing}")

    schemas: dict[str, dict[str, Any]] = {}
    for scale in scales:
        for table in TPCDS_TABLES:
            cols = list(tpcds_schema[table])
            schemas[f"{account}/{namespace}/tpcds/sf{scale}/{table}"] = _schema_with_meta(
                cols, _sql_type_to_jsonschema
            )
            schemas[f"{account}/{namespace}/dsb/sf{scale}/{table}"] = _schema_with_meta(
                cols, _sql_type_to_jsonschema
            )

    cb_schema = _build_clickbench_schema()
    if cb_schema is None:
        logger.warning(
            "ClickBench schema unresolved (no committed %s and no local %s); "
            "omitting the clickbench/hits collection. Run derive-clickbench-typed "
            "to generate + commit it.",
            CLICKBENCH_SCHEMA_JSON,
            CLICKBENCH_PARQUET,
        )
        return schemas
    schemas[f"{account}/{namespace}/clickbench/hits"] = cb_schema
    return schemas

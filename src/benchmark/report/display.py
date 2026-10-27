"""Engine display names — the one place platform ids become reader-facing names.

The results DB stores lowercase platform ids (``motherduck``, ``bigquery``); the
published report writes vendor casing (``MotherDuck``, ``BigQuery``). Every module
that renders an id for a reader resolves it here, so the report cannot spell the
same engine two ways. Machine records (the provenance appendix, file paths,
``metadata`` JSON) keep the raw id: those rows document what the database says,
not how the vendor brands it.
"""

from __future__ import annotations

#: engine id -> vendor-cased display name.
ENGINE_DISPLAY: dict[str, str] = {
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "bigquery": "BigQuery",
    "redshift": "Redshift",
    "motherduck": "MotherDuck",
    "clickhouse": "ClickHouse",
    "fabric": "Fabric",
}


def engine_display(platform: str) -> str:
    """Vendor casing for a platform id; unknown ids pass through unchanged."""
    return ENGINE_DISPLAY.get(platform, platform)

"""Connector factory — maps platform IDs to connector classes.

To stay robust against an operator that hasn't installed every SDK yet, each
engine is registered via a lazy import wrapper: the connector module is only
loaded the first time someone asks for that platform. Missing SDK packages
surface as a clear ``ConnectorError`` instead of breaking the registry
module's import for everyone.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from ..exceptions import ConnectorError
from .base import WarehouseConnector

logger = logging.getLogger("benchmark.connectors.registry")

# Mapping of platform_id -> (module under benchmark.connectors, class name), in the
# canonical engine order (the order of the engines table in the docs).
_REGISTRY: dict[str, tuple[str, str]] = {
    "snowflake": (".snowflake", "SnowflakeConnector"),
    "databricks": (".databricks", "DatabricksConnector"),
    "bigquery": (".bigquery", "BigQueryConnector"),
    "redshift": (".redshift", "RedshiftConnector"),
    "fabric": (".fabric", "FabricConnector"),
    "clickhouse": (".clickhouse", "ClickHouseConnector"),
    "motherduck": (".motherduck", "MotherDuckConnector"),
}

#: The seven engines in canonical order. ``--platforms all`` expands to this on
#: every command that takes ``--platforms`` (``cli._resolve_platforms``).
ALL_PLATFORMS: tuple[str, ...] = tuple(_REGISTRY)


def _load(platform: str) -> type[WarehouseConnector]:
    module_path, class_name = _REGISTRY[platform]
    try:
        module = importlib.import_module(module_path, package=__package__)
    except ImportError as exc:
        raise ConnectorError(
            f"connector for '{platform}' could not be imported: {exc}. "
            "Install the engine's SDK (see pyproject.toml) and retry."
        ) from exc
    cls = getattr(module, class_name)
    return cls


def connector_class(platform: str) -> type[WarehouseConnector]:
    """The connector class for ``platform``, without constructing one.

    Lets callers inspect class-level policy — notably ``INLINE_SERVER_TIME`` — without
    opening a connection or needing credentials.
    """
    if platform not in _REGISTRY:
        raise ValueError(
            f"no connector registered for platform '{platform}'. Registered: {sorted(_REGISTRY)}"
        )
    return _load(platform)


def create_connector(platform: str, config: dict[str, Any] | None = None) -> WarehouseConnector:
    return connector_class(platform)(config)


def registered_platforms() -> list[str]:
    """The registered engine ids, in canonical order (:data:`ALL_PLATFORMS`)."""
    return list(ALL_PLATFORMS)

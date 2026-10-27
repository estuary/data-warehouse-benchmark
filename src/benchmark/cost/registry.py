"""Cost-collector factory — mirrors ``connectors/registry.py``.

Lazy imports so missing/optional engine SDKs don't break the registry module
for engines that *are* available.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from ..exceptions import BenchmarkError
from .base import CostCollector

logger = logging.getLogger("benchmark.cost.registry")

_REGISTRY: dict[str, tuple[str, str]] = {
    "motherduck": (".motherduck", "MotherDuckCostCollector"),
    "bigquery": (".bigquery", "BigQueryCostCollector"),
    "snowflake": (".snowflake", "SnowflakeCostCollector"),
    "databricks": (".databricks", "DatabricksCostCollector"),
    "redshift": (".redshift", "RedshiftCostCollector"),
    "fabric": (".fabric", "FabricCostCollector"),
    "clickhouse": (".clickhouse", "ClickHouseCostCollector"),
}


def _load(platform: str) -> type[CostCollector]:
    module_path, class_name = _REGISTRY[platform]
    try:
        module = importlib.import_module(module_path, package=__package__)
    except ImportError as exc:
        raise BenchmarkError(
            f"cost collector for '{platform}' could not be imported: {exc}"
        ) from exc
    return getattr(module, class_name)


def create_cost_collector(platform: str, config: dict[str, Any] | None = None) -> CostCollector:
    if platform not in _REGISTRY:
        raise KeyError(
            f"no cost collector registered for platform '{platform}'. "
            f"Registered: {sorted(_REGISTRY)}"
        )
    cls = _load(platform)
    return cls(**(config or {}))


def registered_platforms() -> list[str]:
    return sorted(_REGISTRY)

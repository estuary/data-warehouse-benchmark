"""Idempotent pre-publish step for the ClickHouse materialization.

The materialize-clickhouse Estuary connector errors out if the destination
database doesn't already exist — it creates tables, not databases. We could
ask the operator to ``CREATE DATABASE`` by hand, but that breaks the
"add a new materialization block, re-publish, done" workflow and silently
diverges from the YAML.

This module walks the YAML, pulls one database name out of each
materialization's ``endpoint.connector.config.database``, and issues
``CREATE DATABASE IF NOT EXISTS`` for each via the same
:class:`ClickHouseConnector` the harness uses. Idempotent: re-running against
an already-prepared cluster is a no-op.

Hooked into ``benchmark flow-publish clickhouse`` (see :mod:`benchmark.cli`)
so the prep happens automatically before flowctl publishes. Operators can
also run it on its own via ``benchmark clickhouse-prepare-databases``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from ..connectors.registry import create_connector
from ..exceptions import ConnectorError

logger = logging.getLogger("benchmark.staging.clickhouse_prepare")


def extract_databases(yaml_path: Path) -> list[str]:
    """Return the distinct database names referenced by ClickHouse
    materialization blocks in ``yaml_path``, in YAML order.

    Reads the YAML pre-substitution: the ``database`` field is expected to
    be a literal string (we don't currently template database names with
    ``${VAR}`` placeholders, and this asserts that — a placeholder would
    show up here as the literal ``${VAR}`` string and fail later at
    ``CREATE DATABASE``, which is the right failure mode).
    """
    doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    seen: list[str] = []
    for mat_name, mat in (doc.get("materializations") or {}).items():
        config = (((mat or {}).get("endpoint") or {}).get("connector") or {}).get("config") or {}
        database = config.get("database")
        if not isinstance(database, str) or not database:
            logger.warning(
                "materialization has no string database field; skipping",
                extra={"materialization": mat_name},
            )
            continue
        if database not in seen:
            seen.append(database)
    return seen


def prepare_databases(yaml_path: Path) -> list[str]:
    """``CREATE DATABASE IF NOT EXISTS`` every database referenced by the
    materialization YAML. Returns the list of databases ensured (in YAML
    order).

    Raises :class:`ConnectorError` if the connector can't be opened or a
    CREATE statement fails — better to fail loudly than to publish a
    materialization that will silently 404 on its destination.
    """
    databases = extract_databases(yaml_path)
    if not databases:
        logger.info("no clickhouse databases referenced by %s; nothing to prepare", yaml_path.name)
        return []

    logger.info(
        "preparing %d clickhouse database(s) from %s: %s",
        len(databases),
        yaml_path.name,
        ", ".join(databases),
    )

    connector = create_connector("clickhouse", config={})
    with connector:
        for db in databases:
            result = connector.execute_query(f"CREATE DATABASE IF NOT EXISTS {db}")
            if result.error_message:
                raise ConnectorError(
                    f"CREATE DATABASE IF NOT EXISTS {db} failed: {result.error_message}"
                )
            logger.info("  ensured database %s", db)

    return databases

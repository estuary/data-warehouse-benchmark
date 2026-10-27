"""Per-engine physical-design specification.

Each engine gets a JSON entry describing clustering keys, distribution
strategy, partition keys, etc. The harness writes
`queries/physical_design.json` so design decisions are version-controlled,
then `apply-physical-design --scale N --platforms X` issues the engine-specific
DDL. The published results do not apply it: every engine benchmarks the
Estuary-loaded tables as-is.

The seven TPC-DS fact tables are consistently clustered on
``(<date_sk>, <item_sk>)`` across engines that surface clustering — this is
the join pattern used by 60+ of the canonical queries.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .settings import get_settings

logger = logging.getLogger("benchmark.physical_design")


_FACT_TABLE_DATE_ITEM_KEYS: dict[str, list[str]] = {
    "store_sales": ["ss_sold_date_sk", "ss_item_sk"],
    "store_returns": ["sr_returned_date_sk", "sr_item_sk"],
    "catalog_sales": ["cs_sold_date_sk", "cs_item_sk"],
    "catalog_returns": ["cr_returned_date_sk", "cr_item_sk"],
    "web_sales": ["ws_sold_date_sk", "ws_item_sk"],
    "web_returns": ["wr_returned_date_sk", "wr_item_sk"],
    "inventory": ["inv_date_sk", "inv_item_sk"],
}


DESIGN: dict[str, dict] = {
    "motherduck": {
        "notes": "DuckDB-native; no clustering / distribution knobs surfaced.",
        "clustering": {},
        "distribution": {},
        "partitioning": {},
    },
    "bigquery": {
        "notes": (
            "TPC-DS surrogate date keys are INT64; PARTITION BY doesn't apply "
            "without joining date_dim. CLUSTER BY (date_sk, item_sk) on the "
            "seven fact tables. BigQuery cannot ALTER clustering on a "
            "populated table — apply via CREATE OR REPLACE … CLUSTER BY …"
        ),
        "partitioning": {},
        "distribution": {},
        "clustering": dict(_FACT_TABLE_DATE_ITEM_KEYS),
    },
    "snowflake": {
        "notes": (
            "CLUSTER BY on the seven fact tables. Snowflake auto-clusters in "
            "the background; the apply step issues "
            "ALTER TABLE ... CLUSTER BY (date_sk, item_sk)."
        ),
        "partitioning": {},
        "distribution": {},
        "clustering": dict(_FACT_TABLE_DATE_ITEM_KEYS),
    },
    "databricks": {
        "notes": (
            "Liquid Clustering on the seven fact tables. Apply via "
            "ALTER TABLE ... CLUSTER BY (date_sk, item_sk); then "
            "OPTIMIZE <table> to materialize."
        ),
        "partitioning": {},
        "distribution": {},
        "clustering": dict(_FACT_TABLE_DATE_ITEM_KEYS),
    },
    "redshift": {
        "notes": (
            "DISTKEY on the natural join column (item_sk) and SORTKEY on "
            "(date_sk) for date-range pruning. Distribution is fixed at "
            "CREATE TABLE — apply step rebuilds via CTAS."
        ),
        "partitioning": {},
        "distribution": {
            t: {"dist_key": cols[1], "dist_style": "KEY", "sort_keys": [cols[0]]}
            for t, cols in _FACT_TABLE_DATE_ITEM_KEYS.items()
        },
        "clustering": {},
    },
    "fabric": {
        "notes": (
            "Fabric DW auto-maintains a CLUSTERED COLUMNSTORE INDEX and "
            "auto-statistics; manual cluster/distribution knobs aren't exposed. "
            "The one supported manual step is CREATE STATISTICS on the fact-table "
            "join/filter columns (date_sk, item_sk) — modest value since auto-stats "
            "is on by default."
        ),
        "partitioning": {},
        "distribution": {},
        "clustering": {},
        "statistics": dict(_FACT_TABLE_DATE_ITEM_KEYS),
    },
    "clickhouse": {
        "notes": (
            "ClickHouse uses an `ORDER BY` clause in CREATE TABLE for primary "
            "key indexing; apply step rebuilds tables via ATTACH/DETACH "
            "PARTITION + new ORDER BY."
        ),
        "partitioning": {},
        "distribution": {},
        "clustering": dict(_FACT_TABLE_DATE_ITEM_KEYS),
    },
}


def emit_design(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(DESIGN, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("physical-design spec written to %s", output)


# ─── DDL emitters per engine ────────────────────────────────────────────────


def _bigquery_cluster_ddl(project: str, dataset: str, table: str, columns: list[str]) -> list[str]:
    cols_csv = ", ".join(columns)
    fqn = f"`{project}.{dataset}.{table}`"
    tmp = f"`{project}.{dataset}.{table}_recluster`"
    return [
        f"CREATE TABLE {tmp} CLUSTER BY {cols_csv} AS SELECT * FROM {fqn};",
        f"DROP TABLE {fqn};",
        f"ALTER TABLE {tmp} RENAME TO `{table}`;",
    ]


def _redshift_rebuild_ddl(schema: str, table: str, spec: dict) -> str:
    dist_key = spec["dist_key"]
    sort_csv = ", ".join(spec.get("sort_keys", []))
    return (
        f"CREATE TABLE {schema}.{table}_redist\n"
        f"  DISTKEY({dist_key})\n"
        f"  SORTKEY({sort_csv})\n"
        f"AS SELECT * FROM {schema}.{table};\n"
        f"DROP TABLE {schema}.{table};\n"
        f"ALTER TABLE {schema}.{table}_redist RENAME TO {table};"
    )


def _clickhouse_rebuild_ddl(database: str, table: str, columns: list[str]) -> str:
    cols_csv = ", ".join(columns)
    return (
        f"CREATE TABLE {database}.{table}_v2 ENGINE = MergeTree "
        f"ORDER BY ({cols_csv}) AS SELECT * FROM {database}.{table};\n"
        f"DROP TABLE {database}.{table};\n"
        f"RENAME TABLE {database}.{table}_v2 TO {database}.{table};"
    )


def apply_design(platforms: list[str], scale_factor: int, design_path: Path) -> None:
    if not design_path.exists():
        raise FileNotFoundError(
            f"physical-design spec missing: {design_path} — run design-physical first"
        )
    spec = json.loads(design_path.read_text(encoding="utf-8"))
    for platform in platforms:
        entry = spec.get(platform)
        if not entry:
            logger.warning("no physical-design entry for platform %s; skipping", platform)
            continue
        if (
            not entry.get("clustering")
            and not entry.get("distribution")
            and not entry.get("partitioning")
            and not entry.get("statistics")
        ):
            logger.info(
                "physical-design no-op for %s at SF%d (%s)",
                platform,
                scale_factor,
                entry.get("notes", ""),
            )
            continue

        emit = _PLATFORM_EMITTERS.get(platform)
        if emit is None:
            logger.warning(
                "physical-design apply not implemented for platform %s; entry: %s",
                platform,
                entry,
            )
            continue
        emit(entry, scale_factor)


# ─── Per-platform DDL plan loggers ───────────────────────────────────────────


def _apply_bigquery(entry: dict, scale_factor: int) -> None:
    from .connectors.registry import create_connector

    clustering = entry.get("clustering", {})
    if not clustering:
        return

    project = get_settings().BIGQUERY.PROJECT or ""
    if not project:
        raise RuntimeError("BIGQUERY_PROJECT not set")

    datasets = [f"tpcds_sf{scale_factor}", f"dsb_sf{scale_factor}"]
    connector = create_connector("bigquery")
    connector.connect()
    try:
        for dataset in datasets:
            for table, cols in clustering.items():
                stmts = _bigquery_cluster_ddl(project, dataset, table, cols)
                logger.info("%s.%s -> CLUSTER BY (%s)", dataset, table, ", ".join(cols))
                for stmt in stmts:
                    connector.client.query(stmt).result()
                logger.info("%s.%s done", dataset, table)
    finally:
        connector.disconnect()


def _apply_snowflake(entry: dict, scale_factor: int) -> None:
    """Execute Snowflake physical-design via in-place ``ALTER TABLE … CLUSTER BY``.

    NON-DESTRUCTIVE BY CONSTRUCTION. Unlike Redshift / ClickHouse / BigQuery —
    whose sort/dist/cluster keys are fixed on a populated table and therefore
    require a CTAS rebuild (a path that can silently deduplicate ClickHouse fact
    rows, see ``_rebuild_clickhouse_table``) — Snowflake alters the
    clustering key as a metadata operation. No table recreation, no CTAS, no row
    movement, and Snowflake has no ReplacingMergeTree-style dedup, so this cannot
    drop or collapse rows. Reclustering then proceeds in the background as a
    serverless task.

    Applies to BOTH ``TPCDS_SF{N}`` and ``DSB_SF{N}`` schemas (DSB shares the
    TPC-DS fact-table DDL). Idempotent: reads each table's current
    ``CLUSTERING_KEY`` and skips tables already clustered on the target columns,
    so re-runs don't trigger redundant background reclustering credits.
    """
    from .connectors.registry import create_connector

    clustering = entry.get("clustering", {})
    if not clustering:
        return

    database = get_settings().SNOWFLAKE.DATABASE
    schemas = [f"TPCDS_SF{scale_factor}", f"DSB_SF{scale_factor}"]
    connector = create_connector("snowflake", config={"scale_factor": scale_factor})
    connector.connect()
    try:
        for schema in schemas:
            logger.info(
                "Snowflake apply: clustering %d fact tables in %s.%s",
                len(clustering),
                database,
                schema,
            )
            for table, cols in clustering.items():
                _cluster_snowflake_table(connector, database, schema, table, cols)
    finally:
        connector.disconnect()


def _cluster_snowflake_table(
    connector, database: str, schema: str, table: str, cols: list[str]
) -> None:
    """Set one fact table's clustering key in place (idempotent, non-destructive)."""
    target = f"{database}.{schema}.{table.upper()}"

    # SHOW TABLES doubles as an existence check (skip cleanly if the table is
    # absent) and an idempotency probe. Snowflake renders CLUSTERING_KEY as e.g.
    # "LINEAR(ss_sold_date_sk, ss_item_sk)".
    probe = connector.execute_query(
        f"SHOW TABLES LIKE '{table.upper()}' IN SCHEMA {database}.{schema}"
    )
    if probe.error_message:
        raise RuntimeError(f"Snowflake SHOW TABLES failed for {target}: {probe.error_message}")
    if not probe.rows:
        logger.warning("table %s not found; skipping", target)
        return

    # SHOW TABLES exposes the clustering key in the ``cluster_by`` column,
    # rendered as e.g. "LINEAR(ss_sold_date_sk, ss_item_sk)".
    cols_lower = [c.lower() for c in probe.columns]
    ck_idx = cols_lower.index("cluster_by") if "cluster_by" in cols_lower else None
    current_ck = (probe.rows[0][ck_idx] if ck_idx is not None else None) or ""
    target_paren = "(" + ",".join(c.lower() for c in cols) + ")"
    if target_paren in current_ck.replace(" ", "").lower():
        logger.info("  %s already CLUSTER BY %s; skipping", target, target_paren)
        return

    ddl = f"ALTER TABLE {target} CLUSTER BY ({', '.join(cols)})"
    result = connector.execute_query(ddl)
    if result.error_message:
        raise RuntimeError(f"Snowflake CLUSTER BY failed on {target}: {result.error_message}")
    logger.info("  %s -> CLUSTER BY (%s)", target, ", ".join(cols))


def _apply_databricks(entry: dict, scale_factor: int) -> None:
    """Execute Databricks physical-design via Liquid Clustering (in-place).

    NON-DESTRUCTIVE. Like Snowflake — and unlike Redshift / ClickHouse, whose
    keys are fixed on a populated table and therefore require a CTAS rebuild
    (a path that can silently deduplicate ClickHouse fact rows) —
    Databricks alters the clustering key as a Delta metadata operation:
    ``ALTER TABLE … CLUSTER BY (cols)`` then ``OPTIMIZE`` to compact existing
    data into the new layout. Delta OPTIMIZE only rewrites file layout and has
    no ReplacingMergeTree-style dedup, so neither step can drop or collapse
    rows.

    Applies to BOTH ``tpcds_sf{N}`` and ``dsb_sf{N}`` schemas (DSB shares the
    TPC-DS fact-table DDL). Idempotent: reads ``DESCRIBE DETAIL.clusteringColumns``
    and skips the re-cluster + OPTIMIZE when a table already carries the target
    columns, so re-runs don't pay redundant OPTIMIZE compute.
    """
    from .connectors.registry import create_connector

    clustering = entry.get("clustering", {})
    if not clustering:
        return

    catalog = get_settings().DATABRICKS.CATALOG
    schemas = [f"tpcds_sf{scale_factor}", f"dsb_sf{scale_factor}"]
    connector = create_connector("databricks")
    connector.connect()
    try:
        for schema in schemas:
            logger.info(
                "Databricks apply: clustering %d fact tables in %s.%s",
                len(clustering),
                catalog,
                schema,
            )
            for table, cols in clustering.items():
                _cluster_databricks_table(connector, catalog, schema, table, cols)
    finally:
        connector.disconnect()


def _flatten_cluster_cols(value: object) -> list[str]:
    """Normalize ``DESCRIBE DETAIL.clusteringColumns`` to a flat lowercased list.

    Databricks has rendered this as a flat array (``["c1","c2"]``) and, in older
    runtimes, as nested arrays (``[["c1"],["c2"]]``). The connector may hand it
    back as a parsed Python list or a JSON string. Returns ``[]`` when it can't
    be parsed (caller then just applies — still non-destructive).
    """
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    out: list[str] = []
    if isinstance(value, list | tuple):
        for item in value:
            if isinstance(item, list | tuple):
                out.extend(str(x).lower() for x in item)
            else:
                out.append(str(item).lower())
    return out


def _cluster_databricks_table(
    connector, catalog: str, schema: str, table: str, cols: list[str]
) -> None:
    """Liquid-cluster one fact table in place (idempotent, non-destructive)."""
    target = f"{catalog}.{schema}.{table}"

    # Idempotency probe: DESCRIBE DETAIL exposes the active Liquid Clustering
    # columns in a `clusteringColumns` column. Best-effort — if the probe errors
    # or the column is absent, fall through and (re-)apply, which is still
    # non-destructive (just redundant OPTIMIZE compute).
    probe = connector.execute_query(f"DESCRIBE DETAIL {target}")
    if probe.error_message:
        logger.warning(
            "DESCRIBE DETAIL failed for %s (%s); applying anyway",
            target,
            probe.error_message,
        )
    elif probe.rows:
        cols_lower = [c.lower() for c in probe.columns]
        cc_idx = (
            cols_lower.index("clusteringcolumns") if "clusteringcolumns" in cols_lower else None
        )
        current = _flatten_cluster_cols(probe.rows[0][cc_idx] if cc_idx is not None else None)
        if current == [c.lower() for c in cols]:
            logger.info("  %s already CLUSTER BY (%s); skipping", target, ", ".join(cols))
            return

    # ALTER sets the clustering key (metadata); OPTIMIZE compacts existing rows
    # into the new layout. Both preserve every row.
    for stmt in (
        f"ALTER TABLE {target} CLUSTER BY ({', '.join(cols)})",
        f"OPTIMIZE {target}",
    ):
        result = connector.execute_query(stmt)
        if result.error_message:
            raise RuntimeError(
                f"Databricks physical-design apply failed on {target}: {result.error_message}"
            )
    logger.info("  %s -> CLUSTER BY (%s) + OPTIMIZE", target, ", ".join(cols))


def _apply_redshift(entry: dict, scale_factor: int) -> None:
    """Execute Redshift physical-design DDL via CTAS rebuilds.

    Redshift can't ALTER distribution on a populated table, so each fact
    table is rebuilt as ``CREATE TABLE … AS SELECT *`` with the correct
    ``DISTKEY``/``SORTKEY``, swapped in via RENAME, then ``ANALYZE``-d to
    refresh statistics.

    The 7 TPC-DS fact tables exist in BOTH ``tpcds_sf{N}`` and ``dsb_sf{N}``
    schemas (DSB shares the TPC-DS DDL), so the rebuild applies to both.

    Idempotent: the first step drops any stale ``{table}_redist`` from a
    previously interrupted apply, so retries are safe.
    """
    from .connectors.registry import create_connector

    distribution = entry.get("distribution", {})
    if not distribution:
        return

    schemas = [f"tpcds_sf{scale_factor}", f"dsb_sf{scale_factor}"]
    connector = create_connector("redshift")
    connector.connect()
    try:
        for schema in schemas:
            logger.info(
                "Redshift apply: rebuilding %d fact tables in %s",
                len(distribution),
                schema,
            )
            for table, spec in distribution.items():
                _rebuild_redshift_table(connector, schema, table, spec)
    finally:
        connector.disconnect()


def _rebuild_redshift_table(connector, schema: str, table: str, spec: dict) -> None:
    """CTAS-rebuild one Redshift table with the configured DISTKEY/SORTKEY."""
    dist_key = spec["dist_key"]
    sort_csv = ", ".join(spec.get("sort_keys", []))
    target = f"{schema}.{table}"
    redist = f"{schema}.{table}_redist"

    # Order chosen so the original table is never gone before the new one is
    # named correctly. Crash recovery between any two steps is local: just
    # rename whichever table exists back to {table}.
    statements = [
        f"DROP TABLE IF EXISTS {redist}",
        (
            f"CREATE TABLE {redist} "
            f"DISTKEY({dist_key}) SORTKEY({sort_csv}) "
            f"AS SELECT * FROM {target}"
        ),
        f"DROP TABLE {target}",
        f"ALTER TABLE {redist} RENAME TO {table}",
        f"ANALYZE {target}",
    ]
    for stmt in statements:
        result = connector.execute_query(stmt)
        if result.error_message:
            raise RuntimeError(
                f"Redshift physical-design apply failed on {target}: {result.error_message}"
            )
    logger.info("  %s rebuilt: DISTKEY(%s) SORTKEY(%s)", target, dist_key, sort_csv)


def _apply_clickhouse(entry: dict, scale_factor: int) -> None:
    """Execute ClickHouse physical-design via per-fact-table rebuilds.

    ClickHouse's MergeTree ``ORDER BY`` is immutable on a populated table,
    so each fact table is rebuilt as ``CREATE TABLE <new> ... ORDER BY ...
    AS SELECT * FROM <orig>`` and then atomically swapped via
    ``EXCHANGE TABLES``.

    Applies to BOTH ``tpcds_sf{N}`` and ``dsb_sf{N}`` databases (DSB shares
    the TPC-DS DDL).

    Idempotent: reads ``system.tables.sorting_key`` first and skips tables
    that already match the target. Also drops any stale ``<table>_v2`` from
    a previously-interrupted apply.
    """
    from .connectors.registry import create_connector

    clustering = entry.get("clustering", {})
    if not clustering:
        return

    databases = [f"tpcds_sf{scale_factor}", f"dsb_sf{scale_factor}"]
    connector = create_connector("clickhouse")
    connector.connect()
    try:
        for db in databases:
            logger.info(
                "ClickHouse apply: checking %d fact tables in %s",
                len(clustering),
                db,
            )
            for table, cols in clustering.items():
                _rebuild_clickhouse_table(connector, db, table, cols)
    finally:
        connector.disconnect()


def _rebuild_clickhouse_table(connector, database: str, table: str, cols: list[str]) -> None:
    """Rebuild one ClickHouse fact table to use the configured ``ORDER BY``."""
    target_key = ", ".join(cols)
    fqn = f"{database}.{table}"
    staging = f"{database}.{table}_v2"

    # Read current engine + sorting key. Two things to handle:
    #   * Preserve ClickHouse Cloud's ``SharedMergeTree`` family (don't
    #     downgrade to plain MergeTree on rebuild — that loses replication).
    #   * Swap out the *replacing* variant: the Estuary materializer creates
    #     ``SharedReplacingMergeTree`` so the upsert stream can re-key the
    #     same row safely, but the replacing engine **silently deduplicates
    #     by the ORDER BY**. Our physical-design key is fact-shaped
    #     ``(date_sk, item_sk)`` which is NOT unique in TPC-DS / DSB, so a
    #     CTAS into ``SharedReplacingMergeTree`` with that key collapses
    #     5–50% of fact-table rows. Rebuild as the non-replacing variant.
    probe = connector.execute_query(
        f"SELECT engine, sorting_key FROM system.tables "
        f"WHERE database = '{database}' AND name = '{table}'"
    )
    if probe.error_message:
        raise RuntimeError(
            f"clickhouse system.tables lookup failed for {fqn}: {probe.error_message}"
        )
    if not probe.rows:
        logger.warning("table %s not found; skipping", fqn)
        return
    engine, current_key = probe.rows[0]
    current_key = (current_key or "").strip()
    # SharedReplacingMergeTree -> SharedMergeTree, ReplacingMergeTree -> MergeTree
    rebuild_engine = engine.replace("ReplacingMergeTree", "MergeTree")
    if current_key == target_key and rebuild_engine == engine:
        logger.info("  %s already ORDER BY (%s) on %s; skipping", fqn, target_key, engine)
        return

    # Cleanup any stale staging from a previously-aborted run, then rebuild.
    # EXCHANGE TABLES swaps the two names atomically — after the exchange,
    # the staging name holds the OLD data and is safe to drop.
    statements = [
        f"DROP TABLE IF EXISTS {staging}",
        (
            f"CREATE TABLE {staging} ENGINE = {rebuild_engine} "
            f"ORDER BY ({target_key}) "
            # allow_nullable_key=1: TPC-DS surrogate keys are Nullable(Int32)
            # in the Estuary-materialized schema; without this ClickHouse
            # refuses Nullable types as ORDER BY columns.
            f"SETTINGS allow_nullable_key = 1 "
            f"AS SELECT * FROM {fqn}"
        ),
        f"EXCHANGE TABLES {fqn} AND {staging}",
        f"DROP TABLE {staging}",
    ]
    for stmt in statements:
        result = connector.execute_query(stmt)
        if result.error_message:
            raise RuntimeError(
                f"ClickHouse physical-design apply failed on {fqn}: {result.error_message}"
            )
    logger.info("  %s rebuilt: ORDER BY (%s)", fqn, target_key)


def _fabric_statistics_ddl(schema: str, table: str, columns: list[str]) -> list[str]:
    """Idempotent CREATE STATISTICS statements for a Fabric DW table.

    Fabric Warehouse exposes no manual cluster/distribution knobs (columnstore
    and distribution are auto-managed), so the one supported manual physical-
    design step is statistics on the join/filter columns. Each statement is
    guarded by a sys.stats existence check, so re-runs are no-ops.
    """
    stmts: list[str] = []
    for col in columns:
        stat = f"stat_{table}_{col}"
        stmts.append(
            "IF NOT EXISTS (SELECT 1 FROM sys.stats "
            f"WHERE name = '{stat}' AND object_id = OBJECT_ID('{schema}.{table}')) "
            f"CREATE STATISTICS {stat} ON {schema}.{table} ({col});"
        )
    return stmts


def _apply_fabric(entry: dict, scale_factor: int) -> None:
    """Apply Fabric DW physical design: CREATE STATISTICS on fact-table join
    columns. Fabric Warehouse auto-manages columnstore + distribution, so there
    is no clustering/distribution DDL to issue; statistics are the one supported
    manual knob (modest — auto-stats is on by default). Applies to both
    tpcds_sf{N} and dsb_sf{N} on the DW component; idempotent via sys.stats.
    """
    from .connectors.registry import create_connector

    statistics = entry.get("statistics", {})
    if not statistics:
        return

    schemas = [f"tpcds_sf{scale_factor}", f"dsb_sf{scale_factor}"]
    connector = create_connector("fabric")  # defaults to the DW component
    connector.connect()
    try:
        for schema in schemas:
            logger.info(
                "Fabric apply: ensuring statistics on %d fact tables in %s",
                len(statistics),
                schema,
            )
            for table, columns in statistics.items():
                for stmt in _fabric_statistics_ddl(schema, table, columns):
                    result = connector.execute_query(stmt)
                    if result.error_message:
                        raise RuntimeError(
                            f"Fabric physical-design apply failed on {schema}.{table}: "
                            f"{result.error_message}"
                        )
                logger.info("  %s.%s statistics ensured", schema, table)
    finally:
        connector.disconnect()


_PLATFORM_EMITTERS = {
    "bigquery": _apply_bigquery,
    "snowflake": _apply_snowflake,
    "databricks": _apply_databricks,
    "redshift": _apply_redshift,
    "clickhouse": _apply_clickhouse,
    "fabric": _apply_fabric,
}

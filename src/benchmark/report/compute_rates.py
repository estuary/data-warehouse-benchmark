"""Effective list-price USD/hour for a compute-size label, per engine.

The report's cross-engine views need one comparable compute axis: the
published hourly list price of whatever "size" each engine ran on (Snowflake
warehouse size, BigQuery slot commitment, Redshift RPUs, Fabric SKU, ...).
Rates are reused from the cost collectors wherever they already exist so this
axis stays consistent with recorded costs; only Snowflake's credits/hour table
lives here (its collector prices credits post-hoc from
``QUERY_ATTRIBUTION_HISTORY`` and carries no per-size rate).

All values are list prices — negotiated discounts are out of scope. Labels are
the ``compute_label`` strings stamped on result rows by
``connectors.base.compute_label_from_environment`` (e.g. ``"Large (Gen2)"``,
``"ENTERPRISE / 200 slots"``, ``"16 RPU"``, ``"F32"``, ``"Standard"``).
``None`` means the label can't be priced as capacity-per-hour — chiefly BigQuery
on-demand, which is scan-priced and has no hourly rate.
"""

from __future__ import annotations

import re

from ..cost.bigquery import BigQueryCostCollector
from ..cost.clickhouse import USD_PER_COMPUTE_UNIT_HOUR, VCPU_PER_COMPUTE_UNIT
from ..cost.databricks import DatabricksCostCollector
from ..cost.fabric import FabricCostCollector
from ..cost.motherduck import MotherDuckCostCollector
from ..cost.redshift import RedshiftCostCollector
from ..cost.snowflake import SnowflakeCostCollector

# ClickHouse Cloud provisions 1 vCPU per 4 GiB of replica memory, so a
# "<replicas>x<mem>GB" label fully determines the billable compute units.
_CLICKHOUSE_LABEL_RE = re.compile(r"^(\d+)x(\d+)GB$", re.I)
_GIB_PER_VCPU = 4

# Snowflake credits/hour by warehouse size (Standard warehouse type, Gen1
# baseline). Sizes as reported by SHOW WAREHOUSES (the `actual.size` the label
# is derived from).
SNOWFLAKE_CREDITS_PER_HOUR: dict[str, float] = {
    "x-small": 1.0,
    "small": 2.0,
    "medium": 4.0,
    "large": 8.0,
    "x-large": 16.0,
    "2x-large": 32.0,
    "3x-large": 64.0,
    "4x-large": 128.0,
}
# Gen2 Standard warehouses bill a credit multiplier over Gen1: 1.35x, as the
# benchmark account's metering shows (the busiest hours bill exactly 1.35 credits per
# size-hour). The benchmark provisions generation="2" (terraform), so swept labels
# carry "(Gen2)" and get this multiplier.
SNOWFLAKE_GEN2_CREDIT_MULTIPLIER = 1.35
# The cheapest edition that runs the benchmark (Gen2 standard warehouses run on
# Standard; nothing here needs Enterprise), as every engine is priced at its cheapest
# capable tier; $/credit from the cost collector's price table.
SNOWFLAKE_EDITION = "standard"


def effective_usd_per_hour(platform: str, compute_label: str | None) -> float | None:
    """List-price $/hour for ``compute_label`` on ``platform``, or ``None``.

    Best-effort and case-insensitive on the size token. Unknown labels return
    ``None`` (rendered as a blank cell / skipped point, never a guess).
    """
    if not compute_label:
        return None
    label = compute_label.strip()

    if platform == "snowflake":
        size = re.sub(r"\s*\(gen\d+\)\s*$", "", label, flags=re.IGNORECASE).strip().lower()
        credits = SNOWFLAKE_CREDITS_PER_HOUR.get(size)
        if credits is None:
            return None
        if re.search(r"\(gen2\)", label, flags=re.IGNORECASE):
            credits *= SNOWFLAKE_GEN2_CREDIT_MULTIPLIER
        return credits * SnowflakeCostCollector.CREDIT_PRICE_USD[SNOWFLAKE_EDITION]

    if platform == "bigquery":
        m = re.search(r"(\d+)\s*slots", label, flags=re.IGNORECASE)
        if m:
            return int(m.group(1)) * BigQueryCostCollector.ENTERPRISE_USD_PER_SLOT_HOUR
        return None  # on-demand: scan-priced, no capacity $/hr

    if platform == "redshift":
        m = re.search(r"(\d+)\s*RPU", label, flags=re.IGNORECASE)
        if m:
            return int(m.group(1)) * RedshiftCostCollector.DEFAULT_RPU_PRICE_USD
        return None

    if platform == "fabric":
        return FabricCostCollector.CAPACITY_USD_PER_HOUR.get(label.lower())

    if platform == "motherduck":
        return MotherDuckCostCollector.TIER_USD_PER_HOUR.get(label.lower())

    if platform == "databricks":
        dbu = DatabricksCostCollector.DBU_PER_HOUR.get(label.lower())
        if dbu is None:
            return None
        return dbu * DatabricksCostCollector.DEFAULT_DBU_PRICE_USD

    if platform == "clickhouse":
        # The label DOES determine the rate: ClickHouse Cloud provisions 1 vCPU per
        # 4 GiB and bills per compute unit of 2 vCPU, so "<replicas>x<mem>GB" gives
        # usd/hr = (mem_gb / 8) * replicas * rate. Verified against two
        # independently recorded campaign figures — $0.60/hr at 1x16GB and
        # $13.43/hr at 3x120GB. Returning None here would silently drop ClickHouse
        # out of every price-normalised comparison — including the ClickBench one,
        # where it is the engine most likely to win on price-performance.
        match = _CLICKHOUSE_LABEL_RE.match(label)
        if match is None:
            return None
        replicas, mem_gb = int(match.group(1)), int(match.group(2))
        units = (mem_gb / _GIB_PER_VCPU) / VCPU_PER_COMPUTE_UNIT
        return units * replicas * USD_PER_COMPUTE_UNIT_HOUR

    return None

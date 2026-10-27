"""Resize the Databricks serverless SQL warehouse to a run-plan size — PINNED.

The warehouse size is a workspace resource (one warehouse, addressed by
``DATABRICKS_SQL_HTTP_PATH``), so per-scale sizing is out-of-band — the
MotherDuck/Redshift pattern. This script edits ``cluster_size`` via the SQL
Warehouses REST API, polls until the live config reflects it, and keeps
``terraform/sizing.auto.tfvars.json`` in sync so a later ``terraform apply``
doesn't revert the size.

**Benchmarks always run pinned to ONE cluster** (``min_num_clusters ==
max_num_clusters == 1``) — the Redshift ``max_capacity == base_capacity`` /
single-cluster-Snowflake semantics — so multi-cluster autoscaling (IWM) can't
flex capacity under a concurrency pass and the size label measures a fixed
unit of compute. Only non-benchmark work (bulk loads) may raise the ceiling,
explicitly: ``--max-clusters N``; the next resize re-pins.

Usage:
    uv run python scripts/resize/databricks_resize.py --size Small
    uv run python scripts/resize/databricks_resize.py --size Large --wait 600
    uv run python scripts/resize/databricks_resize.py --size Medium --max-clusters 3  # loads only

Run-plan sizes (src/benchmark/run_plan.yaml): SF1 X-Small, SF10 Small,
SF100 Medium, SF1000 Large.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.request
from pathlib import Path

from benchmark.logging_config import configure_logging
from benchmark.settings import get_settings

logger = logging.getLogger("benchmark.scripts.databricks_resize")

SIZING_TFVARS = Path("terraform/sizing.auto.tfvars.json")


def _api(host: str, token: str, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{host}/api/2.0/sql/warehouses/{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def _sync_tfvars(size: str) -> None:
    doc = json.loads(SIZING_TFVARS.read_text()) if SIZING_TFVARS.exists() else {}
    doc["databricks_warehouse_size"] = size
    SIZING_TFVARS.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    logger.info("synced databricks_warehouse_size=%s into %s", size, SIZING_TFVARS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--size", required=True, help="cluster_size: 2X-Small/X-Small/Small/Medium/Large/..."
    )
    parser.add_argument("--wait", type=int, default=300, help="seconds to poll for the resize")
    parser.add_argument(
        "--max-clusters",
        type=int,
        default=1,
        help="Autoscaling ceiling. Benchmarks stay pinned at 1 (fixed capacity); "
        "raise ONLY for non-benchmark bulk loads — the next resize re-pins.",
    )
    args = parser.parse_args()
    configure_logging("INFO")

    db = get_settings().DATABRICKS
    if not (db.HOST and db.TOKEN and db.SQL_HTTP_PATH):
        logger.error("DATABRICKS_HOST / TOKEN / SQL_HTTP_PATH must be set")
        return 1
    host = db.HOST if db.HOST.startswith("http") else f"https://{db.HOST}"
    token = db.TOKEN.get_secret_value()
    warehouse_id = db.SQL_HTTP_PATH.rstrip("/").rsplit("/", 1)[-1]

    before = _api(host, token, warehouse_id)
    logger.info(
        "warehouse %s: %s (clusters %s-%s) -> %s (clusters 1-%s)",
        warehouse_id,
        before.get("cluster_size"),
        before.get("min_num_clusters"),
        before.get("max_num_clusters"),
        args.size,
        args.max_clusters,
    )
    already = (
        before.get("cluster_size") == args.size
        and before.get("min_num_clusters") == 1
        and before.get("max_num_clusters") == args.max_clusters
    )
    if already:
        _sync_tfvars(args.size)
        logger.info("already at %s pinned 1-%s; nothing to do", args.size, args.max_clusters)
        return 0

    _api(
        host,
        token,
        f"{warehouse_id}/edit",
        {
            "id": warehouse_id,
            "cluster_size": args.size,
            "min_num_clusters": 1,
            "max_num_clusters": args.max_clusters,
        },
    )
    deadline = time.time() + args.wait
    while time.time() < deadline:
        cur = _api(host, token, warehouse_id)
        if (
            cur.get("cluster_size") == args.size
            and cur.get("max_num_clusters") == args.max_clusters
        ):
            _sync_tfvars(args.size)
            logger.info(
                "resize applied: cluster_size=%s clusters=%s-%s state=%s",
                cur.get("cluster_size"),
                cur.get("min_num_clusters"),
                cur.get("max_num_clusters"),
                cur.get("state"),
            )
            return 0
        time.sleep(5)
    logger.error("resize to %s not confirmed within %ss", args.size, args.wait)
    return 1


if __name__ == "__main__":
    sys.exit(main())

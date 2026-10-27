"""Resize (or suspend/resume) the Microsoft Fabric capacity to a run-plan SKU.

The capacity is an ARM resource (``Microsoft.Fabric/capacities``) shared by the whole
workspace, so sizing is out-of-band from the run — the Redshift / MotherDuck /
ClickHouse pattern. The Data Warehouse component has no per-query compute dial; the
capacity SKU *is* the size, and the F-ladder steps 4x per rung (F2 -> F8 -> F32 ->
F128), which is what ``run_plan.yaml`` designates per scale.

WHY az CLI AND NOT THE SERVICE PRINCIPAL. The FABRIC_CLIENT_ID service principal is
provisioned for data-plane access (T-SQL over the DW endpoint). Changing a capacity
SKU is a control-plane write that needs Contributor on the resource, and the
applier must be a capacity admin, a role the query principal deliberately does not
hold. So this drives ``az``, which runs as the operator, and fails loudly rather
than silently degrading if that is not logged in.

SUSPEND/RESUME is the cost lever, and it matters more here than elsewhere: a Fabric
capacity bills for being Active whether or not a query runs, and there is no idle
auto-pause. Park it when a campaign ends:

    uv run python scripts/resize/fabric_resize.py --suspend

A suspended capacity refuses queries, so `connectivity-check` will fail until
`--resume`. That is the intended trade, not a fault.

Usage:
    uv run python scripts/resize/fabric_resize.py --show
    uv run python scripts/resize/fabric_resize.py --sku F2
    uv run python scripts/resize/fabric_resize.py --scale 1        # run-plan SKU for a scale
    uv run python scripts/resize/fabric_resize.py --suspend
    uv run python scripts/resize/fabric_resize.py --resume
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from benchmark.logging_config import configure_logging  # noqa: E402
from benchmark.prepare_scale import (  # noqa: E402
    DEFAULT_SIZING_TFVARS_PATH,
    TfvarEdit,
    write_sizing_edits,
)
from benchmark.run_plan import get_run_plan  # noqa: E402
from benchmark.settings import get_settings  # noqa: E402

logger = logging.getLogger("benchmark.scripts.fabric_resize")

POLL_INTERVAL_S = 15
RESIZE_TIMEOUT_S = 900

#: The vendor ladder, smallest first. Used to validate a target before spending a
#: poll window on a SKU the platform will refuse.
VALID_SKUS = ("F2", "F4", "F8", "F16", "F32", "F64", "F128", "F256", "F512")


def _resource_id() -> str:
    f = get_settings().FABRIC
    missing = [
        n
        for n, v in (
            ("FABRIC_SUBSCRIPTION_ID", f.SUBSCRIPTION_ID),
            ("FABRIC_RESOURCE_GROUP", f.RESOURCE_GROUP),
            ("FABRIC_CAPACITY_NAME", f.CAPACITY_NAME),
        )
        if not v
    ]
    if missing:
        raise SystemExit(f"missing settings: {', '.join(missing)}")
    return (
        f"/subscriptions/{f.SUBSCRIPTION_ID}/resourceGroups/{f.RESOURCE_GROUP}"
        f"/providers/Microsoft.Fabric/capacities/{f.CAPACITY_NAME}"
    )


def _az(*args: str) -> dict:
    """Run an az command and parse its JSON. Raises with az's own message."""
    cmd = ["az", *args, "-o", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"az failed ({' '.join(args[:2])}): {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def show() -> tuple[str | None, str | None]:
    """(sku, state) from ARM."""
    doc = _az("resource", "show", "--ids", _resource_id())
    sku = (doc.get("sku") or {}).get("name")
    state = (doc.get("properties") or {}).get("state")
    return sku, state


def _sync_tfvars(sku: str) -> None:
    """Record the SKU in the tool-owned sizing tfvars so terraform agrees.

    NOTE (same trap as Redshift): a later ``prepare-scale`` re-asserts the
    RUN-PLAN target into this file, so if you park the capacity off-plan, re-check
    this value afterwards or a terraform apply will size it back.
    """
    write_sizing_edits(
        [TfvarEdit("fabric", "fabric_capacity_sku", None, sku)],
        REPO_ROOT / DEFAULT_SIZING_TFVARS_PATH,
    )
    logger.info("synced fabric_capacity_sku=%s", sku)


def _wait(want_sku: str | None, want_state: str | None, timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        sku, state = show()
        ok_sku = want_sku is None or (sku or "").upper() == want_sku.upper()
        ok_state = want_state is None or (state or "") == want_state
        if ok_sku and ok_state:
            logger.info("confirmed: sku=%s state=%s", sku, state)
            return True
        logger.info("waiting: sku=%s state=%s", sku, state)
        time.sleep(POLL_INTERVAL_S)
    logger.error("not confirmed within %ss (want sku=%s state=%s)", timeout_s, want_sku, want_state)
    return False


def resize(sku: str, timeout_s: int) -> int:
    sku = sku.upper()
    if sku not in VALID_SKUS:
        logger.error("%s is not a Fabric F-SKU (%s)", sku, ", ".join(VALID_SKUS))
        return 1
    cur_sku, cur_state = show()
    logger.info("capacity: sku=%s state=%s -> %s", cur_sku, cur_state, sku)
    if (cur_sku or "").upper() == sku and cur_state == "Active":
        _sync_tfvars(sku)
        logger.info("already at %s and Active; nothing to do", sku)
        return 0
    if cur_state == "Paused":
        logger.error(
            "capacity is Paused — resume before resizing (--resume), so the SKU change "
            "is applied to a running capacity rather than queued behind a resume"
        )
        return 1
    _az("resource", "update", "--ids", _resource_id(), "--set", f"sku.name={sku}")
    if not _wait(sku, "Active", timeout_s):
        return 1
    _sync_tfvars(sku)
    return 0


def set_state(target: str, timeout_s: int) -> int:
    """target: 'suspend' | 'resume'."""
    cur_sku, cur_state = show()
    want_state = "Paused" if target == "suspend" else "Active"
    logger.info("capacity %s: state=%s -> %s", cur_sku, cur_state, want_state)
    if cur_state == want_state:
        logger.info("already %s; nothing to do", want_state)
        return 0
    _az("resource", "invoke-action", "--ids", _resource_id(), "--action", target)
    return 0 if _wait(None, want_state, timeout_s) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true", help="print the live SKU and state")
    g.add_argument("--sku", help="target F-SKU, e.g. F2")
    g.add_argument("--scale", type=int, choices=[1, 10, 100, 1000], help="run-plan SKU for a scale")
    g.add_argument("--suspend", action="store_true", help="pause the capacity (stops billing)")
    g.add_argument("--resume", action="store_true", help="resume a paused capacity")
    ap.add_argument("--wait", type=int, default=RESIZE_TIMEOUT_S)
    args = ap.parse_args()
    configure_logging("INFO")

    if args.show:
        sku, state = show()
        logger.info("sku=%s state=%s", sku, state)
        return 0
    if args.suspend:
        return set_state("suspend", args.wait)
    if args.resume:
        return set_state("resume", args.wait)

    sku = args.sku
    if args.scale is not None:
        target = get_run_plan().target("fabric", args.scale)
        if target is None:
            logger.error("run_plan declares no fabric SKU for SF%s", args.scale)
            return 1
        sku = str(target)
        logger.info("run-plan SKU for SF%s is %s", args.scale, sku)
    return resize(str(sku), args.wait)


if __name__ == "__main__":
    sys.exit(main())

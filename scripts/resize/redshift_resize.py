"""Drive Redshift Serverless through a scale change: resize -> gates -> ready.

Sizes the workgroup to the run-plan target for ``--scale`` via the AWS API
(no terraform round-trip), keeps ``terraform/sizing.auto.tfvars.json`` in sync
so a later ``terraform apply`` doesn't revert the RPU, then walks the
pre-benchmark gates:

  1. ``update-workgroup --base-capacity <run_plan target>`` + poll AVAILABLE
  2. sync ``redshift_baseline_rpu`` into terraform/sizing.auto.tfvars.json
  3. ``benchmark capture-cost-config --platforms redshift`` (price the actual RPU)
  4. ``benchmark prepare-scale --scale N --platforms redshift`` (readiness report)

Usage:
    uv run python scripts/resize/redshift_resize.py --scale 100
    uv run python scripts/resize/redshift_resize.py --scale 1 --compute-only

Max capacity is ALWAYS pinned to the base (fixed-size semantics) unless
``--max-rpu`` raises it explicitly — benchmarks run pinned so Serverless
vertical autoscaling can't add scaling noise; only non-benchmark work (bulk
loads) should pass a higher ceiling. Terraform pins max == base the same way.

Compute-size band sweeps (the MotherDuck out-of-band pattern — Redshift has one
workgroup, so ``--compute-size`` on `sql`/`concurrency` stays Snowflake-only):
``--rpu`` overrides the run-plan target per band. Each pass re-runs
capture-cost-config, which accumulates a per-size price entry keyed by the
"<N> RPU" compute label.

    uv run python scripts/resize/redshift_resize.py --scale 100 --rpu 32 --compute-only
    uv run benchmark concurrency --scale 100 --platforms redshift
    # ... after the last band, back to the run-plan size (still pinned):
    uv run python scripts/resize/redshift_resize.py --scale 100 --compute-only
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

import boto3

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from benchmark.prepare_scale import (  # noqa: E402
    DEFAULT_SIZING_TFVARS_PATH,
    TfvarEdit,
    write_sizing_edits,
)
from benchmark.run_plan import get_run_plan  # noqa: E402
from benchmark.settings import get_settings  # noqa: E402

logger = logging.getLogger("benchmark.scripts.redshift_resize")

POLL_INTERVAL_S = 15
RESIZE_TIMEOUT_S = 900
# UpdateWorkgroup raises ConflictException while another operation is settling
# ("retry the operation in 10 minutes") — retry for up to this long.
CONFLICT_RETRY_TIMEOUT_S = 2400
CONFLICT_RETRY_INTERVAL_S = 120


def resize_workgroup(target_rpu: int, max_rpu: int) -> None:
    """Set the workgroup's base (and max) capacity and wait until AVAILABLE at it.

    ``max_rpu == target_rpu`` (the default) pins the workgroup to a fixed size —
    benchmarks always run pinned so Serverless vertical autoscaling can't add
    scaling noise (and a compute-size band sweep stays an honest fixed-size
    grid). Terraform pins max == base identically, so an apply never unpins.
    """
    settings = get_settings()
    workgroup = settings.REDSHIFT.WORKGROUP
    session = boto3.Session(profile_name=settings.AWS.PROFILE)
    client = session.client("redshift-serverless", region_name=settings.AWS.REGION)

    wg = client.get_workgroup(workgroupName=workgroup)["workgroup"]
    if (
        wg["baseCapacity"] == target_rpu
        and wg.get("maxCapacity") == max_rpu
        and wg["status"] == "AVAILABLE"
    ):
        logger.info("workgroup %s already at %d/%d RPU (base/max)", workgroup, target_rpu, max_rpu)
        return

    logger.info(
        "resizing workgroup %s: base %d -> %d, max %s -> %d RPU",
        workgroup,
        wg["baseCapacity"],
        target_rpu,
        wg.get("maxCapacity"),
        max_rpu,
    )
    # UpdateWorkgroup rejects changing base and max in one call ("Can't update
    # multiple configurations at the same time"), so apply them as two ordered
    # steps that keep base <= max at every point: raise max before base when
    # growing; change base before lowering max when shrinking.
    steps: list[dict] = []
    if max_rpu > (wg.get("maxCapacity") or 0):
        if wg.get("maxCapacity") != max_rpu:
            steps.append({"maxCapacity": max_rpu})
        if wg["baseCapacity"] != target_rpu:
            steps.append({"baseCapacity": target_rpu})
    else:
        if wg["baseCapacity"] != target_rpu:
            steps.append({"baseCapacity": target_rpu})
        if wg.get("maxCapacity") != max_rpu:
            steps.append({"maxCapacity": max_rpu})
    for step in steps:
        _apply_workgroup_update(client, workgroup, step)
    logger.info("workgroup AVAILABLE at %d/%d RPU (base/max)", target_rpu, max_rpu)


def _apply_workgroup_update(client, workgroup: str, params: dict) -> None:
    """One UpdateWorkgroup call: retry transient refusals, wait until it lands.

    Two retryable refusals observed live: ConflictException while a prior
    operation settles, and ValidationException "Wait until at least one
    snapshot exists and retry" — downsizing RPU needs a namespace recovery
    point, which Serverless creates automatically every ~30 min.
    """
    conflict_deadline = time.monotonic() + CONFLICT_RETRY_TIMEOUT_S
    while True:
        try:
            client.update_workgroup(workgroupName=workgroup, **params)
            break
        except client.exceptions.ConflictException:
            if time.monotonic() >= conflict_deadline:
                raise
            logger.info(
                "another workgroup operation in progress — retrying in %ds",
                CONFLICT_RETRY_INTERVAL_S,
            )
            time.sleep(CONFLICT_RETRY_INTERVAL_S)
        except client.exceptions.ValidationException as exc:
            if "snapshot exists and retry" not in str(exc) or time.monotonic() >= conflict_deadline:
                raise
            logger.info(
                "downsize needs a namespace recovery point — retrying in %ds",
                CONFLICT_RETRY_INTERVAL_S,
            )
            time.sleep(CONFLICT_RETRY_INTERVAL_S)

    key = next(iter(params))  # baseCapacity | maxCapacity
    deadline = time.monotonic() + RESIZE_TIMEOUT_S
    while time.monotonic() < deadline:
        wg = client.get_workgroup(workgroupName=workgroup)["workgroup"]
        if wg["status"] == "AVAILABLE" and wg.get(key) == params[key]:
            logger.info("applied %s=%d", key, params[key])
            return
        logger.info("workgroup %s (%s %s) — waiting", wg["status"], key, wg.get(key))
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"workgroup did not reach AVAILABLE with {params} in {RESIZE_TIMEOUT_S}s")


def sync_sizing_tfvars(target_rpu: int) -> None:
    """Record the new RPU in the tool-owned sizing tfvars so terraform agrees."""
    path = REPO_ROOT / DEFAULT_SIZING_TFVARS_PATH
    write_sizing_edits([TfvarEdit("redshift", "redshift_baseline_rpu", None, target_rpu)], path)
    logger.info("synced redshift_baseline_rpu=%d -> %s", target_rpu, path)


def run_cli(*args: str) -> None:
    """Run a benchmark CLI command, failing loudly on a non-zero exit."""
    cmd = ["uv", "run", "benchmark", *args]
    logger.info("running: %s", " ".join(cmd))
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=int, required=True, choices=[1, 10, 100, 1000])
    parser.add_argument(
        "--compute-only",
        action="store_true",
        help="Skip prepare-scale's slow data row-count sweep; verify compute only.",
    )
    parser.add_argument(
        "--rpu",
        type=int,
        default=None,
        help="Base-RPU override for compute-size band sweeps (default: the run-plan "
        "target for --scale). prepare-scale will report compute 'pending' when this "
        "differs from the run-plan target — expected during a sweep.",
    )
    parser.add_argument(
        "--max-rpu",
        type=int,
        default=None,
        help="Max-capacity override for non-benchmark work (e.g. bulk loads that "
        "benefit from autoscaling headroom). Default: pinned to the base capacity — "
        "benchmarks always run fixed-size so vertical autoscaling can't add noise. "
        "Terraform likewise pins max == base; a later apply re-pins any override.",
    )
    args = parser.parse_args()

    if args.rpu is not None:
        target_rpu = args.rpu
    else:
        target = get_run_plan().target("redshift", args.scale)
        if target is None:
            raise SystemExit(f"run_plan has no redshift cell for SF{args.scale} — skip this scale")
        target_rpu = int(target)
    max_rpu = args.max_rpu if args.max_rpu is not None else target_rpu
    if max_rpu < target_rpu:
        parser.error(f"--max-rpu {max_rpu} is below the base capacity {target_rpu}")

    resize_workgroup(target_rpu, max_rpu)
    sync_sizing_tfvars(target_rpu)
    run_cli("capture-cost-config", "--platforms", "redshift")
    prepare = ["prepare-scale", "--scale", str(args.scale), "--platforms", "redshift", "--yes"]
    if args.compute_only:
        prepare.append("--compute-only")
    run_cli(*prepare)

    logger.info(
        "redshift ready at SF%d (%d RPU). Next: uv run benchmark sql --scale %d --platforms redshift",
        args.scale,
        target_rpu,
        args.scale,
    )


if __name__ == "__main__":
    main()

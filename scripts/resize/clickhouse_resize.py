"""Resize the ClickHouse Cloud service to a run-plan rung — PINNED, single replica.

ClickHouse Cloud provisions 1 vCPU per 4 GiB and bills per 2-vCPU unit, so the
vendor's capacity dial is MEMORY, not replica count: replicas add throughput and HA
but do NOT pool memory for a single query. The sizing ladder is therefore memory
doublings at ONE replica (``run_plan.yaml`` ``clickhouse.sweep``), which is the same
vertical axis every other engine is measured on.

The service is provisioned out of band (``terraform/modules/clickhouse`` only records
its UUID and manages nothing), so sizing goes through the Cloud API's
``replicaScaling`` endpoint, mirroring the Redshift and MotherDuck out-of-band
pattern.

PINNED means ``minReplicaMemoryGb == maxReplicaMemoryGb`` with ``idleScaling`` off:

  * min == max disables vertical autoscaling, so a size label describes a fixed unit
    of compute for the whole pass; resizing out of band mid-run quarantines every
    row the mislabelled pass wrote.
  * idleScaling off stops the service idling between queries. An idle service
    cold-starts on the next query, which lands as latency in whatever cell happens
    to be running.

THREE API TRAPS, verified live on this service:

  1. ``minTotalMemoryGb`` / ``maxTotalMemoryGb`` are DEPRECATED and echo **3x the
     per-replica value regardless of replica count** — this service reports 48 with
     ``numReplicas: 1`` and 16 GB replicas. Never infer size from the totals. This
     script reads and writes only the ``*ReplicaMemoryGb`` fields.
  2. The service has no ``tier`` field. ``compute_label_from_
     environment`` falls back to ``tier`` only when replicas+memory are missing, so
     the label still resolves — but do not add code that depends on ``tier``.
  3. The GET echoes the **desired** scaling settings, not the live hardware: a
     downsize (e.g. 120->64) can read back as "confirmed" within 2 seconds while
     ``system.asynchronous_metrics`` still reports the old ``CGroupMemoryTotal`` /
     ``CGroupMaxCPU``, so a pass started then runs on the old replica under the
     new label. A pinned resize therefore also polls the HARDWARE WITNESS over SQL
     and succeeds only when the replica's cgroup matches the rung.

BILLING. ``idleScaling`` off means the service bills continuously, not just while
queries run. That is correct during a pass and expensive if a campaign aborts and
nobody notices. Restore idling when the sweep is done:

    uv run python scripts/resize/clickhouse_resize.py --restore-idle

Usage:
    uv run python scripts/resize/clickhouse_resize.py --validate-ladder   # read-only
    uv run python scripts/resize/clickhouse_resize.py --label 1x32GB
    uv run python scripts/resize/clickhouse_resize.py --memory-gb 64 --replicas 1
    uv run python scripts/resize/clickhouse_resize.py --restore-idle
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.request
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

logger = logging.getLogger("benchmark.scripts.clickhouse_resize")

API_ROOT = "https://api.clickhouse.cloud/v1"
POLL_INTERVAL_S = 10
RESIZE_TIMEOUT_S = 900

#: ClickHouse Cloud provisions 1 vCPU per 4 GiB, so per-replica memory must be a
#: multiple of 4. The floor is the smallest rung the vendor offers; the ceiling is
#: the largest rung VERIFIED on this service (a 120 GB PATCH returned 200 and the
#: follow-up GET confirmed it). A rung above the ceiling is not refused
#: here — the API is the authority — but it is flagged, because a mid-campaign
#: rejection is far more expensive than a warning now.
MEMORY_STEP_GB = 4
MEMORY_MIN_GB = 8
MEMORY_VERIFIED_MAX_GB = 120

#: ``1x32GB`` -> (1, 32). The label form is what the runners stamp as compute_label
#: and what run_plan's sweep declares, so a sweep driver can pass rungs through
#: verbatim rather than re-deriving them.
LABEL_RE = re.compile(r"^(\d+)x(\d+)GB$")


def _request(path: str, payload: dict | None = None, method: str | None = None) -> dict:
    """Call the Cloud API. GET by default; PATCH when a payload is given."""
    ch = get_settings().CLICKHOUSE
    if not (ch.ORG_ID and ch.SERVICE_ID and ch.API_KEY_ID and ch.API_KEY_SECRET):
        raise SystemExit("CLICKHOUSE_ORG_ID / SERVICE_ID / API_KEY_ID / API_KEY_SECRET must be set")
    creds = base64.b64encode(
        f"{ch.API_KEY_ID}:{ch.API_KEY_SECRET.get_secret_value()}".encode()
    ).decode()
    url = f"{API_ROOT}/organizations/{ch.ORG_ID}/services/{ch.SERVICE_ID}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/json"},
        method=method or ("PATCH" if payload is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        logger.error("Cloud API %s %s -> HTTP %s: %s", req.method, path or "/", exc.code, detail)
        raise
    doc = json.loads(body) if body else {}
    return doc.get("result", doc)


def _sizing(svc: dict) -> tuple[int | None, int | None, int | None, bool | None, str | None]:
    """(replicas, min_gb, max_gb, idle_scaling, state) — per-replica fields ONLY."""
    return (
        svc.get("numReplicas"),
        svc.get("minReplicaMemoryGb"),
        svc.get("maxReplicaMemoryGb"),
        svc.get("idleScaling"),
        svc.get("state"),
    )


def _describe(svc: dict) -> str:
    reps, lo, hi, idle, state = _sizing(svc)
    mem = f"{lo}GB" if lo == hi else f"{lo}-{hi}GB"
    return f"{reps}x{mem} idleScaling={idle} state={state}"


def validate_ladder() -> int:
    """Check the declared ladder against the arithmetic and the live service.

    Runs before any resize so a campaign fails here, in a second, rather than four
    hours in on the rung the API refuses.
    """
    svc = _request("")
    logger.info("live service: %s", _describe(svc))
    reps, _, _, _, _ = _sizing(svc)
    if svc.get("minTotalMemoryGb") is not None:
        logger.info(
            "ignoring deprecated totals (%s/%s) — they echo 3x per-replica regardless "
            "of replica count",
            svc.get("minTotalMemoryGb"),
            svc.get("maxTotalMemoryGb"),
        )

    ladder = get_run_plan().sweep_ladder("clickhouse")
    if not ladder:
        logger.error("run_plan declares no clickhouse sweep ladder")
        return 1

    bad = 0
    for rung in ladder:
        m = LABEL_RE.match(str(rung))
        if not m:
            logger.error("rung %r is not a <replicas>x<memory>GB label", rung)
            bad += 1
            continue
        want_reps, gb = int(m.group(1)), int(m.group(2))
        notes = []
        if gb % MEMORY_STEP_GB:
            notes.append(f"not a multiple of {MEMORY_STEP_GB}GB (1 vCPU per 4 GiB)")
            bad += 1
        if gb < MEMORY_MIN_GB:
            notes.append(f"below the {MEMORY_MIN_GB}GB floor")
            bad += 1
        if gb > MEMORY_VERIFIED_MAX_GB:
            notes.append(f"above the verified {MEMORY_VERIFIED_MAX_GB}GB ceiling — UNVERIFIED")
        if want_reps != 1:
            notes.append(f"{want_reps} replicas — the sizing axis is single-replica")
        logger.info("  %-10s %s", rung, "; ".join(notes) if notes else "ok")

    if reps != 1:
        logger.warning(
            "service is at %s replicas; a single-replica rung will reduce it in place", reps
        )
    if bad:
        logger.error("%d rung(s) violate the vendor's memory arithmetic", bad)
        return 1
    logger.info("ladder ok: %s", ", ".join(str(r) for r in ladder))
    return 0


def _hardware_witness(memory_gb: int, wait_s: int) -> int:
    """Poll the replica's cgroup over SQL until it matches the rung (API trap 3).

    The Cloud API's GET reflects desired state; only the running server can say what
    hardware a pass will actually land on. Reconnects every poll because a replica
    replacement drops connections — a connect failure means "still replacing", not
    an error.
    """
    import clickhouse_connect

    ch = get_settings().CLICKHOUSE
    want_bytes = memory_gb * 1024**3
    deadline = time.time() + wait_s
    last = None
    while time.time() < deadline:
        try:
            client = clickhouse_connect.get_client(
                host=ch.HOST,
                port=8443,
                username=ch.USER,
                password=ch.PASSWORD.get_secret_value(),
                secure=True,
                connect_timeout=10,
            )
            rows = dict(
                client.query(
                    "SELECT metric, value FROM system.asynchronous_metrics "
                    "WHERE metric IN ('CGroupMemoryTotal', 'CGroupMaxCPU')"
                ).result_rows
            )
            client.close()
            got = int(rows.get("CGroupMemoryTotal", 0))
            cpus = rows.get("CGroupMaxCPU")
            if got == want_bytes:
                logger.info(
                    "hardware witness ok: CGroupMemoryTotal=%sGiB CGroupMaxCPU=%s",
                    got // 1024**3,
                    cpus,
                )
                return 0
            last = f"CGroupMemoryTotal={got // 1024**3}GiB CGroupMaxCPU={cpus}"
        except Exception as exc:
            last = f"unreachable ({type(exc).__name__}) — replica likely being replaced"
        logger.info("hardware witness waiting for %sGiB: %s", memory_gb, last)
        time.sleep(POLL_INTERVAL_S)

    logger.error(
        "hardware never matched %sGiB within %ss (last: %s) — the API accepted the "
        "resize but the replica was not replaced; do NOT run a pass at this label",
        memory_gb,
        wait_s,
        last,
    )
    return 1


def resize(replicas: int, memory_gb: int, *, idle_scaling: bool, wait_s: int) -> int:
    """PATCH replicaScaling to a pinned config; confirm via the GET AND, for a
    benchmark pin (idling off), via the hardware witness (API trap 3)."""
    before = _request("")
    logger.info("before: %s", _describe(before))
    reps, lo, hi, idle, _ = _sizing(before)
    if (reps, lo, hi, idle) == (replicas, memory_gb, memory_gb, idle_scaling):
        logger.info("already at %sx%sGB pinned (idleScaling=%s)", replicas, memory_gb, idle_scaling)
        _sync_tfvars(replicas)
        if not idle_scaling:
            return _hardware_witness(memory_gb, wait_s)
        return 0

    payload = {
        "numReplicas": replicas,
        "minReplicaMemoryGb": memory_gb,
        "maxReplicaMemoryGb": memory_gb,
        "idleScaling": idle_scaling,
    }
    logger.info("PATCH replicaScaling %s", payload)
    _request("/replicaScaling", payload)

    deadline = time.time() + wait_s
    while time.time() < deadline:
        cur = _request("")
        c_reps, c_lo, c_hi, c_idle, _ = _sizing(cur)
        if (c_reps, c_lo, c_hi, c_idle) == (replicas, memory_gb, memory_gb, idle_scaling):
            logger.info("resize confirmed by API: %s", _describe(cur))
            _sync_tfvars(replicas)
            if not idle_scaling:
                return _hardware_witness(memory_gb, int(max(deadline - time.time(), 60)))
            return 0
        logger.info("waiting: %s", _describe(cur))
        time.sleep(POLL_INTERVAL_S)

    logger.error("resize to %sx%sGB not confirmed within %ss", replicas, memory_gb, wait_s)
    return 1


def _sync_tfvars(replicas: int) -> None:
    """Keep terraform's replica count in step so an apply doesn't revert it.

    Only the replica count is a terraform var — memory is not, so the sizing file
    cannot round-trip a rung on its own. That asymmetry is why the label the runners
    stamp is read back from the live service rather than from tfvars.
    """
    edit = TfvarEdit(
        platform="clickhouse",
        tfvar="clickhouse_num_replicas",
        old=None,
        new=replicas,
    )
    write_sizing_edits([edit], DEFAULT_SIZING_TFVARS_PATH)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--validate-ladder", action="store_true", help="read-only ladder check")
    g.add_argument("--label", help="rung as the runners label it, e.g. 1x32GB")
    g.add_argument("--memory-gb", type=int, help="per-replica memory in GB")
    g.add_argument(
        "--restore-idle",
        action="store_true",
        help="re-enable idleScaling at the current size (run when a sweep is done — "
        "a pinned service bills continuously)",
    )
    ap.add_argument("--replicas", type=int, default=1, help="replica count (default 1)")
    ap.add_argument("--wait", type=int, default=RESIZE_TIMEOUT_S)
    ap.add_argument(
        "--idle-scaling",
        choices=("on", "off"),
        default="off",
        help="benchmarks run with idling OFF so a cold start can't land in a cell",
    )
    args = ap.parse_args()
    configure_logging("INFO")

    if args.validate_ladder:
        return validate_ladder()

    if args.restore_idle:
        svc = _request("")
        reps, lo, hi, _, _ = _sizing(svc)
        if lo != hi:
            logger.error("service is not pinned (%s-%sGB); refusing to guess a size", lo, hi)
            return 1
        logger.info("restoring idleScaling at %sx%sGB", reps, lo)
        return resize(int(reps), int(lo), idle_scaling=True, wait_s=args.wait)

    if args.label:
        m = LABEL_RE.match(args.label)
        if not m:
            logger.error("--label must look like 1x32GB, got %r", args.label)
            return 1
        replicas, memory_gb = int(m.group(1)), int(m.group(2))
    else:
        replicas, memory_gb = args.replicas, args.memory_gb

    if memory_gb % MEMORY_STEP_GB or memory_gb < MEMORY_MIN_GB:
        logger.error(
            "%sGB is not a valid per-replica size (multiples of %sGB, min %sGB)",
            memory_gb,
            MEMORY_STEP_GB,
            MEMORY_MIN_GB,
        )
        return 1

    return resize(replicas, memory_gb, idle_scaling=args.idle_scaling == "on", wait_s=args.wait)


if __name__ == "__main__":
    sys.exit(main())

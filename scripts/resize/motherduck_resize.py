"""Set the MotherDuck Duckling size — ACCOUNT-level, via the REST API.

The Duckling size (pulse | standard | jumbo | mega | giga) is a property of the
service account, not of a connection or session, and our Terraform module for
MotherDuck is a no-op placeholder (no first-party provider). So sizing is
out-of-band, the Redshift/ClickHouse pattern:

    GET  https://api.motherduck.com/v1/users/{user}/instances
    PUT  https://api.motherduck.com/v1/users/{user}/instances
         {"config": {"read_write": {"instance_size": "<size>", ...}}}

Pulse rejects ``cooldown_seconds`` (its cooldown is fixed at 400s), every other
tier accepts 60-86400s; the PUT body must wrap the config in ``config``.

The PUT is accepted immediately but the flock re-provisions behind it: a pass
started right after ``standard -> mega`` can resolve its label from
``md_live_duckling_size()`` as Standard and write a mislabelled cell. So
``--size`` also WAITS, polling the live size from a fresh connection until it
reports the target (or ``--wait-seconds`` run out, exit 1). The per-row label
check after a pass (``check_compute_labels.py``) remains the final assertion of
what actually served.

Usage:
    uv run python scripts/resize/motherduck_resize.py --size giga
    uv run python scripts/resize/motherduck_resize.py --show
    uv run python scripts/resize/motherduck_resize.py --live   # what is serving now
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.request

from benchmark.logging_config import configure_logging
from benchmark.settings import get_settings

logger = logging.getLogger("benchmark.scripts.motherduck_resize")

API = "https://api.motherduck.com/v1/users/{user}/instances"
SIZES = ("pulse", "standard", "jumbo", "mega", "giga")
COOLDOWN_SECONDS = 60

#: How long to wait for the live duckling to report the requested size.
DEFAULT_WAIT_SECONDS = 900
POLL_SECONDS = 15


def _request(method: str, body: dict | None = None) -> dict:
    md = get_settings().MOTHERDUCK
    if md.TOKEN is None:
        raise SystemExit("MOTHERDUCK_TOKEN is not configured")
    if not md.ACCOUNT_USER:
        raise SystemExit("MOTHERDUCK_ACCOUNT_USER is not configured (the token owner's username)")
    req = urllib.request.Request(
        API.format(user=md.ACCOUNT_USER),
        method=method,
        headers={
            "Authorization": f"Bearer {md.TOKEN.get_secret_value()}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def current_size() -> str:
    cfg = _request("GET")
    return str(cfg.get("read_write", {}).get("instance_size", "?"))


def set_size(size: str) -> int:
    """PUT the read_write instance size; read_scaling is left as configured."""
    before = _request("GET")
    if before.get("read_write", {}).get("instance_size") == size:
        logger.info("duckling already %s", size)
        return 0
    read_write: dict = {"instance_size": size}
    if size != "pulse":  # pulse rejects cooldown_seconds (fixed 400s)
        read_write["cooldown_seconds"] = COOLDOWN_SECONDS
    body = {"config": {"read_write": read_write, "read_scaling": before.get("read_scaling")}}
    _request("PUT", body)
    after = current_size()
    if after != size:
        logger.error("PUT accepted but config reads %s, wanted %s", after, size)
        return 1
    logger.info("duckling %s -> %s", before.get("read_write", {}).get("instance_size"), size)
    return 0


def live_size() -> str | None:
    """The size actually serving right now, from a fresh connection.

    ``md_live_duckling_size()`` describes the instance a connection landed on,
    which is what a benchmark pass labels its rows with; the account config is
    only what was asked for.
    """
    from benchmark.connectors.registry import create_connector

    connector = create_connector("motherduck", config={})
    with connector:
        size = connector.runtime_compute().get("size")
    return str(size).lower() if size else None


def wait_for_live(size: str, timeout_s: int) -> int:
    """Poll until the live duckling reports ``size``; exit 1 if it never does."""
    deadline = time.monotonic() + timeout_s
    while True:
        seen = live_size()
        if seen == size:
            logger.info("live duckling is %s", size)
            return 0
        if time.monotonic() >= deadline:
            logger.error("live duckling still %s after %ds; wanted %s", seen, timeout_s, size)
            return 1
        logger.info("live duckling is %s, waiting for %s", seen, size)
        time.sleep(POLL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--size", choices=SIZES, help="Set the read_write Duckling size and wait for it to serve."
    )
    group.add_argument("--show", action="store_true", help="Print the current config.")
    group.add_argument("--live", action="store_true", help="Print the size actually serving.")
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=DEFAULT_WAIT_SECONDS,
        help="How long --size waits for the live size to match (default %(default)s).",
    )
    args = parser.parse_args()
    configure_logging()
    if args.show:
        logger.info("current config: %s", json.dumps(_request("GET")))
        return 0
    if args.live:
        logger.info("live duckling: %s", live_size())
        return 0
    if set_size(args.size) != 0:
        return 1
    return wait_for_live(args.size, args.wait_seconds)


if __name__ == "__main__":
    sys.exit(main())

"""Redshift concurrency sweep orchestrator: scale x RPU band x level grid.

Walks the full grid (scales x pinned RPU bands x concurrency levels), one
`benchmark concurrency` invocation per level, with a **timeout ejection
policy**: if a level records more than ``--max-timeouts`` timed-out queries,
the (scale, band) cell is ejected — its higher levels are skipped and the sweep
moves UP to the next band (more compute may clear the timeouts); when the
largest band ejects the sweep naturally moves ON to the next scale. Completed
levels of an ejected cell remain valid recorded data.

Each cell resizes via scripts/resize/redshift_resize.py (pinned max == base, re-runs
capture-cost-config so the band is priced) before its levels run. Rows are
stamped with the "<B> RPU" compute label, so no grid cell collides.

Usage:
    uv run python scripts/sweeps/redshift_sweep.py                      # full grid
    uv run python scripts/sweeps/redshift_sweep.py --scales 1 --bands 8,16
    uv run python scripts/sweeps/redshift_sweep.py --scales 100,1000 --max-timeouts 5

Run `benchmark reconcile` the same day as each scale's cells finish —
SYS_SERVERLESS_USAGE retains 7 days.

Before the first cell the sweep checks that SYS_QUERY_HISTORY is being exported to
S3 Tables (Redshift's system-table integration), so queue and compile time outlive
the 7-day window, and that the AWS CLI is new enough to read that setting
(MIN_AWS_CLI_VERSION). It exits 2 if either is missing; --skip-export-check
overrides. Enable the export with:

    aws redshift-serverless update-namespace --namespace-name <namespace> \
        --log-destination-type s3table --s3-table-action Enable \
        --s3-table-names sys_query_history --s3-table-granularity namespace
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

logger = logging.getLogger("benchmark.scripts.redshift_sweep")

RESULTS_DB = REPO_ROOT / "results" / "benchmark.duckdb"
DEFAULT_SCALES = (1, 10, 100, 1000)
DEFAULT_BANDS = (8, 16, 32, 64, 128)
DEFAULT_LEVELS = (1, 5, 10, 20, 50, 100)
TIMEOUT_ERROR_PREFIX = "query exceeded timeout"  # runner.execute_with_timeout

#: First AWS CLI release with Redshift's system-table export to S3 Tables (the
#: ``--log-destination-type s3table`` options of update-namespace; "Amazon Redshift
#: enhanced System Table retention" in the CLI changelog). The project's boto3 model
#: predates the feature and drops the status fields, so the preflight reads them
#: through the CLI, and an older CLI cannot see them either.
MIN_AWS_CLI_VERSION = (2, 35, 22)
#: System tables whose history must outlive Redshift's 7-day retention.
EXPORTED_SYSTEM_TABLES = ("sys_query_history",)


def _run(cmd: list[str]) -> int:
    logger.info("running: %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=REPO_ROOT, check=False).returncode


def aws_cli_version() -> tuple[int, ...] | None:
    """The installed AWS CLI's version, or None when it is missing or unparseable."""
    try:
        out = subprocess.run(["aws", "--version"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    m = re.search(r"aws-cli/(\d+)\.(\d+)\.(\d+)", out.stdout + out.stderr)
    return tuple(int(x) for x in m.groups()) if m else None


def _aws_json(args: list[str], profile: str) -> dict:
    out = subprocess.run(
        ["aws", *args, "--profile", profile, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:2])} failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def exported_system_tables(workgroup: str, profile: str) -> tuple[str, set[str]]:
    """(namespace name, system tables it exports to S3 Tables) for ``workgroup``.

    ``{"all"}`` when the namespace exports every supported table.
    """
    namespace = _aws_json(
        ["redshift-serverless", "get-workgroup", "--workgroup-name", workgroup], profile
    )["workgroup"]["namespaceName"]
    ns = _aws_json(
        ["redshift-serverless", "get-namespace", "--namespace-name", namespace], profile
    )["namespace"]
    status = ns.get("s3TablePublishStatus") or {}
    if status.get("enabledAll"):
        return namespace, {"all"}
    return namespace, {str(t).lower() for t in status.get("s3Tables") or []}


def export_preflight(workgroup: str | None, profile: str) -> list[str]:
    """Problems that should stop the sweep before its first cell; empty when ready."""
    version = aws_cli_version()
    if version is None:
        return ["the AWS CLI is not installed or its version could not be read"]
    if version < MIN_AWS_CLI_VERSION:
        need = ".".join(map(str, MIN_AWS_CLI_VERSION))
        have = ".".join(map(str, version))
        return [f"AWS CLI {have} predates the S3 Tables export options; {need} or later is needed"]
    if not workgroup:
        return ["REDSHIFT_WORKGROUP is not set"]
    namespace, tables = exported_system_tables(workgroup, profile)
    missing = [t for t in EXPORTED_SYSTEM_TABLES if "all" not in tables and t not in tables]
    if missing:
        return [f"namespace {namespace} does not export {', '.join(missing)} to S3 Tables"]
    return []


def _cell_filter(run_type: str, level: int | None) -> tuple[str, list]:
    """(WHERE fragment, params) for one grid cell: concurrency cells key on the
    level; warm cells have concurrency IS NULL."""
    base = "platform = 'redshift' AND run_type = ? AND scale_factor = ? AND compute_label = ?"
    if level is not None:
        return base + " AND concurrency = ?", [run_type]
    return base + " AND concurrency IS NULL", [run_type]


def _cell_params(run_type: str, scale: int, band: int, level: int | None) -> list:
    params: list = [run_type, scale, f"{band} RPU"]
    if level is not None:
        params.append(level)
    return params


def _cell_watermark(run_type: str, scale: int, band: int, level: int | None) -> str:
    """DB-side created_at high-water mark for a cell (avoids comparing a
    Python clock against DuckDB's session-local naive ``created_at``)."""
    where, _ = _cell_filter(run_type, level)
    con = duckdb.connect(str(RESULTS_DB), read_only=True)
    try:
        row = con.execute(
            f"SELECT coalesce(max(created_at), TIMESTAMP '1970-01-01') "
            f"FROM sql_benchmark_results WHERE {where}",
            _cell_params(run_type, scale, band, level),
        ).fetchone()
        return str(row[0])
    finally:
        con.close()


def _timeouts_since(
    run_type: str, scale: int, band: int, level: int | None, watermark: str
) -> tuple[int, int]:
    """(timed_out, total) rows for this cell after ``watermark``."""
    where, _ = _cell_filter(run_type, level)
    con = duckdb.connect(str(RESULTS_DB), read_only=True)
    try:
        row = con.execute(
            f"""
            SELECT
              count(*) FILTER (WHERE error_message LIKE ? || '%'),
              count(*)
            FROM sql_benchmark_results
            WHERE {where} AND created_at > CAST(? AS TIMESTAMP)
            """,
            [TIMEOUT_ERROR_PREFIX, *_cell_params(run_type, scale, band, level), watermark],
        ).fetchone()
        return int(row[0]), int(row[1])
    finally:
        con.close()


def sweep_cell(
    scale: int,
    band: int,
    levels: tuple[int, ...],
    max_timeouts: int,
    blocks: str,
    run_type: str,
) -> bool:
    """Run one (scale, band) cell. Returns False if ejected.

    ``run_type='concurrency'`` runs level-by-level; ``'warm'`` is a single
    ``benchmark sql`` pass (1 warmup + 3 measured, ``--force`` so a band after
    the first isn't skipped by resume — rows are append-only and the report
    dedups size-aware by compute_label).
    """
    rc = _run(
        [
            "uv",
            "run",
            "python",
            "scripts/resize/redshift_resize.py",
            "--scale",
            str(scale),
            "--rpu",
            str(band),
            "--compute-only",
        ]
    )
    if rc != 0:
        logger.error("resize to %d RPU failed (exit %d) — skipping cell", band, rc)
        return False

    cell_levels: tuple[int | None, ...] = levels if run_type == "concurrency" else (None,)
    for level in cell_levels:
        watermark = _cell_watermark(run_type, scale, band, level)
        if run_type == "concurrency":
            assert level is not None
            cmd = [
                "uv",
                "run",
                "benchmark",
                "concurrency",
                "--scale",
                str(scale),
                "--platforms",
                "redshift",
                "--blocks",
                blocks,
                "--levels",
                str(level),
            ]
        else:
            cmd = [
                "uv",
                "run",
                "benchmark",
                "sql",
                "--scale",
                str(scale),
                "--platforms",
                "redshift",
                "--blocks",
                blocks,
                "--force",
            ]
        rc = _run(cmd)
        label = f"level {level}" if level is not None else "warm pass"
        if rc != 0:
            logger.error(
                "%s SF%d %d RPU %s exited %d — ejecting cell", run_type, scale, band, label, rc
            )
            return False
        timed_out, total = _timeouts_since(run_type, scale, band, level, watermark)
        logger.info("SF%d %d RPU %s: %d/%d rows timed out", scale, band, label, timed_out, total)
        if timed_out > max_timeouts:
            logger.warning(
                "EJECT SF%d %d RPU at %s (%d timeouts > %d allowed) — moving up a band",
                scale,
                band,
                label,
                timed_out,
                max_timeouts,
            )
            return False
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scales", default=",".join(map(str, DEFAULT_SCALES)))
    parser.add_argument("--bands", default=",".join(map(str, DEFAULT_BANDS)))
    parser.add_argument("--levels", default=",".join(map(str, DEFAULT_LEVELS)))
    parser.add_argument(
        "--blocks",
        default="tpcds,dsb",
        help="Query blocks per cell (concurrency default). ClickBench is single-scale: "
        "run it as its own sweep, `--scales 1 --blocks clickbench` (the clickhouse "
        "precedent records ClickBench concurrency at scale_factor=1).",
    )
    parser.add_argument(
        "--max-timeouts",
        type=int,
        default=0,
        help="Timed-out queries tolerated per level before the cell ejects (default 0: "
        "any timeout ejects — timeouts burn the full per-query timeout in RPU-hours).",
    )
    parser.add_argument(
        "--run-type",
        choices=["concurrency", "warm"],
        default="concurrency",
        help="'concurrency' walks --levels per cell; 'warm' runs one benchmark-sql "
        "pass per cell (1 warmup + 3 measured, --force so later bands aren't "
        "resume-skipped) — the report's warm/efficiency sections read these rows.",
    )
    parser.add_argument(
        "--skip-export-check",
        action="store_true",
        help="Run without the S3 Tables export of SYS_QUERY_HISTORY; its queue time is "
        "then lost 7 days after the run.",
    )
    args = parser.parse_args()
    if not args.skip_export_check:
        from benchmark.settings import get_settings

        settings = get_settings()
        problems = export_preflight(settings.REDSHIFT.WORKGROUP, settings.AWS.PROFILE)
        for problem in problems:
            logger.error("preflight: %s", problem)
        if problems:
            sys.exit(2)
        logger.info("preflight: SYS_QUERY_HISTORY is exported to S3 Tables")
    scales = tuple(int(s) for s in args.scales.split(","))
    bands = tuple(int(b) for b in args.bands.split(","))
    levels = tuple(int(le) for le in args.levels.split(","))

    outcomes: dict[tuple[int, int], bool] = {}
    for scale in scales:
        for band in bands:
            outcomes[(scale, band)] = sweep_cell(
                scale, band, levels, args.max_timeouts, args.blocks, args.run_type
            )
    for (scale, band), ok in outcomes.items():
        logger.info("SF%d %d RPU: %s", scale, band, "complete" if ok else "EJECTED")
    logger.info("sweep done — run `uv run benchmark reconcile` today (7-day usage window)")


if __name__ == "__main__":
    main()

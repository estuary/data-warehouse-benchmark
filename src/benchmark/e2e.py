"""End-to-end loading + benchmark orchestration (the real cloud path).

Chains the production sequence for one or more engines, in order:

    generate (tpcds/dsb; clickbench = download + derive-typed)
    -> ground truth (validate-queries for tpcds/dsb; hash-clickbench-ground-truth)
    -> generate-checksums -> generate-expected-counts
    -> stage-data (S3) -> Estuary capture publish (full spec, once)
    -> [per engine] materialization publish (full spec, once)
    -> validate-loads (data-existence check in the target, scoped by suite)
    -> capture-cost-config (skipped when results/cost_config.json exists)
    -> [per engine] sql benchmark
    -> analyze -> report

Each step shells out to the corresponding ``benchmark`` subcommand — the same
commands an operator runs by hand — so this is a faithful, ordered *driver*, not
a reimplementation. ``build_plan`` is pure and unit-tested, so the ordering and
per-engine fan-out are verified without credentials.

A real run needs: populated ``env/<section>.env``, terraform-provisioned infra,
``benchmark flow-auth`` (Estuary token), and reachable warehouses. See
``docs/E2E_LOADING.md``.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .cost.discovery import DEFAULT_COST_CONFIG_PATH

logger = logging.getLogger("benchmark.e2e")

DEFAULT_BLOCKS: tuple[str, ...] = ("tpcds", "dsb", "clickbench")
# How each step is invoked. `uv run` so it works without an activated venv.
RUNNER: tuple[str, ...] = ("uv", "run", "benchmark")

# Default seconds validate-loads polls for the Estuary backfill to catch up
# after a materialization publish (the publish returns before tables exist /
# finish loading). Generous enough for an SF1000 backfill; overridable via the
# e2e --load-timeout flag.
DEFAULT_LOAD_TIMEOUT: int = 3600


@dataclass(frozen=True)
class Step:
    """One ordered step in the E2E plan."""

    key: str  # phase key, for --from / --skip (stable across engines)
    name: str  # display name (e.g. "materialize:snowflake")
    argv: tuple[str, ...]  # the `benchmark` subcommand + flags
    scope: str  # "shared" | "engine" | "final"
    # When set, run_e2e skips the step if this file already exists (the
    # capture-cost-config step, whose output the sql step reads).
    skip_if_exists: Path | None = None


def build_plan(
    platforms: Sequence[str],
    scale: int,
    blocks: Iterable[str] = DEFAULT_BLOCKS,
    *,
    load_timeout: int = DEFAULT_LOAD_TIMEOUT,
    recapture_cost_config: bool = False,
) -> list[Step]:
    """Build the ordered E2E step plan. Pure: no side effects.

    ``recapture_cost_config`` makes the capture-cost-config step unconditional;
    otherwise it carries ``skip_if_exists`` and runs only when the cost-config
    file is missing.
    """
    blocks = tuple(blocks)
    plan: list[Step] = []

    # --- shared: generate + stage + capture (once per scale, not per engine) ---
    if "tpcds" in blocks:
        plan.append(
            Step(
                "generate-tpcds",
                "generate-tpcds",
                ("generate-tpcds", "--scales", str(scale)),
                "shared",
            )
        )
    if "dsb" in blocks:
        plan.append(
            Step("generate-dsb", "generate-dsb", ("generate-dsb", "--scales", str(scale)), "shared")
        )
    if "clickbench" in blocks:
        plan.append(
            Step("download-clickbench", "download-clickbench", ("download-clickbench",), "shared")
        )
        # Derive the typed parquet (Date32 + RFC-3339 timestamp casts) that
        # stage-data uploads and the capture binds — this also persists the
        # committed collection schema. Without it the typed hits.parquet never
        # exists, so ClickBench never stages and the capture drops its binding.
        plan.append(
            Step(
                "derive-clickbench",
                "derive-clickbench-typed",
                ("derive-clickbench-typed",),
                "shared",
            )
        )

    # Ground-truth hashes/rows so validate-sf1 can verify correctness (the
    # runners record latency/cost only). TPC-DS/DSB hash against their
    # local SF<scale> parquet; ClickBench against its raw parquet (keyed at
    # SF100, scale-invariant).
    gt_suites = [b for b in blocks if b in ("tpcds", "dsb")]
    if gt_suites:
        plan.append(
            Step(
                "validate-queries",
                "validate-queries",
                ("validate-queries", "--suites", ",".join(gt_suites), "--scale", str(scale)),
                "shared",
            )
        )
    if "clickbench" in blocks:
        plan.append(
            Step(
                "hash-clickbench-gt",
                "hash-clickbench-ground-truth",
                ("hash-clickbench-ground-truth",),
                "shared",
            )
        )

    plan.append(Step("checksums", "generate-checksums", ("generate-checksums",), "shared"))
    plan.append(
        Step("expected-counts", "generate-expected-counts", ("generate-expected-counts",), "shared")
    )
    plan.append(Step("stage", "stage-data", ("stage-data", "--suites", ",".join(blocks)), "shared"))
    # Publish the FULL capture (every suite × scale binding), once. Captures are
    # declarative + stateful and source-s3 runs continuously: each scale's data
    # flows as it lands in S3, so there is no per-run republish (a targeted
    # change goes through a scoped `flow-publish`, which updates only the named
    # specs; only a `--backfill` bump re-reads history). Data presence is a
    # runtime fact, checked in the target by validate-loads below.
    plan.append(Step("capture", "capture", ("flow-publish", "source-s3"), "shared"))

    # --- per engine: materialize (S3 collections -> warehouse), full spec, once
    #     (same define-once rationale as the capture) ---
    for p in platforms:
        plan.append(
            Step(
                "materialize",
                f"materialize:{p}",
                ("flow-publish", f"estuary/materializations/{p}.flow.yaml"),
                "engine",
            )
        )

    # --- shared: row-count validation (pass the engines + suites explicitly;
    #     the command otherwise defaults to motherduck / all suites) ---
    # The materialization publish above is asynchronous: Estuary creates the
    # target tables and backfills after the publish returns. So validate-loads
    # waits (polls) for the load to catch up rather than racing it — without the
    # wait, the very next step fails on tables that don't exist yet.
    plan.append(
        Step(
            "validate-loads",
            "validate-loads",
            (
                "validate-loads",
                "--platforms",
                ",".join(platforms),
                "--scale",
                str(scale),
                "--suites",
                ",".join(blocks),
                "--wait",
                str(load_timeout),
            ),
            "shared",
        )
    )

    # --- shared: cost config. `sql` exits 2 without results/cost_config.json, so
    #     capture it before the first sql step. It introspects each live engine's
    #     compute, so it runs after validate-loads, by which point every engine has
    #     answered. A file already there (the repo ships a baseline) is kept unless
    #     the run asks for a recapture, which merges into it. ---
    plan.append(
        Step(
            "cost-config",
            "capture-cost-config",
            ("capture-cost-config", "--platforms", ",".join(platforms)),
            "shared",
            skip_if_exists=None if recapture_cost_config else DEFAULT_COST_CONFIG_PATH,
        )
    )

    # --- per engine: SQL benchmark ---
    for p in platforms:
        plan.append(
            Step(
                "sql",
                f"sql:{p}",
                ("sql", "--scale", str(scale), "--platforms", p, "--blocks", ",".join(blocks)),
                "engine",
            )
        )

    # --- final: analysis + report (once) ---
    plan.append(Step("analyze", "analyze", ("analyze",), "final"))
    plan.append(Step("report", "report", ("report",), "final"))
    return plan


def filter_plan(
    plan: Sequence[Step], *, from_key: str | None = None, skip: Iterable[str] = ()
) -> list[Step]:
    """Resume from a phase (``from_key``) and/or drop phases (``skip``), by key."""
    skip = set(skip)
    steps = list(plan)
    if from_key is not None:
        keys = [s.key for s in steps]
        if from_key not in keys:
            raise ValueError(f"unknown --from step {from_key!r}; valid: {sorted(set(keys))}")
        steps = steps[keys.index(from_key) :]
    return [s for s in steps if s.key not in skip]


def run_e2e(
    platforms: Sequence[str],
    scale: int,
    blocks: Iterable[str] = DEFAULT_BLOCKS,
    *,
    from_key: str | None = None,
    skip: Iterable[str] = (),
    keep_going: bool = False,
    load_timeout: int = DEFAULT_LOAD_TIMEOUT,
    recapture_cost_config: bool = False,
    runner: Sequence[str] = RUNNER,
) -> list[tuple[str, str]]:
    """Run the ordered E2E plan, shelling each step to ``benchmark``.

    Returns ``[(step_name, status)]`` where status is ok, failed or skipped
    (not run: an earlier step failed, or the step's ``skip_if_exists`` file is
    already there). Stops at the first failed step unless ``keep_going``
    (loading must succeed before the dependent steps are meaningful).
    """
    plan = filter_plan(
        build_plan(
            platforms,
            scale,
            blocks,
            load_timeout=load_timeout,
            recapture_cost_config=recapture_cost_config,
        ),
        from_key=from_key,
        skip=skip,
    )
    logger.info(
        "E2E plan: %d steps | platforms=%s scale=%d blocks=%s",
        len(plan),
        ",".join(platforms),
        scale,
        ",".join(blocks),
    )
    results: list[tuple[str, str]] = []
    aborted = False
    for step in plan:
        if aborted:
            results.append((step.name, "skipped"))
            continue
        if step.skip_if_exists is not None and step.skip_if_exists.exists():
            logger.info(
                "E2E skip %s: %s already exists, kept as is", step.name, step.skip_if_exists
            )
            results.append((step.name, "skipped"))
            continue
        logger.info("E2E ▶ %s: benchmark %s", step.name, " ".join(step.argv))
        t0 = time.perf_counter()
        proc = subprocess.run([*runner, *step.argv])
        dt = time.perf_counter() - t0
        ok = proc.returncode == 0
        logger.info("E2E %s %s (%.1fs)", "✓" if ok else "✗", step.name, dt)
        results.append((step.name, "ok" if ok else "failed"))
        if not ok and not keep_going:
            logger.error("E2E aborting at failed step %s (use --keep-going to continue)", step.name)
            aborted = True
    return results

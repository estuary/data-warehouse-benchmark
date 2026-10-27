#!/usr/bin/env python

"""Unified Typer CLI — `uv run benchmark <command>`.

Covers the whole pipeline (query suite, data generation, infra wiring, loads,
SQL benchmark with warm/cold/concurrency passes, analysis, and report
generation). Every ``--platforms`` option takes a comma list of engine ids or
``all`` (the seven engines in canonical order, see :func:`_resolve_platforms`);
an unknown name fails before anything runs. An engine that is not provisioned
or does not answer is skipped per platform with a clear log line, so partial
provisioning is OK.
"""

from __future__ import annotations

import logging
import sys
from collections import Counter, deque
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.text import Text

from .e2e import DEFAULT_LOAD_TIMEOUT
from .logging_config import configure_logging

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator

    from .staging.load_check import TableTick

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="2026 Cloud Warehouse Benchmark CLI.",
)


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Per-engine deadline for connectivity-check's SELECT 1 (seconds).
CONNECTIVITY_TIMEOUT_S = 30.0

# --help text shared by every --platforms option.
PLATFORMS_HELP = "Comma list of engines, or 'all' for the seven in canonical order."


def _logger() -> logging.Logger:
    return logging.getLogger("benchmark.cli")


def _configure(verbose: bool = False) -> Path:
    """Configure logging; returns the JSON audit-log path for callers to surface."""
    return configure_logging("DEBUG" if verbose else "INFO")


class _ConciseExcFormatter(logging.Formatter):
    """Render an exception as one truncated line instead of a traceback. A probe
    failure then reads as the ERROR message + a short reason (which table, why),
    not a multi-screen traceback panel. Every ERROR still logs; nothing is dropped.
    Used only for prepare-scale's console during the sweep — the audit JSON file
    keeps the full exception, and --verbose restores full tracebacks."""

    limit: int = 200

    def formatException(self, ei) -> str:  # noqa: N802 — overrides stdlib logging.Formatter API
        exc = ei[1] if ei else None
        if exc is None:
            return ""
        one = " ".join(str(exc).split())
        return one if len(one) <= self.limit else one[: self.limit - 1] + "…"


# ---- Query suite, data generation, staging, Estuary ----------------------


@app.command("validate-queries")
def validate_queries(
    scale_factor: Annotated[int, typer.Option("--scale", help="Scale factor to hash against.")] = 1,
    suites: Annotated[
        str,
        typer.Option(
            "--suites",
            help="Comma-separated suite list. 'tpcds' hashes against data/tpcds/sf{N}; "
            "'dsb' hashes against data/dsb/sf{N}.",
        ),
    ] = "tpcds,dsb",
    data_root: Annotated[Path, typer.Option("--data-root")] = Path("data"),
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Hash canonical queries against per-suite local DuckDB and persist into
    ``ground_truth_hashes``. Runs once per suite so DSB queries hash against
    DSB-skewed data and produce hashes distinct from their TPC-DS pair."""
    _configure(verbose)
    from .datagen.ground_truth import hash_ground_truth
    from .results import ResultsDB

    db = ResultsDB(db_path)
    suite_list = [s.strip() for s in suites.split(",") if s.strip()]

    overall_ok = 0
    overall_total = 0
    for suite in suite_list:
        suite_dir = data_root / suite / f"sf{scale_factor}"
        if not suite_dir.exists():
            typer.echo(f"  skipping {suite}: data dir missing ({suite_dir})", err=True)
            continue
        summary = hash_ground_truth(suite_dir, db, scale_factor=scale_factor, suite=suite)
        ok = sum(1 for s in summary.values() if s["error_message"] is None)
        overall_ok += ok
        overall_total += len(summary)
        typer.echo(f"{suite} ground-truth: {ok}/{len(summary)} ok")
        for qid, s in summary.items():
            if s["error_message"]:
                typer.echo(f"  FAIL {qid}: {s['error_message'][:120]}")

    typer.echo(f"total: {overall_ok}/{overall_total} ok across {suite_list}")


@app.command("generate-tpcds")
def generate_tpcds(
    scales: Annotated[str, typer.Option("--scales", help="Comma-separated scale factors.")] = "1",
    output_root: Annotated[Path, typer.Option("--output-root")] = Path("data/tpcds"),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Regenerate even if a scale's parquet data already exists "
            "(generation is skip-if-present by default).",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Generate TPC-DS data via DuckDB's tpcds extension."""
    _configure(verbose)
    from .datagen.tpcds import generate_tpcds_sf, write_expected_counts_manifest

    sfs = [int(s) for s in scales.split(",") if s.strip()]
    all_counts: dict[int, dict[str, int]] = {}
    for sf in sfs:
        counts = generate_tpcds_sf(sf, output_root / f"sf{sf}", force=force)
        all_counts[sf] = counts
    write_expected_counts_manifest(all_counts, output_root / "expected_counts.json")
    total = sum(sum(c.values()) for c in all_counts.values())
    typer.echo(f"TPC-DS generated for scales {sfs} ({total:,} total rows)")


@app.command("generate-dsb")
def generate_dsb(
    scales: Annotated[str, typer.Option("--scales", help="Comma-separated scale factors.")] = "1",
    output_root: Annotated[Path, typer.Option("--output-root")] = Path("data/dsb"),
    queries_only: Annotated[
        bool,
        typer.Option(
            "--queries-only", help="Only rebuild the 102 SQL files; skip data generation."
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Regenerate even if a scale's parquet data already exists "
            "(generation is skip-if-present by default).",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Rebuild the 102 DSB query SQL files and generate DSB-skewed data.

    The SQL set (queries 1-99 mirror TPC-DS; 100-102 are DSB-specific) is always
    rebuilt. Unless ``--queries-only`` is passed, skewed data is then generated
    per scale via ``benchmark.datagen.dsb.generate_dsb_sf``. That requires the
    microsoft/dsb generator: a native ``dsdgen`` (``dsdgen-dsb`` on PATH or under
    ``vendor/dsb``), or, when absent, one built from source (Docker on macOS). With
    no generator available the command fails (exit 1); it never substitutes
    uniform data.
    """
    _configure(verbose)
    logger = _logger()

    # 1) Always (re)build the 102 DSB query SQL files (idempotent helper script).
    logger.info(
        "rebuilding DSB query SQL files (102 queries: 1-99 copies of TPC-DS, "
        "100-102 are DSB-specific)"
    )
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "data"))
    try:
        import build_dsb_queries

        build_dsb_queries.main()
    finally:
        sys.path.pop(0)

    if queries_only:
        typer.echo("DSB query set rebuilt (--queries-only; no data generated)")
        return

    # 2) Generate DSB-skewed data per scale. The generator is native dsdgen (or
    #    a native build on Linux / Docker on macOS); it RAISES rather than
    #    silently producing uniform data, so a setup gap fails loudly here.
    from .datagen.dsb import generate_dsb_sf
    from .datagen.tpcds import write_expected_counts_manifest
    from .exceptions import DataGenerationError

    sfs = [int(s) for s in scales.split(",") if s.strip()]
    all_counts: dict[int, dict[str, int]] = {}
    try:
        for sf in sfs:
            all_counts[sf] = generate_dsb_sf(sf, output_root / f"sf{sf}", force=force)
    except DataGenerationError as exc:
        typer.echo(f"DSB generation failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    write_expected_counts_manifest(all_counts, output_root / "expected_counts.json")

    total = sum(sum(c.values()) for c in all_counts.values())
    typer.echo(f"DSB rebuilt + skewed data generated for scales {sfs} ({total:,} rows)")


@app.command("stage-data")
def stage_data(
    bucket: Annotated[str | None, typer.Option("--bucket")] = None,
    suites: Annotated[
        str,
        typer.Option(
            "--suites",
            help="Comma-separated suites to stage (default all). Scope to e.g. "
            "tpcds,dsb to skip ClickBench's multi-GiB upload.",
        ),
    ] = "tpcds,dsb,clickbench",
    no_progress: Annotated[
        bool,
        typer.Option("--no-progress", help="Disable the ClickBench upload progress bar."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Stage local Parquet to S3 for the requested suites.

    Requires AWS credentials (env/aws.env) and BENCHMARK_S3_BUCKET (env/benchmark.env).
    """
    _configure(verbose)
    from .staging.s3 import stage_all_suites

    suite_tuple = tuple(s.strip() for s in suites.split(",") if s.strip())
    stage_all_suites(bucket=bucket, suites=suite_tuple, show_progress=not no_progress)


@app.command("flow-auth")
def flow_auth(
    token: Annotated[str | None, typer.Option("--token")] = None,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Authenticate flowctl using ESTUARY_REFRESH_TOKEN."""
    _configure(verbose)
    from .staging.estuary import auth

    auth(token)
    typer.echo("flowctl authenticated")


def _resolve_flow_source(source: str) -> Path:
    from .staging.estuary import ESTUARY_DIR

    aliases: dict[str, Path] = {
        "source-s3": ESTUARY_DIR / "captures" / "source-s3.flow.yaml",
        # SF1000 split: 24 capture tasks (48 bindings) for parallel ingest;
        # the sf1000 bindings are deliberately absent from source-s3.
        "sf1000-captures": ESTUARY_DIR / "captures" / "source-s3-sf1000.flow.yaml",
        "motherduck": ESTUARY_DIR / "materializations" / "motherduck.flow.yaml",
        "redshift": ESTUARY_DIR / "materializations" / "redshift.flow.yaml",
        "fabric": ESTUARY_DIR / "materializations" / "fabric.flow.yaml",
        "clickhouse": ESTUARY_DIR / "materializations" / "clickhouse.flow.yaml",
        "snowflake": ESTUARY_DIR / "materializations" / "snowflake.flow.yaml",
        "bigquery": ESTUARY_DIR / "materializations" / "bigquery.flow.yaml",
        "databricks": ESTUARY_DIR / "materializations" / "databricks.flow.yaml",
    }
    return aliases.get(source, Path(source))


@app.command("flow-render")
def flow_render(
    source: Annotated[
        str,
        typer.Argument(
            help="Path to a YAML under estuary/, or an alias: source-s3, "
            "sf1000-captures, or one of the 7 engine names. Resolves to the "
            "matching capture/materialization.",
        ),
    ],
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """${VAR}-substitute a Flow spec and write to .applied/ without publishing."""
    _configure(verbose)
    from .staging.estuary import render

    typer.echo(render(_resolve_flow_source(source)))


@app.command("flow-publish")
def flow_publish(
    source: Annotated[
        str,
        typer.Argument(
            help="Path to a YAML under estuary/, or an alias: source-s3, "
            "sf1000-captures, or one of the 7 engine names. Resolves to the "
            "matching capture/materialization.",
        ),
    ],
    materializations: Annotated[
        str | None,
        typer.Option(
            "--materializations",
            help="Comma list of materialization names (full or trailing segment, e.g. "
            "'bigquery-tpcds-sf1') to publish a scoped SUBSET instead of the full spec.",
        ),
    ] = None,
    scale: Annotated[
        int | None,
        typer.Option("--scale", help="Scope to materializations for this scale (-sf<N> suffix)."),
    ] = None,
    suites: Annotated[
        str | None,
        typer.Option("--suites", help="Scope to these suites (e.g. tpcds,dsb)."),
    ] = None,
    backfill: Annotated[
        int | None,
        typer.Option(
            "--backfill",
            help="Set the backfill counter on every selected binding (forces Estuary to "
            "re-read the collection's history). Use a value above the current one, e.g. 2. "
            "Only valid with a scope filter.",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """${VAR}-substitute a Flow spec and publish it via flowctl.

    Default: publishes the FULL spec — captures / materializations are define-once
    and stateful, carrying every suite × scale binding and picking up each scale's
    data as it lands in S3. For the ``clickhouse`` alias, ``CREATE DATABASE IF NOT
    EXISTS`` is run for every database referenced by the YAML before publish.

    Scoped: pass ``--materializations`` / ``--scale`` / ``--suites`` to publish only
    a matching SUBSET (flowctl only updates the named specs; the rest are left
    intact). Add ``--backfill N`` to bump those bindings' backfill counter and force
    a re-read of history — the targeted-recovery path (e.g. re-backfill just
    ``bigquery`` tpcds+dsb at one scale) without re-touching other scales or
    ClickBench. See docs.estuary.dev/reference/backfilling-data.
    """
    _configure(verbose)

    yaml_path = _resolve_flow_source(source)
    scoped = bool(materializations or scale is not None or suites)
    if backfill is not None and not scoped:
        typer.echo(
            "--backfill requires a scope filter (--materializations/--scale/--suites)", err=True
        )
        raise typer.Exit(2)

    if scoped:
        from .staging.estuary import publish_scoped

        names = (
            {n.strip() for n in materializations.split(",") if n.strip()}
            if materializations
            else None
        )
        suite_list = [s.strip() for s in suites.split(",") if s.strip()] if suites else None
        selected = publish_scoped(
            yaml_path,
            names=names,
            scales=[scale] if scale is not None else None,
            suites=suite_list,
            backfill=backfill,
        )
        suffix = f" (backfill={backfill})" if backfill is not None else ""
        typer.echo(f"published {len(selected)} materialization(s){suffix}: {', '.join(selected)}")
        return

    from .staging.estuary import publish

    if source == "clickhouse":
        from .staging.clickhouse_prepare import prepare_databases

        databases = prepare_databases(yaml_path)
        if databases:
            typer.echo(f"clickhouse: ensured databases {databases}")
    publish(yaml_path)
    typer.echo(f"published {yaml_path.name}")


@app.command("flow-disable")
def flow_disable(
    source: Annotated[
        str,
        typer.Argument(help="An engine name or a materialization YAML under estuary/."),
    ],
    materializations: Annotated[
        str | None,
        typer.Option(
            "--materializations",
            help="Comma list of materialization names (full or trailing segment).",
        ),
    ] = None,
    scale: Annotated[
        int | None,
        typer.Option("--scale", help="Scope to materializations for this scale (-sf<N> suffix)."),
    ] = None,
    suites: Annotated[
        str | None,
        typer.Option("--suites", help="Scope to these suites (e.g. tpcds,dsb)."),
    ] = None,
    enable: Annotated[
        bool,
        typer.Option("--enable", help="Resume the selected materializations instead."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Pause an engine's Estuary materializations before a measured run (or resume them).

    A live materialization keeps committing to the warehouse the benchmark is
    about to measure, so pause it once ``validate-loads`` passes and before
    ``sql`` / ``cold`` / ``concurrency`` / ``sustained``. Publishes the scoped spec
    with ``shards.disable: true`` (``--enable`` publishes it as the YAML has it);
    no backfill counter moves and the loaded data stays. Only materializations
    Estuary already holds are touched, and a scope filter is required.
    """
    _configure(verbose)
    if not (materializations or scale is not None or suites):
        typer.echo(
            "flow-disable requires a scope filter (--materializations/--scale/--suites)", err=True
        )
        raise typer.Exit(2)
    from .staging.estuary import set_materializations_disabled

    names = (
        {n.strip() for n in materializations.split(",") if n.strip()} if materializations else None
    )
    suite_list = [s.strip() for s in suites.split(",") if s.strip()] if suites else None
    done = set_materializations_disabled(
        _resolve_flow_source(source),
        disabled=not enable,
        names=names,
        scales=[scale] if scale is not None else None,
        suites=suite_list,
    )
    typer.echo(
        f"{'enabled' if enable else 'disabled'} {len(done)} materialization(s): {', '.join(done)}"
    )


@app.command("flow-reset")
def flow_reset(
    source: Annotated[
        str,
        typer.Argument(
            help="Capture spec to derive collection models from: an alias "
            "(e.g. source-s3) or a YAML path under estuary/.",
        ),
    ] = "source-s3",
    suites: Annotated[
        str | None,
        typer.Option("--suites", help="Scope to these suites (e.g. tpcds,dsb,clickbench)."),
    ] = None,
    scale: Annotated[
        int | None,
        typer.Option("--scale", help="Scope to collections at this scale (sf<N> segment)."),
    ] = None,
    tables: Annotated[
        str | None,
        typer.Option("--tables", help="Scope to these table names (e.g. call_center,item)."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List the collections that would reset; publish nothing."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Scoped Estuary data-flow reset: re-capture from S3 and rewrite collections.

    Publishes the scope-matching collections with ``reset: true`` — Estuary
    regenerates each journal and auto-bumps the backfill counters on the capture
    binding AND every subscribing materialization binding
    (docs.estuary.dev/reference/backfilling-data). Use when the CAPTURE must
    re-read S3 (e.g. after a capture-runtime change such as ``enable-runtime-v2``);
    for re-loading a destination from the existing journal, use
    ``flow-publish --backfill`` instead.

    WARNING: collections are shared upstream of all 7 engines — a reset truncates
    and reloads the matching destination tables on EVERY engine, not just one. A
    scope filter is mandatory. Verify afterwards with ``validate-loads --wait``.
    """
    _configure(verbose)
    from .staging.estuary import build_reset_spec, reset_collections

    yaml_path = _resolve_flow_source(source)
    suite_list = [s.strip() for s in suites.split(",") if s.strip()] if suites else None
    table_list = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
    scale_list = [scale] if scale is not None else None

    if dry_run:
        _, selected = build_reset_spec(
            yaml_path, suites=suite_list, scales=scale_list, tables=table_list
        )
        typer.echo(f"would reset {len(selected)} collection(s):")
        for name in selected:
            typer.echo(f"  {name}")
        return

    selected = reset_collections(yaml_path, suites=suite_list, scales=scale_list, tables=table_list)
    typer.echo(f"reset {len(selected)} collection(s): {', '.join(selected)}")


@app.command("flow-metrics")
def flow_metrics(
    task: Annotated[
        str | None,
        typer.Option("--task", help="Substring filter on the task catalog name."),
    ] = None,
    collection: Annotated[
        str | None,
        typer.Option("--collection", help="Substring filter on the collection name."),
    ] = None,
    prefix: Annotated[
        str | None,
        typer.Option("--prefix", help="Catalog prefix to query (default: ESTUARY_ACCOUNT)."),
    ] = None,
    watch: Annotated[
        int,
        typer.Option(
            "--watch",
            help="Refresh every N seconds, showing docs/s deltas (0 = one snapshot). "
            "Estuary recommends >=60s intervals.",
        ),
    ] = 0,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Pipeline-side task telemetry from Estuary's OpenMetrics API.

    Shows ``captured_out_docs_total`` / ``materialized_out_docs_total`` per
    task x collection — how many documents the pipeline itself has moved —
    without connecting to any destination warehouse. Complements
    ``validate-loads`` (destination-side row counts): during a backfill,
    ``--watch 60`` makes progress visible even for engines the local machine
    cannot reach. Auth reuses ESTUARY_REFRESH_TOKEN as a Bearer token
    (docs.estuary.dev/reference/openmetrics-api).
    """
    _configure(verbose)
    import time as _time

    from rich.console import Console
    from rich.live import Live
    from rich.table import Table

    from .staging.estuary_metrics import MetricSample, doc_totals

    def snapshot() -> list[MetricSample]:
        return doc_totals(prefix, task_filter=task, collection_filter=collection)

    def render(rows: list[MetricSample], rates: dict[tuple[str, str, str], float]) -> Table:
        t = Table(title="Estuary OpenMetrics — documents moved")
        t.add_column("task")
        t.add_column("collection")
        t.add_column("metric")
        t.add_column("docs", justify="right")
        if rates:
            t.add_column("docs/s", justify="right")
        for s in rows:
            cells = [s.task, s.collection, s.name, f"{int(s.value):,}"]
            if rates:
                rate = rates.get((s.task, s.collection, s.name))
                cells.append(f"{rate:,.0f}" if rate is not None else "—")
            t.add_row(*cells)
        return t

    first = snapshot()
    if not first:
        typer.echo("no matching samples (task/collection filters too narrow, or tasks idle)")
        raise typer.Exit(1)
    if watch <= 0:
        Console().print(render(first, {}))
        return

    prev = {(s.task, s.collection, s.name): s.value for s in first}
    with Live(render(first, {}), refresh_per_second=1) as live:
        while True:
            _time.sleep(watch)
            rows = snapshot()
            rates = {}
            for s in rows:
                key = (s.task, s.collection, s.name)
                if key in prev:
                    rates[key] = max(0.0, (s.value - prev[key]) / watch)
                prev[key] = s.value
            live.update(render(rows, rates))


@app.command("clickhouse-prepare-databases")
def clickhouse_prepare_databases(
    source: Annotated[str, typer.Option("--source")] = "clickhouse",
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Stand-alone version of the pre-publish DB prep step.

    Parses ``estuary/materializations/clickhouse.flow.yaml`` (or another
    YAML via ``--source``), extracts every ``database`` field, and issues
    ``CREATE DATABASE IF NOT EXISTS`` against the cluster. Idempotent.
    """
    _configure(verbose)
    from .staging.clickhouse_prepare import prepare_databases

    yaml_path = _resolve_flow_source(source)
    databases = prepare_databases(yaml_path)
    typer.echo(f"ensured {len(databases)} database(s): {databases}")


@app.command("flow-delete")
def flow_delete(
    names: Annotated[
        list[str],
        typer.Argument(
            help="Catalog spec names to delete, e.g. 'CorrDyn/benchmark/captures/tpcds-sf1'."
        ),
    ],
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Delete one or more Estuary catalog specs by name."""
    _configure(verbose)
    from .staging.estuary import delete

    for name in names:
        delete(name)
        typer.echo(f"deleted {name}")


@app.command("flow-publish-motherduck")
def flow_publish_motherduck(
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Auth + publish the 2 specs for the MotherDuck pipeline (the unified
    source-s3 capture + the multi-block materialization), in order."""
    _configure(verbose)
    from .staging.estuary import publish_motherduck_pipeline

    publish_motherduck_pipeline()
    typer.echo("MotherDuck pipeline published")
    typer.echo("staging complete")


@app.command("generate-checksums")
def generate_checksums(
    output_path: Annotated[Path, typer.Option("--output")] = Path("data/CHECKSUMS.json"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """SHA-256 every Parquet under data/ and write a manifest."""
    _configure(verbose)
    from .staging.checksums import write_checksum_manifest

    write_checksum_manifest(_REPO_ROOT / "data", output_path)
    typer.echo(f"checksums written to {output_path}")


@app.command("generate-expected-counts")
def generate_expected_counts(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = Path("data/tpcds"),
    output: Annotated[Path, typer.Option("--output")] = Path("data/tpcds/expected_counts.json"),
    from_s3: Annotated[
        bool,
        typer.Option(
            "--from-s3",
            help="Derive counts from the staged S3 parquet footers instead of local "
            "files — backfills a scale whose data only exists in S3. Requires --suite "
            "and --scales.",
        ),
    ] = False,
    suite: Annotated[
        str | None,
        typer.Option("--suite", help="Suite for --from-s3 (tpcds|dsb); builds the S3 prefix."),
    ] = None,
    scales: Annotated[
        str | None,
        typer.Option("--scales", help="Comma-separated scales to derive for --from-s3 (e.g. 10)."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Read row counts from Parquet metadata and merge the load-validation row-count manifest.

    Local by default (walks ``--data-dir/sf*``); with ``--from-s3`` reads the staged
    S3 parquet footers for ``--suite`` at ``--scales`` (a footer is a range read, so
    even multi-GB facts are cheap). Either way the write is **merge-safe** — scales
    not in this run are preserved, so backfilling SF10 never clobbers SF1/SF100.
    """
    _configure(verbose)

    if from_s3:
        if not suite or not scales:
            typer.echo("--from-s3 requires --suite and --scales", err=True)
            raise typer.Exit(2)
        from .settings import get_settings
        from .staging.expected_counts import write_from_s3_metadata

        s = get_settings()
        est = s.ESTUARY
        kid = est.S3_AWS_ACCESS_KEY_ID.get_secret_value() if est.S3_AWS_ACCESS_KEY_ID else None
        sec = (
            est.S3_AWS_SECRET_ACCESS_KEY.get_secret_value()
            if est.S3_AWS_SECRET_ACCESS_KEY
            else None
        )
        if not kid or not sec:
            typer.echo(
                "ESTUARY_S3_AWS_ACCESS_KEY_ID / ESTUARY_S3_AWS_SECRET_ACCESS_KEY not set", err=True
            )
            raise typer.Exit(2)
        sfs = [int(x) for x in scales.split(",") if x.strip()]
        write_from_s3_metadata(
            s.S3_BUCKET, suite, sfs, output, region=s.AWS.REGION, access_key=kid, secret_key=sec
        )
        typer.echo(f"expected_counts manifest updated from S3 ({suite} SF{sfs}) -> {output}")
        return

    from .staging.expected_counts import write_from_parquet_metadata

    write_from_parquet_metadata(data_dir, output)
    typer.echo(f"expected_counts manifest written to {output}")


# ---- Loading + readiness -------------------------------------------------


@app.command("load-motherduck-local")
def load_motherduck_local(
    parquet_dir: Annotated[Path, typer.Option("--data-dir")] = Path("data/tpcds/sf1"),
    schema: Annotated[str, typer.Option("--schema")] = "tpcds_sf1",
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Load local SF1 Parquet directly into MotherDuck (Estuary bypass).

    Convenient when only MOTHERDUCK_TOKEN is configured and the operator
    hasn't yet wired up AWS S3 staging + Estuary. The Estuary path
    (``benchmark stage-data`` -> flowctl capture/materialization) is the
    production-equivalent route used for all other engines.
    """
    _configure(verbose)
    from .staging.motherduck_local import load_sf1_to_motherduck

    counts = load_sf1_to_motherduck(parquet_dir=parquet_dir, schema=schema)
    typer.echo(f"loaded {len(counts)} tables into MotherDuck benchmark_db.{schema}")


@contextmanager
def _live_load_progress() -> Iterator[Callable[[TableTick], None]]:
    """Live rich display for a row-count sweep; yields the ``on_table`` callback
    that drives it. One progress bar per platform (bare platform name on the left;
    the current dataset + a m/M counter to the right of the bar, cleared once that
    platform is fully caught up), with per-table row counts streaming into a bounded
    scrolling log panel above. For the duration the console log handler is detached
    so its records flow into the panel's ring buffer (the file handler stays
    attached, so the full log is still written); tracebacks are dropped from the
    panel (the message carries the signal — the file keeps the full exception).
    Shared by ``validate-loads`` and the ``prepare-scale`` data sweep."""
    # Recent log lines kept on screen — bounded so a long sweep can't grow the
    # scrollback without limit; the full set still goes to the file handler.
    log_window = 25

    # Fixed width for the dataset column to the right of the bar, so its text
    # changing length as the walk moves between datasets can't jitter the columns.
    dataset_col_width = 18

    console = Console(stderr=True)
    progress = Progress(
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TextColumn("{task.fields[dataset]}"),
        TaskProgressColumn(),
        TextColumn("{task.completed}/{task.total} tables"),
        TimeRemainingColumn(),
        console=console,
    )
    tasks: dict[str, int] = {}  # platform -> task id (one bar per platform)
    caught: dict[str, set[str]] = {}  # platform -> caught-up table keys (forward-only)
    ring: deque[str] = deque(maxlen=log_window)

    def render() -> Group:
        # Fixed-height panel of the last log_window log lines, pinned above the bars.
        body = Text("\n".join(ring), style="dim")
        return Group(Panel(body, title="recent logs", height=log_window + 2), progress)

    live = Live(render(), console=console, refresh_per_second=10)

    class _RingFormatter(logging.Formatter):
        """One concise line per record; drop tracebacks (the message is the signal,
        the file handler still keeps the full exception)."""

        def formatException(self, ei: object) -> str:  # noqa: N802 (stdlib override)
            return ""

    class _RingHandler(logging.Handler):
        """Append formatted records to the bounded ring and refresh the live view.
        No level is set here — the logger's existing level still gates arrivals."""

        def emit(self, record: logging.LogRecord) -> None:
            try:
                ring.append(self.format(record))
                live.update(render())
            except Exception:  # never let logging crash the sweep
                self.handleError(record)

    ring_handler = _RingHandler()
    ring_handler.setFormatter(
        _RingFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    )

    def on_table(tick: TableTick) -> None:
        # One bar per platform: the description stays the bare platform name; the
        # current dataset + a m/M counter sit to the right of the bar and clear once
        # the platform is fully caught up. The bar fills as the platform's tables —
        # across all datasets — reach their targets. Row counts aren't on the bar
        # (the worker logs them per table; they scroll in the panel above).
        if tick.platform not in tasks:
            tasks[tick.platform] = progress.add_task(
                tick.platform, total=tick.platform_table_total, completed=0, dataset=""
            )
        if tick.done:
            caught.setdefault(tick.platform, set()).add(tick.key)
        done_count = len(caught.get(tick.platform, ()))
        complete = done_count >= tick.platform_table_total
        dataset = "" if complete else f"{tick.suite} ({tick.suite_index}/{tick.suite_count})"
        progress.update(
            tasks[tick.platform],
            total=tick.platform_table_total,
            completed=done_count,
            dataset=dataset.ljust(dataset_col_width),
        )
        live.update(render())

    root = logging.getLogger("benchmark")
    console_handlers = [h for h in root.handlers if isinstance(h, RichHandler)]
    for h in console_handlers:
        root.removeHandler(h)
    root.addHandler(ring_handler)
    try:
        with live:
            yield on_table
    finally:
        root.removeHandler(ring_handler)
        for h in console_handlers:
            root.addHandler(h)


def _validate_loads_live(
    plat_list: list[str],
    scale_factor: int,
    suite_list: tuple[str, ...],
    wait: int,
    poll_interval: int,
) -> list[str]:
    """Run validate-loads under the live display. Returns the final failure
    strings, same as ``validate_engine_loads``."""
    from .staging.load_check import validate_engine_loads

    with _live_load_progress() as on_table:
        return validate_engine_loads(
            plat_list,
            scale_factor,
            suites=suite_list,
            wait_seconds=wait,
            poll_interval_seconds=poll_interval,
            on_table=on_table,
        )


@app.command("validate-loads")
def validate_loads(
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "motherduck",
    scale_factor: Annotated[int, typer.Option("--scale")] = 1,
    suites: Annotated[str, typer.Option("--suites")] = "tpcds,dsb,clickbench",
    wait: Annotated[
        int,
        typer.Option(
            "--wait",
            help="Seconds to poll for the load to catch up before failing (0 = fail fast). "
            "Use after an Estuary publish, whose backfill is asynchronous.",
        ),
    ] = 0,
    poll_interval: Annotated[
        int, typer.Option("--poll-interval", help="Seconds between polls when --wait > 0.")
    ] = 15,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Row-count check: each engine's loaded tables vs the per-suite manifest.

    Iterates per suite so each block hits its own schema (TPC-DS -> tpcds_sf1,
    DSB -> dsb_sf1, ClickBench -> clickbench). Drop a suite with
    e.g. ``--suites tpcds,dsb`` when ClickBench is still backfilling.

    By default this is a single fail-fast pass. Pass ``--wait N`` to poll for up
    to N seconds — Estuary creates target tables and backfills asynchronously
    after a materialization publish, so an immediate check races the load.
    """
    log_path = _configure(verbose)
    from .staging.load_check import validate_engine_loads

    plat_list = _resolve_platforms(platforms)
    suite_list = tuple(s.strip() for s in suites.split(",") if s.strip())

    # When polling on a TTY, render live per-table progress bars with log lines
    # scrolling above them; otherwise fall back to plain log lines (CI / piped /
    # fail-fast). The display is built here (CLI layer); load_check stays rich-free.
    if wait > 0 and sys.stderr.isatty():
        failures = _validate_loads_live(plat_list, scale_factor, suite_list, wait, poll_interval)
    else:
        failures = validate_engine_loads(
            plat_list,
            scale_factor,
            suites=suite_list,
            wait_seconds=wait,
            poll_interval_seconds=poll_interval,
        )
    if failures:
        # Per-table failure detail already streamed into the live display / log
        # during the run — don't re-dump it. Summarize per platform and point at
        # the full JSON record so the operator can read every outstanding table.
        by_platform = Counter(f.split("/", 1)[0] for f in failures)
        breakdown = ", ".join(f"{plat}: {n}" for plat, n in sorted(by_platform.items()))
        _logger().error(
            "validate-loads: %d table(s) outstanding (%s) — full per-table detail in %s",
            len(failures),
            breakdown,
            log_path,
        )
        raise typer.Exit(code=1)
    typer.echo("all load row-counts ok")


def _validate_scale_opt(value: int) -> int:
    """Typer callback: reject scales outside the canonical set early."""
    from .scales import validate_scale

    try:
        return validate_scale(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _resolve_platforms(platforms: str, *, eligible: Collection[str] | None = None) -> list[str]:
    """Resolve --platforms ('all' or a comma list) to validated platform ids.

    Every command that takes ``--platforms`` resolves it here. ``all`` expands to
    the seven engines in canonical order (``connectors.registry.ALL_PLATFORMS``:
    snowflake, databricks, bigquery, redshift, fabric, clickhouse, motherduck). A
    command that acts on only some engines passes ``eligible`` (e.g. ``reconcile``
    passes the engines with a reconciler), and ``all`` keeps those, in the same
    order. A comma list keeps the order given.

    Fails loudly on an unknown/misspelled name (with a 'did you mean' hint) so a
    typo can't silently skip an engine.

    Args:
        platforms: The raw ``--platforms`` value.
        eligible: Engines the command acts on; ``None`` means all seven.

    Returns:
        The platform ids to run, in order.

    Raises:
        typer.BadParameter: A name is not a registered engine (exit 2).
    """
    import difflib

    from .connectors.registry import registered_platforms

    known = registered_platforms()
    if platforms.strip() == "all":
        return [p for p in known if eligible is None or p in eligible]
    requested = [p.strip() for p in platforms.split(",") if p.strip()]
    unknown = [p for p in requested if p not in known]
    if unknown:
        parts = []
        for u in unknown:
            near = difflib.get_close_matches(u, known, n=1)
            parts.append(f"'{u}'" + (f" (did you mean '{near[0]}'?)" if near else ""))
        raise typer.BadParameter(
            f"unknown platform(s): {', '.join(parts)}. valid: {', '.join(known)}"
        )
    return requested


def _resolve_suites(suites: str) -> tuple[str, ...]:
    """Resolve/validate --suites against the known suites; reject unknowns."""
    from .prepare_scale import DEFAULT_SUITES

    requested = tuple(s.strip() for s in suites.split(",") if s.strip())
    unknown = [s for s in requested if s not in DEFAULT_SUITES]
    if unknown:
        raise typer.BadParameter(
            f"unknown suite(s): {', '.join(unknown)}. valid: {', '.join(DEFAULT_SUITES)}"
        )
    return requested


@app.command("prepare-scale")
def prepare_scale_cmd(
    scale: Annotated[int, typer.Option("--scale", callback=_validate_scale_opt)] = 1,
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "all",
    suites: Annotated[str, typer.Option("--suites")] = "tpcds,dsb,clickbench",
    compute_only: Annotated[
        bool,
        typer.Option(
            "--compute-only", help="Skip the (slow) data row-count sweep; check compute only."
        ),
    ] = False,
    mutate_tfvars: Annotated[bool, typer.Option("--mutate-tfvars/--no-mutate-tfvars")] = True,
    check_staging: Annotated[
        bool,
        typer.Option(
            "--check-staging/--no-check-staging",
            help="For cross-engine data gaps, inspect the staged S3 object (size/date "
            "vs local) and report the verdict inline. Needs AWS creds; degrades to "
            "advisory if unavailable.",
        ),
    ] = True,
    check_s3: Annotated[
        bool,
        typer.Option(
            "--check-s3/--no-check-s3",
            help="Also validate the staged S3 source: parquet footer counts vs the "
            "manifest (== the target-DB baseline), asserting S3 == DB end-to-end. Per "
            "suite (shared upstream); cheap (footer range reads). Needs the Estuary S3 "
            "keys. Skipped under --compute-only.",
        ),
    ] = True,
    check_estuary: Annotated[
        bool,
        typer.Option(
            "--check-estuary/--no-check-estuary",
            help="Also validate the upstream Estuary collections (presence + non-empty "
            "+ freshness) for each suite. Shared across engines, so reported once per "
            "suite. Needs flowctl auth; degrades to 'unreachable' if unavailable. "
            "Skipped under --compute-only.",
        ),
    ] = True,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the write confirmation.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Check per-platform readiness (data + compute) for a scale and help reach it.

    Reports where each platform stands on both axes — data loaded (row counts vs
    the manifest) and compute sized (live introspection vs the run-plan) — then,
    for off-target compute, proposes sizing-var edits to the tool-owned
    ``terraform/sizing.auto.tfvars.json`` (confirmed before writing; the human
    ``terraform.tfvars`` is never touched) and prints manual steps for out-of-band
    engines. ``--no-mutate-tfvars`` = report-only; ``--yes`` = skip the confirm.
    It never runs ``terraform apply``.
    """
    log_path = _configure(verbose)
    from .prepare_scale import (
        DEFAULT_SIZING_TFVARS_PATH,
        assess,
        assess_sources,
        build_board_table,
        build_sources_table,
        default_staged_check,
        load_sizing,
        remediation_plan,
        write_sizing_edits,
    )

    # Validate inputs up front so a typo fails loudly instead of silently skipping.
    plats = _resolve_platforms(platforms)
    suite_list = _resolve_suites(suites)

    # --compute-only skips the row-count data sweep (the slow part). The sweep
    # probes engines, so expected misses (a table not yet loaded, an unreachable
    # engine) log a per-table connector ERROR with a full traceback — the error
    # line is the signal, the traceback wall is the noise.
    if not compute_only and sys.stderr.isatty():
        # On a TTY, drive the data sweep with the live per-platform display (bars +
        # a bounded log panel, which already drops tracebacks); the full exception
        # still lands in the audit JSON via the file handler.
        with _live_load_progress() as on_table:
            results = assess(plats, scale, suite_list, check_data=True, on_table=on_table)
    else:
        # Non-TTY / --compute-only: keep the console handlers but, during the sweep,
        # render those expected exceptions as one truncated line (rich_tracebacks
        # off + a concise formatter); every ERROR still shows, INFO and per-table
        # progress logs are untouched, the audit JSON keeps the full exception, and
        # --verbose restores full tracebacks.
        root_logger = logging.getLogger("benchmark")
        console_handlers = [
            h for h in root_logger.handlers if not isinstance(h, logging.FileHandler)
        ]
        saved = [(h, h.formatter, getattr(h, "rich_tracebacks", None)) for h in console_handlers]
        if not verbose:
            for h in console_handlers:
                h.setFormatter(_ConciseExcFormatter())
                if hasattr(h, "rich_tracebacks"):
                    h.rich_tracebacks = False
        try:
            results = assess(plats, scale, suite_list, check_data=not compute_only)
        finally:
            for h, fmt, rich_tb in saved:
                h.setFormatter(fmt)
                if rich_tb is not None:
                    h.rich_tracebacks = rich_tb

    Console().print(build_board_table(scale, results))

    # The staged S3 source + Estuary collections feed every engine's load, so
    # validate them once per suite (upstream of the per-platform board). The S3
    # axis (footers vs manifest) + the data axis (DB vs manifest) together assert
    # S3 == DB end-to-end. Skipped under --compute-only.
    if (check_s3 or check_estuary) and not compute_only:
        sources = assess_sources(suite_list, scale, check_s3=check_s3, check_estuary=check_estuary)
        Console().print(build_sources_table(scale, sources))

    sizing = load_sizing(DEFAULT_SIZING_TFVARS_PATH)
    staged_check = default_staged_check if (check_staging and not compute_only) else None
    plan = remediation_plan(results, scale, sizing, staged_check=staged_check)
    edits = [e for e in plan.tfvar_edits if not e.is_noop]

    if edits:
        typer.echo(f"\nProposed sizing vars ({DEFAULT_SIZING_TFVARS_PATH}):")
        for e in edits:
            typer.echo(f"  {e.tfvar}: {e.old!r} -> {e.new!r}")
        if not mutate_tfvars:
            typer.echo("(--no-mutate-tfvars: not writing; set these yourself.)")
        elif yes or typer.confirm("Write these sizing vars?"):
            write_sizing_edits(edits, DEFAULT_SIZING_TFVARS_PATH)
            typer.echo(f"Wrote {DEFAULT_SIZING_TFVARS_PATH}. Now run:")
            typer.echo("  terraform -chdir=terraform plan")
            typer.echo("  terraform -chdir=terraform apply")

    if plan.manual_steps:
        typer.echo("\nManual (out-of-band) steps:")
        for s in plan.manual_steps:
            typer.echo(f"  {s}")

    if plan.data_steps:
        typer.echo("\nData gaps:")
        for s in plan.data_steps:
            typer.echo(f"  {s}")

    # The live display only keeps the last screenful of per-table lines; the full
    # sweep (every table, all platforms) is in the JSON audit log. Surface its path
    # so it's reachable after the run.
    typer.echo(f"\nFull sweep log (every table checked): {log_path}")


# ---- Translation, optimization, correctness ------------------------------


@app.command("translate")
def translate(
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "motherduck",
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Translate canonical DuckDB SQL to each requested engine dialect.

    Rewrites every file under ``queries/<engine>/`` for each engine, including the
    imported ClickBench SQL and the hand-applied correctness patches, so review
    ``git diff queries/`` afterwards and keep those.
    """
    _configure(verbose)
    from .translator import translate_all

    plat_list = _resolve_platforms(platforms)
    matrix = translate_all(plat_list)
    for engine in plat_list:
        ok = sum(
            1 for q in matrix.values() if q["engines"].get(engine, {}).get("status") == "success"
        )
        typer.echo(f"{engine}: {ok}/{len(matrix)} translated")


@app.command("validate-dialects")
def validate_dialects(
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "all",
    threshold: Annotated[
        float, typer.Option("--threshold", help="Minimum per-engine success rate.")
    ] = 0.8,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Dialect gate: check sqlglot translation success per engine.

    Translates all canonical queries to each dialect in-memory (no files
    written) and fails (exit 1) if any engine's success rate is below
    ``--threshold`` (default 0.8).
    """
    _configure(verbose)
    from .translator import dialect_success_rates, translate_all

    engines = _resolve_platforms(platforms)
    matrix = translate_all(engines, write_files=False)
    rates = dialect_success_rates(matrix)
    failed: list[str] = []
    for engine in engines:
        r = rates.get(engine, {"ok": 0, "total": 0, "rate": 0.0})
        ok_flag = r["rate"] >= threshold
        if not ok_flag:
            failed.append(engine)
        typer.echo(
            f"  {engine:12s} {int(r['ok'])}/{int(r['total'])} = "
            f"{r['rate'] * 100:.1f}%  [{'PASS' if ok_flag else 'FAIL'}]"
        )
    if failed:
        typer.echo(f"dialect gate FAILED for {failed} (< {threshold * 100:.0f}%)", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"dialect gate PASSED — all engines >= {threshold * 100:.0f}%")


@app.command("optimize")
def optimize(
    platform: Annotated[str, typer.Option("--platform")] = "motherduck",
    via: Annotated[
        str,
        typer.Option(
            "--via",
            help="Rewrite backend: 'claude-cli' (default, isolated `claude -p` per "
            "query, uses Claude Code auth) or 'sdk' (Anthropic API + BENCHMARK_ANTHROPIC_API_KEY).",
        ),
    ] = "claude-cli",
    jobs: Annotated[
        int, typer.Option("--jobs", help="Concurrent queries (each its own subprocess/API call).")
    ] = 4,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            help="Per-query timeout in seconds for the claude-cli backend "
            "(raise it for queries that time out generating a rewrite).",
        ),
    ] = 180,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Model id/alias (e.g. 'claude-opus-4-8'). Both backends default to "
            "BENCHMARK_OPTIMIZE_MODEL (Opus 4.8).",
        ),
    ] = None,
    max_attempts: Annotated[
        int,
        typer.Option(
            "--max-attempts",
            help="Model attempts per query (1 + retries) before keeping the original "
            "when a gate (parse / arity / equivalence) keeps failing.",
        ),
    ] = 3,
    equivalence: Annotated[
        bool,
        typer.Option(
            "--equivalence/--no-equivalence",
            help="Verify each rewrite is result-equivalent to the DuckDB ground truth by "
            "executing it locally (transpiled back to DuckDB) against the SF1 Parquet. "
            "Divergence triggers a retry; engine-only idioms are deferred to validate-sf1.",
        ),
    ] = True,
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Root of the local SF1 Parquet (data/<suite>/sf1)."),
    ] = Path("data"),
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Re-optimize every query, discarding the resume journal. Without it, a "
            "re-run resumes — already-optimized queries are skipped (no LLM spend), so an "
            "interrupted run (out of credits, Ctrl-C) continues where it left off.",
        ),
    ] = False,
    queries: Annotated[
        str | None,
        typer.Option(
            "--queries",
            help="Comma-separated query ids (e.g. 'dsb_q59,tpcds_q05') to optimize instead "
            "of the whole suite. The journal preserves every other query's state.",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Per-engine query optimization pass.

    Rewrites ``queries/<platform>/**/*.sql`` to idiomatic, engine-tuned SQL (one
    isolated model call per query — no context bleed between queries) and writes
    an audit log to ``results/optimization_<platform>.json``. ``--platform all``
    loops every engine. For motherduck this is essentially a no-op (DuckDB-native).

    Each rewrite must pass a gate sequence — parses in the engine's sqlglot
    dialect, preserves the output-column count, and (with ``--equivalence``)
    executes locally to a result matching the DuckDB ground truth within
    tolerance. A failed gate is fed back to the model and retried
    (``--max-attempts``); the original is kept if no candidate passes.

    Resumable: progress is journaled per query, so an interrupted run continues
    where it left off on the next invocation (use ``--force`` to redo all).

    Default backend ``claude-cli`` shells ``claude -p`` and needs no API key;
    ``--via sdk`` uses the Anthropic API (``BENCHMARK_ANTHROPIC_API_KEY``).
    """
    _configure(verbose)
    from .equivalence import LocalEquivalenceChecker
    from .optimize_pass import OPTIMIZE_BACKENDS, OptimizeNotConfiguredError, optimize_for_platform
    from .results import ResultsDB

    if via not in OPTIMIZE_BACKENDS:
        raise typer.BadParameter(f"--via must be one of {OPTIMIZE_BACKENDS}")
    platforms = _resolve_platforms(platform) if platform == "all" else [platform]
    query_filter = (
        frozenset(q.strip() for q in queries.split(",") if q.strip()) if queries else None
    )

    checker = (
        LocalEquivalenceChecker(ResultsDB(db_path), data_root=data_root) if equivalence else None
    )
    failures = 0
    try:
        for p in platforms:
            try:
                n = optimize_for_platform(
                    p,
                    model=model,
                    backend=via,
                    jobs=jobs,
                    timeout_s=timeout,
                    max_attempts=max_attempts,
                    equiv_checker=checker,
                    force=force,
                    queries=query_filter,
                )
                typer.echo(f"optimization pass complete for {p} via {via}: {n} queries rewritten")
            except OptimizeNotConfiguredError as e:
                typer.echo(f"optimize {p}: {e}", err=True)
                failures += 1
    finally:
        if checker is not None:
            checker.close()
    if failures:
        raise typer.Exit(1)


@app.command("design-physical")
def design_physical(
    output: Annotated[Path, typer.Option("--output")] = Path("queries/physical_design.json"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Emit the per-engine physical-design spec (clustering / distribution /
    partition keys). MotherDuck entry is a no-op."""
    _configure(verbose)
    from .physical_design import emit_design

    emit_design(output)
    typer.echo(f"physical design written to {output}")


@app.command("apply-physical-design")
def apply_physical_design(
    scale: Annotated[int, typer.Option("--scale")] = 1,
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "motherduck",
    design_path: Annotated[Path, typer.Option("--design")] = Path("queries/physical_design.json"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Apply the physical-design spec to loaded tables (no-op for MotherDuck)."""
    _configure(verbose)
    from .physical_design import apply_design

    plat_list = _resolve_platforms(platforms)
    apply_design(plat_list, scale, design_path)
    typer.echo(f"physical design applied for platforms={plat_list} at SF{scale}")


@app.command("validate-correctness")
def validate_correctness(
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "motherduck",
    blocks: Annotated[str, typer.Option("--blocks")] = "tpcds,dsb,clickbench",
    scale: Annotated[
        int,
        typer.Option(
            "--scale",
            help="Scale factor to validate at. Needs ground truth hashed at the same "
            "scale first: `validate-queries --scale N`.",
        ),
    ] = 1,
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    revert_on_regression: Annotated[
        bool,
        typer.Option(
            "--revert-on-regression",
            help="For any query that fails on the live engine AND was rewritten by the "
            "optimize pass, restore its pre-optimization baseline (from "
            "results/optimization_<platform>.json) and re-validate. Reverts the rewrite "
            "only when it regressed on the real engine — the authoritative equivalence gate. "
            "Valid at --scale 1 only.",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Run every canonical query on the target engine once and compare against
    `ground_truth_hashes`. Iterates per-block so each suite hits the right schema
    (TPC-DS -> tpcds_sf{scale}, DSB -> dsb_sf{scale}, ClickBench -> clickbench).

    Defaults to SF1, which is the correctness gate. Higher scales are a **spot check**:
    the report's weakest claim is that correctness is verified at ~1 GB only, and
    running this at 100 converts that into an evidenced one. Ground truth must exist
    at the same scale — `ground_truth_hashes` is keyed (query_id, scale_factor), so
    run `validate-queries --scale N` first.

    With ``--revert-on-regression`` this is the authoritative half of the
    optimization equivalence guarantee: a rewrite that diverges on the real
    engine is reverted to its translated baseline and re-checked, so a bad
    optimization can never survive into the benchmark.
    """
    _configure(verbose)
    from .connectors.registry import create_connector
    from .exceptions import ConnectorError
    from .optimize_pass import load_optimization_baselines, revert_query_file
    from .queries.registry import QueryRegistry
    from .results import ResultsDB
    from .sql_benchmark.validator import validate_platform, validate_query

    if revert_on_regression and scale != 1:
        # The optimize baselines were equivalence-gated at SF1, so reverting a
        # rewrite because it diverges at another scale would be a different — and
        # unjustified — decision, taken against evidence the baseline never saw.
        typer.echo(
            "--revert-on-regression is valid at --scale 1 only: the optimize "
            "baselines are equivalence-gated at SF1, so a divergence at another "
            "scale is not evidence about the rewrite.",
            err=True,
        )
        raise typer.Exit(code=2)

    # Mirrors `_block_specs`, with one deliberate difference: ClickBench's ground
    # truth is keyed at SF100 regardless of the run tier (see
    # hash_clickbench_ground_truth), and this command looks truth up by scale.
    block_specs: dict[str, tuple[str, int]] = {
        "tpcds": (f"tpcds_sf{scale}", scale),
        "dsb": (f"dsb_sf{scale}", scale),
        "clickbench": ("clickbench", 100),
    }

    plat_list = _resolve_platforms(platforms)
    db = ResultsDB(db_path)
    block_list = [b.strip() for b in blocks.split(",") if b.strip()]
    overall_fail = 0
    for platform in plat_list:
        for block in block_list:
            spec = block_specs.get(block)
            if spec is None:
                typer.echo(f"skipping unknown block '{block}'", err=True)
                continue
            schema, block_scale = spec
            try:
                connector = create_connector(
                    platform,
                    config={"schema": schema, "suite": block, "scale_factor": block_scale},
                )
            except (KeyError, ValueError) as exc:
                typer.echo(f"skipping {platform}/{block}: {exc}", err=True)
                overall_fail += 1
                continue
            try:
                results = validate_platform(
                    platform, connector, db, scale_factor=block_scale, blocks=[block]
                )
            except ConnectorError as exc:
                typer.echo(f"{platform}/{block}: connect failed — {exc}", err=True)
                overall_fail += 1
                continue
            passed = sum(1 for r in results if r.passed)
            failed = [r for r in results if not r.passed]
            nondet = sum(1 for r in results if r.nondeterministic)
            trusted = sum(1 for r in results if r.trusted)
            validated = passed - nondet - trusted  # result-checked passes
            parts = [f"{validated} result-validated"]
            if trusted:
                parts.append(f"{trusted} trusted (ClickBench-approved, not compared)")
            if nondet:
                parts.append(f"{nondet} nondeterministic, perf-only")
            typer.echo(
                f"{platform}/{block} SF{block_scale} validation: {passed}/{len(results)} passed"
                f" [{', '.join(parts)}]"
            )
            for r in failed[:10]:
                typer.echo(f"  FAIL {r.query.id}: {r.reason} ({r.error or ''})")

            if revert_on_regression and failed:
                baselines = load_optimization_baselines(platform)
                to_revert = [r for r in failed if r.query.id in baselines]
                reverted = [
                    r.query.id
                    for r in to_revert
                    if revert_query_file(platform, r.query.id, baselines[r.query.id])
                ]
                if reverted:
                    typer.echo(
                        f"  reverted {len(reverted)} regressed rewrite(s) to baseline; re-validating"
                    )
                    # Re-read the restored files and re-validate just those queries.
                    registry = QueryRegistry(dialect=platform)
                    recheck_conn = create_connector(
                        platform,
                        config={"schema": schema, "suite": block, "scale_factor": block_scale},
                    )
                    still_failing = 0
                    with recheck_conn:
                        for qid in reverted:
                            q = registry.get(qid)
                            if q is None:
                                continue
                            rr = validate_query(platform, recheck_conn, db, block_scale, q)
                            if rr.passed:
                                typer.echo(f"    {qid}: baseline passes ✓")
                            else:
                                still_failing += 1
                                typer.echo(f"    {qid}: STILL FAILS after revert — {rr.reason}")
                    # A reverted-and-passing query no longer counts as a failure.
                    failed = [r for r in failed if r.query.id not in set(reverted)]
                    overall_fail += still_failing

            overall_fail += len(failed)
    if overall_fail:
        raise typer.Exit(code=1)


@app.command("validate-sf1")
def validate_sf1(
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "motherduck",
    blocks: Annotated[str, typer.Option("--blocks")] = "tpcds,dsb,clickbench",
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    revert_on_regression: Annotated[bool, typer.Option("--revert-on-regression")] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Alias for `validate-correctness --scale 1`, the correctness gate.

    It is the name the README, CLAUDE.md and the operator guide use (the `e2e` driver
    does not run it), and SF1 is the gate that actually blocks the pipeline. The
    general command exists because the same check is worth running as a spot check at
    a larger scale, where the name `validate-sf1` would be a lie.
    """
    validate_correctness(
        platforms=platforms,
        blocks=blocks,
        scale=1,
        db_path=db_path,
        revert_on_regression=revert_on_regression,
        verbose=verbose,
    )


# ---- SQL benchmark -------------------------------------------------------


def _block_specs(scale: int) -> dict[str, tuple[str, int]]:
    """Per-block ``(schema, scale_factor)`` map for a given scale factor.

    Different benchmark suites live in different schemas (and equivalent
    dataset prefixes on other engines): TPC-DS -> ``tpcds_sf{scale}``,
    DSB -> ``dsb_sf{scale}``. ClickBench *data* is single-scale (~100 GB) and
    always lives in the ``clickbench`` schema, but it is recorded under the
    *run tier* so the same 100 GB workload is exercised across each engine's
    per-scale compute size (warehouse/RPU/slots) — the sizing curve. (Correctness
    is not judged at run time; validate-sf1 owns it.)

    Shared by ``sql`` / ``cold`` / ``concurrency`` so they stay in sync —
    every block hits its own schema at the correct scale, and results are
    recorded under that scale_factor.
    """
    return {
        "tpcds": (f"tpcds_sf{scale}", scale),
        "dsb": (f"dsb_sf{scale}", scale),
        # Single 100 GB dataset (schema fixed), but recorded under the run tier
        # so it exercises each engine's per-scale compute size. GT stays at SF100.
        "clickbench": ("clickbench", scale),
    }


@app.command("sql")
def sql_benchmark(
    scale: Annotated[int, typer.Option("--scale")] = 1,
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "motherduck",
    blocks: Annotated[str, typer.Option("--blocks")] = "tpcds,dsb",
    run_type: Annotated[str, typer.Option("--run-type")] = "warm",
    num_warmups: Annotated[int, typer.Option("--warmups")] = 1,
    num_measured: Annotated[int, typer.Option("--runs")] = 3,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-run even already-completed queries (appends new rows)."),
    ] = False,
    compute_size: Annotated[
        str | None,
        typer.Option(
            "--compute-size",
            help=(
                "Override the compute size for this run, so the SAME scale can be swept "
                "across sizes for the sizing frontier (combine with --force; each run "
                "auto-labels with the size it used). Interpreted per engine: Snowflake "
                "= warehouse tier (xsmall|small|medium|large|xlarge → BENCHMARK_WH_<SIZE>); "
                "BigQuery = fixed Enterprise-reservation slot count (multiple of 50, >=50 "
                "— the reservation is resized via API before the run and cost is the "
                "committed slots×rate×time). Other engines resize out-of-band (warned)."
            ),
        ),
    ] = None,
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Run the SQL benchmark.

    ``--platforms`` takes a comma list or ``all`` (the seven engines in canonical
    order); an unknown name fails before anything runs. Resolves the connector +
    cost collector per platform from the registries, and exits 2 without
    ``results/cost_config.json`` (run ``capture-cost-config``).

    ``--compute-size`` overrides Snowflake's warehouse for the run (each run
    auto-labels with the actual size it used), so the SAME scale can be swept
    across sizes to build the cost/performance frontier.
    """
    _configure(verbose)
    from .connectors.registry import create_connector
    from .cost.discovery import collector_for, load_cost_config
    from .exceptions import BenchmarkError, ConnectorError
    from .results import ResultsDB
    from .sql_benchmark.runner import run_sql_benchmark

    plat_list = _resolve_platforms(platforms)
    db = ResultsDB(db_path)
    block_list = [b.strip() for b in blocks.split(",") if b.strip()]

    # Per-block schema + scale-factor map (shared with cold/concurrency): the
    # runner is configured per block instead of per platform, so each suite
    # hits its own schema at the correct scale.
    block_specs = _block_specs(scale)

    try:
        cost_config = load_cost_config()
    except BenchmarkError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    # Run provenance: one record per invocation; every result row links to it.
    from .provenance import gather_provenance

    provenance_id = db.record_run_provenance(
        **gather_provenance(
            command="sql",
            scale_factor=scale,
            run_type=run_type,
            platforms=plat_list,
            blocks=block_list,
        )
    )
    _logger().info("run provenance recorded", extra={"provenance_id": provenance_id})

    # --compute-size is interpreted PER ENGINE: Snowflake picks a pre-created
    # warehouse per connection (no infra change); BigQuery resizes its single
    # Enterprise reservation to a fixed slot count via the Reservation API before
    # the run (out-of-band, like MotherDuck's tier). Validate up front against
    # every targeted engine so a bad value fails fast.
    wh_override: str | None = None
    bq_slots: int | None = None
    if compute_size:
        cs = compute_size.strip()
        snowflake_sizes = {"xsmall", "small", "medium", "large", "xlarge"}
        if "snowflake" in plat_list:
            if cs.lower() not in snowflake_sizes:
                typer.echo(
                    f"--compute-size for snowflake must be one of {sorted(snowflake_sizes)}",
                    err=True,
                )
                raise typer.Exit(code=2)
            wh_override = f"BENCHMARK_WH_{cs.upper()}"
        if "bigquery" in plat_list:
            from .compute.bigquery_reservation import validate_slots

            try:
                bq_slots = validate_slots(int(cs))
            except ValueError as exc:
                typer.echo(f"--compute-size for bigquery: {exc}", err=True)
                raise typer.Exit(code=2) from exc
        ignored = [p for p in plat_list if p not in ("snowflake", "bigquery")]
        if ignored:
            typer.echo(
                f"--compute-size ignored for (resize out-of-band): {', '.join(ignored)}",
                err=True,
            )

    for platform in plat_list:
        try:
            cost = collector_for(platform, cost_config)
        except (KeyError, ValueError) as exc:
            typer.echo(f"skipping {platform}: {exc}", err=True)
            continue

        # BigQuery: resize the fixed-capacity reservation to the requested slots
        # and wait for it to go live BEFORE benchmarking. Routing is project-wide,
        # so once live every query uses it (no per-query connector change); the
        # committed-cost collector prices slots×rate×time.
        if bq_slots is not None and platform == "bigquery":
            from .compute.bigquery_reservation import BigQueryReservationManager
            from .cost.bigquery import BigQueryCostCollector

            try:
                mgr = BigQueryReservationManager.from_settings()
                mgr.ensure(bq_slots)
                mgr.wait_until_live(bq_slots)
                # Assignment routing isn't instant — block until queries actually land
                # on the reservation, else the first minutes of queries bill on-demand.
                mgr.wait_until_routed()
            except Exception as exc:  # continue-on-failure (CLAUDE.md §5)
                _logger().error("bigquery reservation resize failed", exc_info=True)
                typer.echo(
                    f"bigquery: reservation resize to {bq_slots} slots failed — {exc}",
                    err=True,
                )
                continue
            cost = BigQueryCostCollector(tier="enterprise", reservation_slots=bq_slots)

        for block in block_list:
            spec = block_specs.get(block)
            if spec is None:
                typer.echo(f"skipping unknown block '{block}'", err=True)
                continue
            schema, block_scale = spec
            conn_config = {"schema": schema, "suite": block, "scale_factor": block_scale}
            if wh_override and platform == "snowflake":
                conn_config["warehouse"] = wh_override
            try:
                connector = create_connector(platform, config=conn_config)
            except (KeyError, ValueError) as exc:
                typer.echo(f"skipping {platform}/{block}: {exc}", err=True)
                continue
            try:
                run_sql_benchmark(
                    platform=platform,
                    connector=connector,
                    cost_collector=cost,
                    results_db=db,
                    scale_factor=block_scale,
                    blocks=[block],
                    num_warmups=num_warmups,
                    num_measured=num_measured,
                    run_type=run_type,
                    force=force,
                    provenance_id=provenance_id,
                    cost_config=cost_config,
                )
            except ConnectorError as exc:
                typer.echo(f"{platform}/{block}: connect failed — {exc}", err=True)
                continue
    _price_single_user(db, cost_config, plat_list, run_type)
    typer.echo(
        f"SQL benchmark complete for scale={scale} platforms={plat_list} "
        f"blocks={block_list} run_type={run_type}"
    )


@app.command("bq-reservation")
def bq_reservation(
    slots: Annotated[
        int | None,
        typer.Option(
            "--slots", help="Create/resize the fixed reservation to this many slots (mult of 50)."
        ),
    ] = None,
    teardown: Annotated[
        bool,
        typer.Option("--teardown", help="Delete the reservation + assignment to stop billing."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Manage the BigQuery Enterprise slot reservation out-of-band (lifecycle for
    a slot sweep).

    ``benchmark sql --platforms bigquery --compute-size <slots>`` already resizes
    the reservation per run, so a sweep is a shell loop of those. This command
    handles the bookends: ``--slots N`` pre-provisions/verifies before the loop,
    and ``--teardown`` deletes the reservation after so a fixed baseline stops
    billing. Requires the runner SA to hold ``roles/bigquery.resourceAdmin``.
    """
    _configure(verbose)
    from .compute.bigquery_reservation import BigQueryReservationManager

    if slots is None and not teardown:
        typer.echo("pass --slots N or --teardown", err=True)
        raise typer.Exit(code=2)
    mgr = BigQueryReservationManager.from_settings()
    if teardown:
        mgr.teardown()
        typer.echo("BigQuery reservation torn down")
        return
    mgr.ensure(slots)
    mgr.wait_until_live(slots)
    routed = mgr.wait_until_routed()
    typer.echo(
        f"BigQuery reservation live at {slots} slots"
        + ("" if routed else " (WARNING: query routing not yet confirmed)")
    )


@app.command("status")
def status(db_path: Path = Path("results/benchmark.duckdb")) -> None:
    """Print a coverage matrix of results recorded so far."""
    import duckdb

    if not db_path.exists():
        typer.echo("no results DB yet")
        return
    con = duckdb.connect(str(db_path))
    rows = con.execute(
        """
        SELECT platform, scale_factor, run_type, COUNT(DISTINCT query_id) AS queries,
               COUNT(*) AS runs
        FROM sql_benchmark_results
        GROUP BY platform, scale_factor, run_type
        ORDER BY platform, scale_factor, run_type
        """
    ).fetchall()
    con.close()
    if not rows:
        typer.echo("results DB is empty")
        return
    typer.echo("platform | scale | run_type | queries | runs")
    typer.echo("---------+-------+----------+---------+-----")
    for r in rows:
        typer.echo(" | ".join(str(c) for c in r))


def _stub(msg: str) -> int:
    typer.echo(msg, err=True)
    return 2


# ---- SQL benchmark: cold / concurrency / sustained -----------------------


@app.command("cold")
def cold(
    scale: Annotated[int, typer.Option("--scale")] = 1000,
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "motherduck",
    blocks: Annotated[str, typer.Option("--blocks")] = "tpcds,dsb",
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Cold-run pass at SF1000 (default); records run_type='cold'."""
    _configure(verbose)
    from .connectors.registry import create_connector
    from .cost.discovery import collector_for, load_cost_config
    from .exceptions import BenchmarkError, ConnectorError
    from .results import ResultsDB
    from .sql_benchmark.cold import run_cold_pass

    plat_list = _resolve_platforms(platforms)
    db = ResultsDB(db_path)
    block_list = [b.strip() for b in blocks.split(",") if b.strip()]
    block_specs = _block_specs(scale)
    try:
        cost_config = load_cost_config()
    except BenchmarkError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    # Run provenance: one record per invocation; every result row links to it.
    from .provenance import gather_provenance

    provenance_id = db.record_run_provenance(
        **gather_provenance(
            command="cold",
            scale_factor=scale,
            run_type="cold",
            platforms=plat_list,
            blocks=block_list,
        )
    )

    for platform in plat_list:
        try:
            cost = collector_for(platform, cost_config)
        except (KeyError, ValueError) as exc:
            typer.echo(f"skipping {platform}: {exc}", err=True)
            continue
        # One connector per block: TPC-DS and DSB live in different schemas
        # (tpcds_sf{scale} / dsb_sf{scale}), so a single connection can't serve
        # both. Mirrors the `sql` command's per-block resolution.
        for block in block_list:
            spec = block_specs.get(block)
            if spec is None:
                typer.echo(f"skipping unknown block '{block}'", err=True)
                continue
            schema, block_scale = spec
            try:
                connector = create_connector(
                    platform,
                    config={"schema": schema, "suite": block, "scale_factor": block_scale},
                )
            except (KeyError, ValueError) as exc:
                typer.echo(f"skipping {platform}/{block}: {exc}", err=True)
                continue
            try:
                run_cold_pass(
                    platform,
                    connector,
                    cost,
                    db,
                    block_scale,
                    blocks=[block],
                    provenance_id=provenance_id,
                    cost_config=cost_config,
                )
            except ConnectorError as exc:
                typer.echo(f"{platform}/{block}: connect failed — {exc}", err=True)
    _price_single_user(db, cost_config, plat_list, "cold")


@app.command("concurrency")
def concurrency(
    scale: Annotated[int, typer.Option("--scale")] = 100,
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "motherduck",
    blocks: Annotated[str, typer.Option("--blocks")] = "tpcds,dsb",
    concurrency_levels: Annotated[str, typer.Option("--levels")] = "1,5,10,20,50,100",
    compute_size: Annotated[
        str | None,
        typer.Option(
            "--compute-size",
            help=(
                "Sweep concurrency across compute sizes: override Snowflake's warehouse "
                "(xsmall|small|medium|large|xlarge → BENCHMARK_WH_<SIZE>). Each pass "
                "auto-labels with the size it used, so a scale × band × level grid doesn't "
                "collide. Snowflake only; ignored elsewhere."
            ),
        ),
    ] = None,
    tag: Annotated[
        str | None,
        typer.Option(
            "--tag",
            help=(
                "Label stamped into every row's metadata.tag (e.g. cold-r1), so repeated "
                "passes of one cell can be told apart after latest-wins collapses them."
            ),
        ),
    ] = None,
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Concurrency pass at SF100 across the requested concurrency levels.

    Exits 2 without ``results/cost_config.json`` (run ``capture-cost-config``).
    """
    _configure(verbose)
    from .cost.discovery import load_cost_config
    from .exceptions import BenchmarkError
    from .results import ResultsDB
    from .sql_benchmark.concurrency import run_concurrency_pass

    plat_list = _resolve_platforms(platforms)
    db = ResultsDB(db_path)
    # Required pre-step, same as the warm path: without it every worker's cost
    # collector falls back to constructor defaults and cost stops varying with
    # compute size. A missing file exits 2, as `sql` does.
    try:
        cost_config = load_cost_config()
    except BenchmarkError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    block_list = [b.strip() for b in blocks.split(",") if b.strip()]
    levels = [int(level) for level in concurrency_levels.split(",") if level.strip()]
    block_specs = _block_specs(scale)

    # Run provenance, same as `sql` and `cold`. Rows recorded before provenance
    # capture existed carry no git SHA / tool versions / executed-query hash.
    from .provenance import gather_provenance

    provenance_id = db.record_run_provenance(
        **gather_provenance(
            command="concurrency",
            scale_factor=scale,
            run_type="concurrency",
            platforms=plat_list,
            blocks=block_list,
        )
    )
    _logger().info("run provenance recorded", extra={"provenance_id": provenance_id})

    # --compute-size: Snowflake warehouse override, mirroring `sql`. Lets a
    # concurrency grid be swept across warehouse bands (each run auto-labels).
    wh_override: str | None = None
    if compute_size:
        valid = {"xsmall", "small", "medium", "large", "xlarge"}
        cs = compute_size.strip().lower()
        if cs not in valid:
            typer.echo(f"--compute-size must be one of {sorted(valid)}", err=True)
            raise typer.Exit(code=2)
        wh_override = f"BENCHMARK_WH_{cs.upper()}"
        ignored = [p for p in plat_list if p != "snowflake"]
        if ignored:
            typer.echo(
                f"--compute-size affects snowflake only; ignored for: {', '.join(ignored)}",
                err=True,
            )

    scales: set[int] = set()
    for platform in plat_list:
        # Per-block: each suite's tables live in its own schema at the right
        # scale (tpcds_sf{scale} / dsb_sf{scale}); results recorded under that
        # scale_factor. Mirrors the `sql` command.
        for block in block_list:
            spec = block_specs.get(block)
            if spec is None:
                typer.echo(f"skipping unknown block '{block}'", err=True)
                continue
            schema, block_scale = spec
            scales.add(block_scale)
            conn_config: dict[str, Any] = {
                "schema": schema,
                "suite": block,
                "scale_factor": block_scale,
            }
            if wh_override and platform == "snowflake":
                conn_config["warehouse"] = wh_override
            try:
                run_concurrency_pass(
                    platform=platform,
                    connector_config=conn_config,
                    # Must be the REAL captured config, not {}: with {} every
                    # worker's collector is built at constructor defaults, which
                    # makes cost invariant to compute size (see analysis/recost.py).
                    cost_config=cost_config,
                    results_db=db,
                    scale_factor=block_scale,
                    blocks=[block],
                    concurrency_levels=levels,
                    provenance_id=provenance_id,
                    tag=tag,
                )
            except Exception:
                _logger().exception("concurrency pass failed for %s/%s", platform, block)
    _price_shared_windows(db, cost_config, plat_list, scales)


@app.command("sustained")
def sustained(
    scale: Annotated[int, typer.Option("--scale")] = 100,
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "fabric",
    blocks: Annotated[str, typer.Option("--blocks")] = "tpcds",
    sessions: Annotated[
        int, typer.Option("--sessions", help="Sessions held busy for the whole pass.")
    ] = 50,
    minutes: Annotated[
        float,
        typer.Option(
            "--minutes",
            help="How long to keep handing out queries; queries running at the end finish.",
        ),
    ] = 30.0,
    tag: Annotated[
        str | None,
        typer.Option(
            "--tag",
            help=(
                "Label stamped into every row's metadata.tag (e.g. cold-r1), so repeated "
                "passes of one cell can be told apart after latest-wins collapses them."
            ),
        ),
    ] = None,
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Sustained load: hold N sessions busy on the suite for M minutes.

    The concurrency grid measures bursts, each suite once across N sessions, over in
    seconds to minutes. This keeps N sessions cycling through the suite for a fixed
    time instead, so load outlasts an engine's smoothing or queueing. Rows are run
    type ``sustained``, one ``run_number`` per pass through the suite, with
    ``metadata.submitted_s``; they stay out of every burst figure (see the
    ``sustained_results`` view). Size the compute out-of-band first, as for
    ``concurrency``. Exits 2 without ``results/cost_config.json`` (run
    ``capture-cost-config``).
    """
    _configure(verbose)
    from .cost.discovery import load_cost_config
    from .exceptions import BenchmarkError
    from .provenance import gather_provenance
    from .results import SUSTAINED_RUN_TYPE, ResultsDB
    from .sql_benchmark.concurrency import run_sustained_pass

    if sessions < 1 or minutes <= 0:
        typer.echo("--sessions must be at least 1 and --minutes above 0", err=True)
        raise typer.Exit(code=2)
    plat_list = _resolve_platforms(platforms)
    db = ResultsDB(db_path)
    try:
        cost_config = load_cost_config()
    except BenchmarkError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    block_list = [b.strip() for b in blocks.split(",") if b.strip()]
    block_specs = _block_specs(scale)
    provenance_id = db.record_run_provenance(
        **gather_provenance(
            command="sustained",
            scale_factor=scale,
            run_type=SUSTAINED_RUN_TYPE,
            platforms=plat_list,
            blocks=block_list,
        )
    )
    _logger().info("run provenance recorded", extra={"provenance_id": provenance_id})
    scales: set[int] = set()
    for platform in plat_list:
        for block in block_list:
            spec = block_specs.get(block)
            if spec is None:
                typer.echo(f"skipping unknown block '{block}'", err=True)
                continue
            schema, block_scale = spec
            scales.add(block_scale)
            conn_config: dict[str, Any] = {
                "schema": schema,
                "suite": block,
                "scale_factor": block_scale,
            }
            try:
                n = run_sustained_pass(
                    platform=platform,
                    connector_config=conn_config,
                    cost_config=cost_config,
                    results_db=db,
                    scale_factor=block_scale,
                    sessions=sessions,
                    minutes=minutes,
                    blocks=[block],
                    provenance_id=provenance_id,
                    tag=tag,
                )
                typer.echo(f"{platform}/{block}: {n} queries recorded over {minutes:g} min")
            except Exception:
                _logger().exception("sustained pass failed for %s/%s", platform, block)
    _price_shared_windows(db, cost_config, plat_list, scales)


# ---- Scale skips ---------------------------------------------------------


@app.command("skip-scale")
def skip_scale(
    scale: Annotated[int, typer.Option("--scale")],
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "all",
    blocks: Annotated[
        str,
        typer.Option(
            "--blocks",
            help="Suites skipped at this cell, or '*' for all of them. A per-suite "
            "skip is common — a run plan may skip DSB at 1 TB while TPC-DS runs "
            "there — and recording that whole-scale would overstate the omission.",
        ),
    ] = "*",
    compute_size: Annotated[
        str,
        typer.Option(
            "--compute-size",
            help="Compute size skipped at this cell, or '*' for every size. Omissions "
            "often have size granularity — Redshift runs 32/64/128 RPU at 1 TB and "
            "cannot finish at 8/16 — and recording that whole-scale reads as 'never "
            "ran there'.",
        ),
    ] = "*",
    run_type: Annotated[
        str,
        typer.Option(
            "--run-type",
            help="Run type skipped (warm, cold, concurrency, sustained), or '*' for "
            "all of them. A size can be out of scope single-user and measured under "
            "load: Redshift 8/16 RPU cannot finish the 1 TB warm suite and ran the 1 TB "
            "concurrency grid.",
        ),
    ] = "*",
    reason: Annotated[str, typer.Option("--reason")] = "operator decision",
    unmark: Annotated[bool, typer.Option("--unmark", help="Reverse a previous skip")] = False,
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Mark or un-mark a cell as intentionally skipped.

    The cell is scale x platform, narrowed by suite (``--blocks``), compute size
    (``--compute-size``) and run type (``--run-type``); each defaults to '*' (all).
    ``--unmark`` deletes the row with exactly that key.
    """
    _configure(verbose)
    from .results import ResultsDB

    plats = _resolve_platforms(platforms)
    db = ResultsDB(db_path)
    block_list = [b.strip() for b in blocks.split(",") if b.strip()] or ["*"]
    n = 0
    for p in plats:
        for block in block_list:
            if unmark:
                with db.connect() as con:
                    con.execute(
                        "DELETE FROM scale_skips WHERE scale_factor = ? AND platform = ?"
                        " AND block = ? AND compute_label = ? AND run_type = ?",
                        [scale, p, block, compute_size, run_type],
                    )
            else:
                db.record_scale_skip(
                    scale,
                    p,
                    reason,
                    "operator",
                    block=block,
                    compute_label=compute_size,
                    run_type=run_type,
                )
            n += 1
    scope = "all suites" if block_list == ["*"] else ", ".join(block_list)
    if run_type != "*":
        scope += f", {run_type} only"
    typer.echo(f"{'unmarked' if unmark else 'recorded'} {n} skip row(s) at SF{scale} ({scope})")


# ---- Connectivity --------------------------------------------------------


@app.command("connectivity-check")
def connectivity_check(
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "all",
    timeout_s: Annotated[
        float,
        typer.Option(
            "--timeout",
            help="Seconds to wait for each engine's SELECT 1 before reporting it as "
            "failed. The probes run in parallel, so this bounds the whole check.",
        ),
    ] = CONNECTIVITY_TIMEOUT_S,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Open each connector, run SELECT 1, print OK/FAIL table.

    Each engine is probed on its own daemon thread with a shared deadline, so an
    engine whose host no longer resolves (the Databricks connector retries a
    failed request for up to 900 s) is reported as failed instead of stalling
    the check. A probe still running at the deadline is abandoned, not joined.
    """
    import threading
    import time

    _configure(verbose)
    from .connectors.registry import create_connector
    from .exceptions import ConnectorError

    plats = _resolve_platforms(platforms)
    outcomes: dict[str, tuple[str, str]] = {}

    def _probe(p: str) -> None:
        try:
            with create_connector(p, config={}) as conn:
                result = conn.execute_query("SELECT 1")
                status = "ok" if not result.error_message else "fail"
                outcomes[p] = (status, result.error_message or "")
        except (KeyError, ValueError) as exc:
            outcomes[p] = ("skip", str(exc))
        except ConnectorError as exc:
            outcomes[p] = ("fail", str(exc))
        except Exception as exc:
            _logger().error("connectivity probe crashed", extra={"platform": p}, exc_info=True)
            outcomes[p] = ("fail", f"{type(exc).__name__}: {exc}")

    threads = {
        p: threading.Thread(target=_probe, args=(p,), name=f"probe-{p}", daemon=True) for p in plats
    }
    for t in threads.values():
        t.start()
    deadline = time.monotonic() + timeout_s
    typer.echo("platform     | status | note")
    typer.echo("-------------+--------+--------------------------------")
    for p, t in threads.items():
        t.join(max(0.0, deadline - time.monotonic()))
        status, note = outcomes.get(p, ("fail", f"no answer within {timeout_s:g} s"))
        typer.echo(f"{p:12s} | {status:6s} | {note[:60]}")


# ---- ClickBench data -----------------------------------------------------


@app.command("download-clickbench")
def download_clickbench(
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/clickbench/hits.parquet.raw"
    ),
    expected_sha256: Annotated[str | None, typer.Option("--sha256")] = None,
    max_retries: Annotated[
        int,
        typer.Option(
            "--max-retries",
            help="Auto-retry-with-resume attempts on network drops before "
            "giving up. Re-running always resumes too.",
        ),
    ] = 10,
    no_progress: Annotated[
        bool,
        typer.Option("--no-progress", help="Disable the download progress bar."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Resumable download of the ClickBench hits parquet (~14 GiB on disk; ~100 GB uncompressed).

    Resumes from a partial file via HTTP Range, and auto-retries transient
    network failures (so a dropped connection recovers on its own). Shows a progress bar
    (bytes/total, speed, ETA) on an interactive terminal. Default writes to
    ``hits.parquet.raw`` — the raw int-shaped upstream artifact;
    ``derive-clickbench-typed`` reads from there and writes ``hits.parquet``
    (the typed file Estuary captures).
    """
    _configure(verbose)
    from .datagen.clickbench import download_clickbench as _dl

    path = _dl(
        output_path,
        expected_sha256=expected_sha256,
        max_retries=max_retries,
        show_progress=not no_progress,
    )
    typer.echo(f"clickbench at {path}")


@app.command("derive-clickbench-typed")
def derive_clickbench_typed(
    input_path: Annotated[Path, typer.Option("--input")] = Path("data/clickbench/hits.parquet.raw"),
    output_path: Annotated[Path, typer.Option("--output")] = Path("data/clickbench/hits.parquet"),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Re-derive even if a typed parquet already exists "
            "(derive is skip-if-present by default).",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Derive a typed ClickBench parquet (full corpus) from the raw download.

    Casts EventDate to Date32 (DATE) and writes the three timestamp columns
    as RFC 3339 ISO 8601 strings ('YYYY-MM-DDTHH:MM:SSZ'). The output is
    what ``stage-data`` uploads and Estuary captures; the materializer
    creates DATE / TIMESTAMP WITH TIME ZONE columns from the collection
    schema's format declarations. Idempotent: skips if a typed parquet already
    exists (``--force`` to re-derive).
    """
    _configure(verbose)
    from .datagen.clickbench import derive_typed_parquet

    rows = derive_typed_parquet(input_path=input_path, output_path=output_path, force=force)
    typer.echo(f"wrote {rows:,} rows to {output_path}")


@app.command("hash-clickbench-ground-truth")
def hash_clickbench_ground_truth(
    parquet_path: Annotated[Path, typer.Option("--data")] = Path("data/clickbench/hits.parquet"),
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Hash 43 ClickBench queries in DuckDB → ground_truth_hashes (keyed SF100).

    Defaults to the typed ``hits.parquet`` — the same data Estuary loaded into
    the engines, so the reference matches what validate-sf1 tests. The view
    builder auto-detects column types, so the raw upstream
    ``hits.parquet.raw`` (EventDate int-days, *EventTime epoch-seconds) also
    works if passed via ``--data``.
    """
    _configure(verbose)
    from .datagen.clickbench import hash_clickbench_ground_truth as _hash
    from .results import ResultsDB

    summary = _hash(parquet_path, ResultsDB(db_path))
    ok = sum(1 for v in summary.values() if v["error_message"] is None)
    typer.echo(f"clickbench ground-truth: {ok}/{len(summary)} ok")


# ---- Reconcile, analysis, report -----------------------------------------


@app.command("reconcile")
def reconcile(
    platforms: Annotated[
        str,
        typer.Option(
            "--platforms",
            help="Comma list of engines, or 'all' for every engine with a reconciler "
            "(all but ClickHouse, whose metrics are recorded inline).",
        ),
    ] = "snowflake,databricks,redshift,fabric,motherduck",
    scale: Annotated[int, typer.Option("--scale")] = 1000,
    run_type: Annotated[str, typer.Option("--run-type")] = "warm",
    all_cells: Annotated[
        bool,
        typer.Option(
            "--all-cells",
            help="Reconcile EVERY (scale, run_type) cell that still holds unreconciled "
            "rows, ignoring --scale/--run-type. A campaign writes many cells and this "
            "command's defaults cover exactly one, so a bare invocation after a "
            "multi-cell campaign silently leaves most of it unreconciled.",
        ),
    ] = False,
    window_hours: Annotated[
        int,
        typer.Option(
            "--window-hours",
            help=(
                "Minimum history/billing lookback. Auto-widened to cover the oldest "
                "unreconciled row (plus a timezone-offset buffer so an engine account "
                "tz behind the harness tz can't clip it), so back-filling days-old runs "
                "needs no manual bump."
            ),
        ),
    ] = 24,
    rates_path: Annotated[
        Path | None, typer.Option("--rates", help="JSON rate overrides (e.g. RPU/CU $).")
    ] = None,
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Post-run reconciliation: back-fill authoritative server time +
    reconciled cost from each engine's history/billing into the results DB. The
    inline cost recorded during the run is an ESTIMATE; this upgrades it to the
    engine's tier (Databricks/Redshift/Fabric -> proportional; Snowflake keeps its
    rate-priced cost and records attributed credits beside it).

    Run after the engine's history settles (Snowflake 6-8h — widen --window-hours;
    Databricks ~minutes; Fabric >=15 min; Redshift within the 7-day window, cost
    needs a superuser).

    ``--platforms all`` expands to the engines with a reconciler
    (``analysis.reconcile.RECONCILERS``): every engine but ClickHouse, whose
    per-query metrics are recorded inline.
    """
    _configure(verbose)
    import json as _json
    from datetime import UTC, datetime, timedelta

    from .analysis.reconcile import (
        CPU_RECONCILERS,
        QUEUE_RECONCILERS,
        RECONCILERS,
        reconcile_platform,
    )
    from .connectors.registry import create_connector
    from .exceptions import ConnectorError
    from .results import ResultsDB

    plat_list = _resolve_platforms(platforms, eligible=RECONCILERS)
    db = ResultsDB(db_path)
    rates = _json.loads(rates_path.read_text(encoding="utf-8")) if rates_path else {}
    now = datetime.now(UTC)
    window_iso = ((now - timedelta(hours=window_hours)).isoformat(), now.isoformat())

    for platform in plat_list:
        try:
            connector = create_connector(platform)
        except (KeyError, ValueError) as exc:
            typer.echo(f"skipping {platform}: {exc}", err=True)
            continue
        # One cell, or every cell that still needs one. `--scale`/`--run-type`
        # default to a single (1000, warm) cell, which is right for a targeted
        # back-fill and wrong after a campaign that wrote ten.
        cells = (
            db.unreconciled_cells(
                platform,
                missing_queue=platform in QUEUE_RECONCILERS,
                missing_cpu=platform in CPU_RECONCILERS,
            )
            if all_cells
            else [(scale, run_type)]
        )
        if all_cells and not cells:
            typer.echo(f"{platform}: nothing to reconcile in any cell")
            continue
        try:
            with connector:
                for cell_scale, cell_run_type in cells:
                    summary = reconcile_platform(
                        platform,
                        connector,
                        db,
                        scale_factor=cell_scale,
                        run_type=cell_run_type,
                        window_iso=window_iso,
                        rates=rates,
                    )
                    typer.echo(f"{platform} SF{cell_scale}/{cell_run_type}: {summary}")
        except ConnectorError as exc:
            typer.echo(f"{platform}: connect failed — {exc}", err=True)
        except Exception:
            _logger().exception("reconcile failed for %s", platform)
        if platform == "motherduck":
            # Engine time lands here, so its queries re-price on it.
            from .analysis.recost import recost_rate_priced_rows
            from .cost.discovery import load_cost_config

            try:
                recost_rate_priced_rows(db, load_cost_config(), platforms=("motherduck",))
            except Exception:
                _logger().exception("motherduck pricing failed; run `recost-capacity`")


#: Engines whose shared compute `recost-capacity` prices by window.
CAPACITY_PRICED_PLATFORMS = (
    "fabric",
    "clickhouse",
    "motherduck",
    "databricks",
    "redshift",
    "bigquery",
    "snowflake",
)


def _price_single_user(
    db: Any, cost_config: dict[str, Any], platforms: list[str], run_type: str
) -> None:
    """Re-price what a sql or cold pass just recorded (offline).

    The inline price is client wall clock x the size's rate, and a reservation's
    slots consumed; single-user queries are priced on engine time (ClickHouse; a
    MotherDuck row again once `reconcile` back-fills its engine time), and BigQuery
    reservations on the baseline held for that time.
    """
    from .analysis.recost import (
        RATE_WINDOW_PLATFORMS,
        recost_bigquery_capacity,
        recost_rate_priced_rows,
    )

    rated = tuple(p for p in platforms if p in RATE_WINDOW_PLATFORMS)
    try:
        if rated:
            recost_rate_priced_rows(db, cost_config, platforms=rated, run_type=run_type)
        if "bigquery" in platforms:
            recost_bigquery_capacity(db)
    except Exception:
        _logger().exception("single-user pricing failed; run `recost-capacity`")


def _price_shared_windows(
    db: Any, cost_config: dict[str, Any], platforms: list[str], scales: set[int]
) -> None:
    """Window-price what a concurrency or sustained pass just recorded (offline).

    The inline cost charges each query the whole compute for its own elapsed time,
    which under load bills it once per query in flight, so the pass's rows are
    re-priced as windows before anything reads them. BigQuery reservations are priced
    on the baseline held. Fabric re-prices again, by CPU share, when `reconcile`
    back-fills its CPU time; Databricks is re-split, Redshift capped and Snowflake
    re-spread by `reconcile` once their billing lands.
    """
    from .analysis.recost import (
        RATE_WINDOW_PLATFORMS,
        recost_bigquery_capacity,
        recost_capacity_windows,
        recost_fabric_windows,
    )

    windowed = tuple(p for p in platforms if p in RATE_WINDOW_PLATFORMS)
    for scale in sorted(scales):
        try:
            if "fabric" in platforms:
                recost_fabric_windows(db, scale_factor=scale)
            if windowed:
                recost_capacity_windows(db, cost_config, platforms=windowed, scale_factor=scale)
            if "bigquery" in platforms:
                recost_bigquery_capacity(db)
        except Exception:
            _logger().exception("window pricing failed at SF%d; run `recost-capacity`", scale)


@app.command("recost-capacity")
def recost_capacity_cmd(
    platforms: Annotated[
        str,
        typer.Option(
            "--platforms",
            help="Any of fabric, clickhouse, motherduck, databricks, redshift, bigquery, "
            "snowflake (comma list), or 'all'.",
        ),
    ] = ",".join(CAPACITY_PRICED_PLATFORMS),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the before/after totals; write nothing.")
    ] = False,
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    cost_config_path: Annotated[Path, typer.Option("--cost-config")] = Path(
        "results/cost_config.json"
    ),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Re-price compute by the time queries held it, offline, on every engine.

    A query holds the compute for its engine time; a window is a stretch in which at
    least one query of a pass ran (one warm statement is its own), and costs what the
    compute billed for that time, shared across its queries (analysis.recost):
    Fabric by CPU share (SKU time plus bursting; elapsed share where CPU time is
    gone); ClickHouse and MotherDuck's per-second Ducklings at the size's rate x the
    window, by engine time; Databricks keeps each billed hour's share up to list rate
    x its windows' time, split by execution time; Redshift keeps its billed share up
    to RPU rate x the window, by execution time; BigQuery reservations price the
    baseline slots held, by slot share; Snowflake like ClickHouse, at list rate x
    engine time (Standard edition), its attributed credits kept as a cross-check.
    `reconcile`, `sql`,
    `cold` and the concurrency and sustained commands do this for what they touch;
    this re-prices everything. Idempotent.
    """
    _configure(verbose)
    from .analysis.recost import (
        cap_redshift_idle,
        price_snowflake_on_engine_time,
        recost_bigquery_capacity,
        recost_fabric_windows,
        recost_rate_priced_rows,
        resplit_databricks_hours,
    )
    from .cost.discovery import load_cost_config
    from .results import ResultsDB

    wanted = _resolve_platforms(platforms, eligible=CAPACITY_PRICED_PLATFORMS)
    db = ResultsDB(db_path)
    unknown = sorted(set(wanted) - set(CAPACITY_PRICED_PLATFORMS))
    if unknown:
        typer.echo(f"not capacity-priced: {', '.join(unknown)}", err=True)
        raise typer.Exit(code=2)
    runs: list[tuple[str, dict[str, Any]]] = []
    if "fabric" in wanted:
        runs.append(("fabric", recost_fabric_windows(db, dry_run=dry_run)))
    rate_platforms = tuple(p for p in wanted if p in ("clickhouse", "motherduck"))
    if rate_platforms:
        summary = recost_rate_priced_rows(
            db, load_cost_config(cost_config_path), platforms=rate_platforms, dry_run=dry_run
        )
        runs.append((",".join(rate_platforms), summary))
    if "databricks" in wanted:
        runs.append(("databricks", resplit_databricks_hours(db, dry_run=dry_run)))
    if "redshift" in wanted:
        runs.append(("redshift", cap_redshift_idle(db, dry_run=dry_run)))
    if "bigquery" in wanted:
        runs.append(("bigquery", recost_bigquery_capacity(db, dry_run=dry_run)))
    if "snowflake" in wanted:
        runs.append(("snowflake", price_snowflake_on_engine_time(db, dry_run=dry_run)))
    for name, summary in runs:
        for c in summary["cells"]:
            where = " ".join(
                str(c[k]) for k in ("platform", "run_type", "compute_label", "basis") if k in c
            )
            typer.echo(
                f"  {where:<44} {c['rows']:>6} rows"
                f"  ${c['before_usd']:>9.2f} -> ${c['after_usd']:>9.2f}"
            )
        typer.echo(
            f"{name}{' (dry run)' if dry_run else ''}: ${summary['total_before_usd']:.2f}"
            f" -> ${summary['total_after_usd']:.2f}"
        )


@app.command("capture-cost-config")
def capture_cost_config_cmd(
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "all",
    out: Annotated[Path, typer.Option("--out")] = Path("results/cost_config.json"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Introspect each engine's cost driver and write the cost-config file.

    Reads the cost-driving knob (MotherDuck tier, ClickHouse vCPU×replicas,
    BigQuery edition, …) live from each platform.
    Unreachable / non-self-reporting platforms are recorded under ``errors``; the
    file is always written and always valid JSON, merged into an existing one. The
    ``sql``, ``cold``, ``concurrency`` and ``sustained`` passes exit 2 without it
    and warn for any platform missing from it.
    """
    _configure(verbose)
    from .cost.discovery import capture_cost_config

    plats = _resolve_platforms(platforms)
    doc = capture_cost_config(plats, out)
    typer.echo(f"\ncost-config → {out}")
    for platform in plats:
        if platform in doc["platforms"]:
            typer.echo(f"  ok   {platform:<12} {doc['platforms'][platform]}")
        else:
            typer.echo(f"  miss {platform:<12} {doc['errors'].get(platform, 'unknown')}")
    typer.echo(f"\n{len(doc['platforms'])} captured, {len(doc['errors'])} unavailable")


@app.command("analyze")
def analyze(
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Emit per-export Parquet files for the report and the explorer to consume."""
    _configure(verbose)
    from .analysis.agreement import emit_row_count_agreement
    from .analysis.clickbench import emit_clickbench_summary
    from .analysis.concurrency_grid import emit_concurrency_grid
    from .analysis.curves import emit_cost_curves
    from .analysis.motherduck_cost import emit_motherduck_cost
    from .analysis.pattern_load import emit_pattern_load
    from .analysis.patterns import emit_pattern_summary
    from .analysis.query_catalog import emit_query_catalog
    from .analysis.rankings import emit_rankings
    from .analysis.rate_card import emit_rate_card
    from .analysis.scale_skips import emit_scale_skips
    from .analysis.size_ladder import emit_size_ladder
    from .analysis.skew import emit_skew_impact
    from .analysis.skew_load import emit_skew_load

    emit_cost_curves(db_path)
    emit_clickbench_summary(db_path)
    emit_concurrency_grid(db_path)
    emit_pattern_summary(db_path)
    emit_pattern_load(db_path)
    emit_rankings(db_path)
    emit_skew_impact(db_path)
    emit_skew_load(db_path)
    emit_motherduck_cost(db_path)
    emit_row_count_agreement(db_path)
    emit_size_ladder(db_path)
    emit_scale_skips(db_path)
    emit_rate_card(db_path)
    emit_query_catalog()


@app.command("render-status")
def render_status(
    template: Annotated[
        Path,
        typer.Option(
            "--template",
            help="HTML template whose {{fact:...}} placeholders to resolve. Required: "
            "the repository ships none.",
        ),
    ],
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Render a status page from an HTML template you supply, resolving `{{fact:...}}`.

    The status page restates the report's headline figures for an outside reader, in
    hand-written HTML, the structure most prone to drifting from the data. Its figures
    therefore come from the same `{{fact:...}}` resolver the report sections use,
    against the same database, so the summary cannot disagree with what it summarizes.

    An unknown or unresolvable fact is a hard error: publishing a literal
    `{{fact:...}}`, or a quietly stale number, are both worse than failing here.

    The template is yours to supply: `--template` is required, the repository ships
    none, and a path that is not a file exits 2. Any HTML file works; the facts it
    can name are the keys of `report.narrative.FACTS`. The page is written beside
    the template as `benchmark-status-<date>.html` unless `--output` names a file.

    `--results-db` accepts a backup copy. Point it at one while a campaign is writing
    the live database: DuckDB is single-writer, and a concurrent reader can make the
    writer fail to take its lock.
    """
    _configure(verbose)
    from datetime import UTC, datetime

    import duckdb as _duckdb

    from .report.narrative import resolve_facts

    if not template.is_file():
        typer.echo(
            f"no status template at {template}: --template names an HTML file you "
            "supply, whose {{fact:...}} placeholders resolve against the results DB "
            "(the repository ships none)",
            err=True,
        )
        raise typer.Exit(code=2)
    if not db_path.exists():
        typer.echo(f"no results DB at {db_path}", err=True)
        raise typer.Exit(code=2)

    dest = output or template.with_name(
        f"benchmark-status-{datetime.now(UTC).strftime('%Y-%m-%d')}.html"
    )
    con = _duckdb.connect(str(db_path), read_only=True)
    try:
        rendered = resolve_facts(template.read_text(encoding="utf-8"), con, source=template)
    finally:
        con.close()
    dest.write_text(rendered, encoding="utf-8")
    typer.echo(f"status page → {dest}")


@app.command("coverage-audit")
def coverage_audit(
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    kinds: Annotated[
        str | None,
        typer.Option(
            "--kinds",
            help="Comma-separated subset: designated-missing, sweep-hole, load-hole, "
            "censored, level-gap, short-level, short-pass.",
        ),
    ] = None,
    strict: Annotated[bool, typer.Option("--strict", help="Exit 1 if any gap is found.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Compare each engine's ladders against the results DB and print every gap.

    Expected coverage is every size on each engine's single-user and under-load
    ladder (run_plan.yaml ``sweep`` / ``load_sweep``) at every scale the plan covers,
    for every suite, plus the designated size, minus the recorded skips.

    Coverage gaps are easy to find by accident — a missing ladder, a censored
    ceiling, an unreconciled cell — because they are visible in the data the whole
    time while nothing is asking. This asks.

    A gap is not automatically a defect: a short ladder can be a deliberate stop. But
    it should be a *decided* stop, recorded with `skip-scale`, rather than one nobody
    noticed. Each gap prints the command that would close it.
    """
    _configure(verbose)
    import duckdb as _duckdb

    from .analysis.coverage import KINDS, audit

    if not db_path.exists():
        typer.echo(f"no results DB at {db_path}", err=True)
        raise typer.Exit(code=2)
    wanted = {k.strip() for k in kinds.split(",")} if kinds else None
    con = _duckdb.connect(str(db_path), read_only=True)
    try:
        gaps = [g for g in audit(con) if wanted is None or g.kind in wanted]
    finally:
        con.close()

    if not gaps:
        typer.echo("no coverage gaps: the run plan is fully measured")
        return

    by_kind: dict[str, list] = {}
    for g in gaps:
        by_kind.setdefault(g.kind, []).append(g)
    for kind in KINDS:
        group = by_kind.get(kind, [])
        if not group:
            continue
        typer.echo(f"\n{kind} — {len(group)}")
        for g in group:
            where = "ClickBench" if g.block == "clickbench" else f"SF{g.scale_factor}"
            typer.echo(f"  {g.platform:11} {where:10} {g.compute_label:24} {g.detail}")
            typer.echo(f"      fill: {g.fill_command}")
    typer.echo(f"\n{len(gaps)} gaps across {len(by_kind)} kinds")
    if strict:
        raise typer.Exit(code=1)


@app.command("verify-figures")
def verify_figures(
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    sections_dir: Annotated[Path, typer.Option("--sections-dir")] = Path("report/sections"),
    tolerance: Annotated[
        float,
        typer.Option("--tolerance", help="Relative tolerance on numeric cells (default 1%)."),
    ] = 0.01,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Also fail when a table omits rows the data has (coverage findings).",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Recompute every hand-authored table in `report/sections/` from the results DB
    and fail on drift.

    The errors a report like this is prone to come from selecting the wrong slice (the
    wrong compute tier, the wrong dedup key, the wrong query set), not from bad
    arithmetic, and each looks plausible on the page. Prose review cannot catch that;
    recomputation can. Exit code 1 means a published number disagrees with the
    database it claims to come from.
    """
    _configure(verbose)
    import duckdb as _duckdb

    from .report import verifiers as _verifiers  # noqa: F401  (registers the verifiers)
    from .report.figures import FigureError, verify

    if not db_path.exists():
        typer.echo(f"no results DB at {db_path}", err=True)
        raise typer.Exit(code=2)
    con = _duckdb.connect(str(db_path), read_only=True)
    try:
        drifts = verify(con, sections_dir, tolerance=tolerance)
    except FigureError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    finally:
        con.close()

    values = [d for d in drifts if d.kind == "value"]
    coverage = [d for d in drifts if d.kind == "coverage"]

    if coverage:
        # Reported always, but never fatal on its own: a curated table is a legitimate
        # editorial choice, it just has to be a deliberate one.
        typer.echo(
            f"{len(coverage)} coverage findings — the data holds rows these tables "
            f"do not show (or shows rows the data no longer has):"
        )
        for drift in coverage:
            typer.echo(f"  {drift.describe()}")
        typer.echo("")

    if not values:
        typer.echo(f"all published figures match the database ({len(coverage)} coverage notes)")
        if coverage and strict:
            raise typer.Exit(code=1)
        return

    typer.echo(f"{len(values)} published figures disagree with the database:\n", err=True)
    for drift in values:
        typer.echo(f"  {drift.describe()}", err=True)
    raise typer.Exit(code=1)


@app.command("report")
def report(
    db_path: Annotated[Path, typer.Option("--results-db")] = Path("results/benchmark.duckdb"),
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path("report"),
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Regenerate report/draft.md plus charts."""
    _configure(verbose)
    from .report.generator import generate_report

    path = generate_report(db_path, output_dir)
    typer.echo(f"report draft at {path}")


@app.command("export-terraform-outputs")
def export_terraform_outputs(
    terraform_dir: Annotated[Path, typer.Option("--terraform-dir")] = Path("terraform"),
    output: Annotated[Path, typer.Option("--output")] = Path("terraform/outputs.json"),
    materialize_keys: Annotated[
        bool,
        typer.Option(
            "--materialize-keys/--no-materialize-keys",
            help="Also write terraform-managed GCP SA keys to the paths the connectors "
            "expect (BIGQUERY_GCP_SA_KEY, ESTUARY_BIGQUERY_SERVICE_ACCOUNT_KEY), 0600. "
            "Removes the manual 'decode the key + drop the file' step.",
        ),
    ] = True,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Export `terraform output -json` to a file the harness reads for per-scale
    provisioned compute (recorded into engine_environment), and materialize the
    terraform-managed GCP SA-key files the connectors authenticate with. Run after
    each `terraform apply`. Reads the file at benchmark time, never shells to
    terraform during a run.
    """
    import subprocess

    _configure(verbose)
    cmd = ["terraform", f"-chdir={terraform_dir}", "output", "-json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        typer.echo("terraform not found on PATH", err=True)
        raise typer.Exit(1) from None
    except subprocess.CalledProcessError as exc:
        typer.echo(f"terraform output failed: {(exc.stderr or '').strip()}", err=True)
        raise typer.Exit(1) from None
    output.write_text(proc.stdout)
    typer.echo(f"wrote terraform outputs -> {output}")

    if materialize_keys:
        import json as _json

        from .terraform_outputs import materialize_credential_files

        try:
            written = materialize_credential_files(_json.loads(proc.stdout))
        except ValueError:
            typer.echo("could not parse terraform outputs for key materialization", err=True)
            written = []
        for path in written:
            typer.echo(f"materialized SA key -> {path}")


@app.command("e2e")
def e2e(
    platforms: Annotated[str, typer.Option("--platforms", help=PLATFORMS_HELP)] = "all",
    scale: Annotated[int, typer.Option("--scale")] = 1,
    blocks: Annotated[str, typer.Option("--blocks")] = "tpcds,dsb,clickbench",
    from_step: Annotated[
        str | None,
        typer.Option("--from", help="Resume from a phase key (e.g. stage, materialize, sql)."),
    ] = None,
    skip: Annotated[str, typer.Option("--skip", help="Comma list of phase keys to skip.")] = "",
    keep_going: Annotated[
        bool, typer.Option("--keep-going", help="Don't stop at the first failed step.")
    ] = False,
    load_timeout: Annotated[
        int,
        typer.Option(
            "--load-timeout",
            help="Seconds validate-loads polls for the Estuary backfill to catch up "
            "after the materialization publish (the publish returns before the load finishes).",
        ),
    ] = DEFAULT_LOAD_TIMEOUT,
    recapture_cost_config: Annotated[
        bool,
        typer.Option(
            "--recapture-cost-config",
            help="Run capture-cost-config even when results/cost_config.json exists "
            "(it merges into the file). Without it the step runs only when the file "
            "is missing.",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Run the full pipeline end-to-end across engines: generate -> stage (S3) ->
    Estuary capture/materialize -> validate-loads -> capture-cost-config -> sql ->
    analyze/report.

    Drives the existing subcommands in order (per engine where needed). The
    capture-cost-config step is skipped when ``results/cost_config.json`` exists,
    unless ``--recapture-cost-config``. A real run needs credentials
    (env/<section>.env), terraform-provisioned infra, and `benchmark flow-auth`
    (Estuary). See docs/E2E_LOADING.md.
    """
    _configure(verbose)
    from .e2e import run_e2e

    plats = _resolve_platforms(platforms)
    blk = tuple(b.strip() for b in blocks.split(",") if b.strip())
    skips = [s.strip() for s in skip.split(",") if s.strip()]
    results = run_e2e(
        plats,
        scale,
        blk,
        from_key=from_step or None,
        skip=skips,
        keep_going=keep_going,
        load_timeout=load_timeout,
        recapture_cost_config=recapture_cost_config,
    )
    for name, status in results:
        typer.echo(f"  {status:8} {name}")
    if any(status == "failed" for _, status in results):
        raise typer.Exit(code=1)

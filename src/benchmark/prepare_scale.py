"""``prepare-scale``: the per-scale, per-platform readiness gate.

The assessment is read-only: for each platform it evaluates two readiness axes
and reports where things stand.

  * **data axis**   — row counts vs ``expected_counts.json`` (reuses
    ``staging.load_check.validate_engine_loads``).
  * **compute axis** — the live ``runtime_compute()`` snapshot diffed against the
    run-plan target for the scale (reuses runner's canonical compute-check spec).

Every per-platform step is wrapped so an unreachable engine is reported
``unreachable`` rather than aborting the sweep.

For off-target compute, :func:`remediation_plan` proposes sizing-var edits to the
tool-owned tfvars JSON and manual steps for out-of-band engines.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from rich.table import Table

from .connectors.registry import create_connector
from .logging_config import bound_platform
from .run_plan import get_run_plan

# _COMPUTE_CHECKS / _normalize_warehouse_size: runner's canonical compute-check spec (single source).
from .sql_benchmark.runner import _COMPUTE_CHECKS, _normalize_warehouse_size
from .staging.load_check import TableTick, validate_engine_loads

logger = logging.getLogger("benchmark.prepare_scale")

# Axis statuses.
READY = "ready"
PENDING = "pending"
SKIP = "skip"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"

DEFAULT_SUITES: tuple[str, ...] = ("tpcds", "dsb", "clickbench")


def _one_line(msg: str) -> str:
    """Collapse whitespace/newlines to one logical line (the rich table wraps it,
    so the full message stays visible — no truncation)."""
    return " ".join(msg.split())


@dataclass
class AxisResult:
    """One readiness axis (data or compute) for one platform."""

    status: str  # READY | PENDING | SKIP | UNREACHABLE | UNKNOWN
    detail: str
    # Raw per-table failure strings (data axis only) so remediation can tailor
    # suggestions per suite + kind; empty for compute / ready axes.
    failures: list[str] = field(default_factory=list)


@dataclass
class PlatformReadiness:
    """Both axes for one platform at one scale."""

    platform: str
    data: AxisResult
    compute: AxisResult

    @property
    def ready(self) -> bool:
        return self.data.status == READY and self.compute.status == READY

    @property
    def overall(self) -> str:
        if self.compute.status == SKIP:
            return SKIP
        # A skipped data axis (--compute-only) doesn't count for or against readiness.
        axes = [self.compute.status]
        if self.data.status != SKIP:
            axes.append(self.data.status)
        if UNREACHABLE in axes:
            return UNREACHABLE
        if all(s == READY for s in axes):
            return READY
        return PENDING


def _matches_target(platform: str, live: dict, target: str | int) -> bool | None:
    """Whether the live compute matches the run-plan target on the platform's
    comparable dimension. ``None`` when not comparable (no spec, or live missing).
    """
    spec = _COMPUTE_CHECKS.get(platform)
    if spec is None:
        return None
    actual_key, _provisioned_key, kind = spec
    running = live.get(actual_key)
    if running is None or target is None:
        return None
    if kind == "int":
        try:
            return int(running) == int(target)
        except (TypeError, ValueError):
            return None
    return _normalize_warehouse_size(running) == _normalize_warehouse_size(target)


def _live_value(platform: str, live: dict) -> object:
    """The live value on the platform's comparable dimension (for display)."""
    spec = _COMPUTE_CHECKS.get(platform)
    return live.get(spec[0]) if spec else None


def compute_axis(platform: str, scale: int) -> AxisResult:
    """Diff live ``runtime_compute()`` against the run-plan target for the scale."""
    target = get_run_plan().target(platform, scale)
    if target is None:
        return AxisResult(SKIP, "no run-plan cell for this scale")
    # Stamp the connector's logs (connect / introspection errors) with the platform.
    with bound_platform(platform):
        try:
            connector = create_connector(platform, config={"scale_factor": scale})
        except Exception as exc:  # build failure (missing SDK, bad config)
            return AxisResult(UNREACHABLE, f"connector build failed: {exc}")
        try:
            with connector:
                live = connector.runtime_compute() or {}
        except Exception as exc:  # connect / auth / introspection failure
            return AxisResult(UNREACHABLE, str(exc))

    # BigQuery on-demand (no reservation): allowed at low scales (cheap, no
    # reservation to pay for) but denied at high scales where an unbounded
    # on-demand scan would run up a huge bill — there a slot reservation is
    # required. The run-plan's on_demand policy decides per scale.
    if platform == "bigquery" and live.get("slot_capacity") is None:
        if get_run_plan().on_demand_allowed("bigquery", scale) is False:
            return AxisResult(
                PENDING,
                f"on-demand not allowed at SF{scale} — reserve {target} slots "
                "(set bigquery_reservation_enabled=true + bigquery_reservation_slot_capacity)",
            )
        return AxisResult(
            READY,
            "on-demand (no reservation) — set bigquery_reservation_enabled=true "
            "+ a slot count for a reservation",
        )

    matched = _matches_target(platform, live, target)
    live_val = _live_value(platform, live)
    if matched is True:
        return AxisResult(READY, f"{live_val} == target {target}")
    if matched is False:
        return AxisResult(
            PENDING, f"live {live_val if live_val is not None else '—'}, target {target}"
        )
    # Not comparable: live missing/empty (e.g. no reservation) — not confirmed at target.
    return AxisResult(
        PENDING, f"live {live_val if live_val is not None else '—'} (unverified), target {target}"
    )


def data_axis(
    platform: str,
    scale: int,
    suites: tuple[str, ...] = DEFAULT_SUITES,
    *,
    on_table: Callable[[TableTick], None] | None = None,
) -> AxisResult:
    """Row-count check for the scale's data via ``validate_engine_loads``.

    ``on_table`` is threaded straight through to drive a live display (the CLI
    passes one on a TTY); None keeps the plain fail-fast pass used elsewhere."""
    try:
        failures = validate_engine_loads([platform], scale, suites=suites, on_table=on_table)
    except (FileNotFoundError, KeyError) as exc:
        return AxisResult(UNKNOWN, f"expected-counts manifest unavailable: {exc}")
    except Exception as exc:  # unexpected — surface, don't abort
        return AxisResult(UNREACHABLE, str(exc))
    if not failures:
        return AxisResult(READY, "loaded")
    if all("connect failed" in f for f in failures):
        return AxisResult(UNREACHABLE, _one_line(failures[0]), failures=failures)
    suffix = f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
    return AxisResult(PENDING, _one_line(failures[0]) + suffix, failures=failures)


def assess(
    platforms: list[str],
    scale: int,
    suites: tuple[str, ...] = DEFAULT_SUITES,
    *,
    check_data: bool = True,
    on_table: Callable[[TableTick], None] | None = None,
) -> list[PlatformReadiness]:
    """Evaluate both axes for every platform. Resilient: one platform's failure
    never aborts the others (each axis already catches; this is defense in depth).

    ``check_data=False`` (``--compute-only``) skips the row-count sweep, which is
    the slow part — useful when you only care about compute sizing right now.
    ``on_table`` is forwarded to the data axis so the CLI can render a live display
    of the sweep; None leaves it as plain logging."""
    results: list[PlatformReadiness] = []
    for platform in platforms:
        try:
            compute = compute_axis(platform, scale)
        except Exception as exc:  # last-resort guard
            compute = AxisResult(UNREACHABLE, f"compute check errored: {exc}")
        if compute.status == SKIP:
            data = AxisResult(SKIP, "platform skipped at this scale")
        elif not check_data:
            data = AxisResult(SKIP, "skipped (--compute-only)")
        else:
            try:
                data = data_axis(platform, scale, suites, on_table=on_table)
            except Exception as exc:
                data = AxisResult(UNREACHABLE, f"data check errored: {exc}")
        results.append(PlatformReadiness(platform, data, compute))
    return results


_SYMBOL = {READY: "✓", PENDING: "✗", SKIP: "–", UNREACHABLE: "!", UNKNOWN: "?"}
_STATUS_STYLE = {
    READY: "green",
    PENDING: "red",
    SKIP: "dim",
    UNREACHABLE: "yellow",
    UNKNOWN: "yellow",
}


def _cell(axis: AxisResult) -> str:
    """A table cell: colored status marker + the full (wrapped) detail."""
    style = _STATUS_STYLE.get(axis.status, "white")
    return f"[{style}]{_SYMBOL.get(axis.status, '?')} {axis.status}[/]\n{axis.detail}"


def build_board_table(scale: int, results: list[PlatformReadiness]) -> Table:
    """Readiness board as a rich Table — columns align, details wrap (no truncation)."""
    ready = sum(1 for r in results if r.overall == READY)
    skipped = sum(1 for r in results if r.overall == SKIP)
    pending = len(results) - ready - skipped

    table = Table(
        title=f"SF{scale} readiness — {ready} ready, {pending} pending, {skipped} skipped",
        title_justify="left",
        show_lines=True,
    )
    table.add_column("platform", style="bold", no_wrap=True)
    table.add_column("data", overflow="fold", ratio=1)
    table.add_column("compute", overflow="fold", ratio=1)
    for r in results:
        table.add_row(r.platform, _cell(r.data), _cell(r.compute))
    return table


# ---------------------------------------------------------------------------
# Source axes (shared upstream — one check per suite, not per platform)
#
# Two upstream checks that feed every engine, so they're reported once per suite
# rather than per platform:
#   * S3 source     — staged parquet footer counts vs the manifest. With the
#                     per-platform data axis (DB vs manifest) this asserts
#                     S3 == DB end-to-end, and pinpoints divergence (bad source
#                     vs bad load).
#   * Estuary       — collections present + non-empty + fresh (no row counts;
#                     row parity is covered transitively by S3 == DB).
# ---------------------------------------------------------------------------


@dataclass
class SuiteSources:
    """Upstream source readiness for one suite (S3 + Estuary). Either axis is
    ``None`` when its check was disabled."""

    suite: str
    s3: AxisResult | None
    estuary: AxisResult | None


def _expected_tables(suite: str, scale: int, data_root: Path) -> list[str] | None:
    """Table names a suite is expected to expose at a scale, from the manifest.

    ClickBench is the single, un-scaled ``hits`` collection. TPC-DS/DSB read the
    table set from ``data/<suite>/expected_counts.json`` for the scale — the same
    source the data axis validates against. ``None`` => no manifest entry (so the
    Estuary axis is UNKNOWN, mirroring the data axis)."""
    if suite == "clickbench":
        return ["hits"]
    manifest = data_root / suite / "expected_counts.json"
    if not manifest.exists():
        return None
    counts = json.loads(manifest.read_text(encoding="utf-8"))
    scale_counts = counts.get(str(scale))
    if not scale_counts:
        return None
    return sorted(scale_counts)


def _fmt_epoch(epoch: int) -> str:
    """UTC date for a fragment modTime; ``—`` when unknown."""
    if not epoch:
        return "—"
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d")


def s3_source_axis(suite: str, scale: int, data_root: Path = Path("data")) -> AxisResult:
    """Staged-S3 parquet footer counts vs the manifest (== the target-DB baseline).

    Reads each staged ``<table>.parquet`` **footer** (a range read — cheap and
    size-independent) and compares to the expected-count manifest. Combined with
    the data axis (DB vs manifest), this asserts the staged S3 source matches the
    loaded DB row-for-row. Uses the long-lived Estuary S3 keys, independent of the
    ambient AWS credential chain (which is often an expired SSO session)."""
    from .settings import get_settings
    from .staging.expected_counts import make_s3_filesystem, parquet_footer_rows
    from .staging.load_check import _suite_plan

    try:
        plan = _suite_plan(suite, scale, data_root)
    except (FileNotFoundError, KeyError) as exc:
        return AxisResult(UNKNOWN, f"expected-counts manifest unavailable: {exc}")
    if plan is None:
        return AxisResult(SKIP, "no S3 source for suite")
    _schema, expected = plan

    s = get_settings()
    est = s.ESTUARY
    kid = est.S3_AWS_ACCESS_KEY_ID.get_secret_value() if est.S3_AWS_ACCESS_KEY_ID else None
    sec = est.S3_AWS_SECRET_ACCESS_KEY.get_secret_value() if est.S3_AWS_SECRET_ACCESS_KEY else None
    if not kid or not sec:
        return AxisResult(UNREACHABLE, "ESTUARY_S3_AWS_* keys not set")

    try:
        s3 = make_s3_filesystem(s.AWS.REGION, kid, sec)

        def _rows(table: str) -> tuple[str, int | None]:
            key = f"{s.S3_BUCKET}/{_staged_path(suite, table, scale)}"
            try:
                return table, parquet_footer_rows(s3, key)
            except Exception:  # object absent / unreadable
                return table, None

        with ThreadPoolExecutor(max_workers=8) as pool:
            got = dict(pool.map(_rows, expected))
    except Exception as exc:  # filesystem build / auth failure
        return AxisResult(UNREACHABLE, f"S3 read failed: {exc}")

    total = len(expected)
    missing = [t for t, n in got.items() if n is None]
    if missing:
        head = ", ".join(sorted(missing)[:3]) + (
            f" (+{len(missing) - 3})" if len(missing) > 3 else ""
        )
        return AxisResult(PENDING, f"{total - len(missing)}/{total} present in S3; missing: {head}")

    mismatches = [(t, got[t], expected[t]) for t in expected if got[t] != expected[t]]
    if mismatches:
        t, g, e = mismatches[0]
        suffix = f" (+{len(mismatches) - 1} more)" if len(mismatches) > 1 else ""
        return AxisResult(
            PENDING,
            f"{len(mismatches)}/{total} mismatch vs DB/manifest: {t} S3={g:,} exp={e:,}{suffix}",
        )
    return AxisResult(READY, f"{total}/{total} match DB/manifest ({sum(got.values()):,} rows)")


def estuary_axis(suite: str, scale: int, data_root: Path = Path("data")) -> AxisResult:
    """Presence + freshness of a suite's Estuary collections (no row counts).

    Confirms every expected collection exists and holds data (max journal offset
    > 0), and reports the most recent fragment write. Requires flowctl auth; an
    auth/connectivity failure degrades to UNREACHABLE rather than aborting."""
    from .staging import estuary as est

    tables = _expected_tables(suite, scale, data_root)
    if tables is None:
        return AxisResult(UNKNOWN, f"no manifest entry for SF{scale}")

    names = [est.collection_name(suite, scale, t) for t in tables]
    try:
        states = est.collection_states(names)
    except Exception as exc:  # flowctl missing / auth expired / network
        return AxisResult(UNREACHABLE, f"flowctl probe failed: {exc}")

    total = len(states)
    present = sum(1 for s in states if s.present)
    nonempty = sum(1 for s in states if s.present and s.journal_bytes > 0)
    last = _fmt_epoch(max((s.last_write for s in states), default=0))
    gib = sum(s.journal_bytes for s in states) / (1024**3)

    if present < total:
        missing = [s.name.rsplit("/", 1)[-1] for s in states if not s.present]
        head = ", ".join(missing[:3]) + (f" (+{len(missing) - 3})" if len(missing) > 3 else "")
        return AxisResult(PENDING, f"{present}/{total} present; missing: {head}")
    if nonempty < total:
        empty = [s.name.rsplit("/", 1)[-1] for s in states if s.journal_bytes == 0]
        head = ", ".join(empty[:3]) + (f" (+{len(empty) - 3})" if len(empty) > 3 else "")
        return AxisResult(PENDING, f"{nonempty}/{total} non-empty; empty: {head}")
    return AxisResult(READY, f"{total}/{total} non-empty, {gib:.1f} GiB, last {last}")


def assess_sources(
    suites: tuple[str, ...],
    scale: int,
    *,
    check_s3: bool = True,
    check_estuary: bool = True,
    data_root: Path = Path("data"),
) -> list[SuiteSources]:
    """S3 + Estuary source axes for every suite. flowctl is authed once; on auth
    failure the Estuary axis is UNREACHABLE for every suite (the collections may
    be fine — we just couldn't look) while the S3 axis still runs."""
    estuary_auth_error: str | None = None
    if check_estuary:
        from .staging import estuary as est

        try:
            est.auth()
        except Exception as exc:
            estuary_auth_error = str(exc)
            logger.warning("flowctl auth failed; Estuary axis unavailable: %s", exc)

    rows: list[SuiteSources] = []
    for suite in suites:
        s3_res = s3_source_axis(suite, scale, data_root) if check_s3 else None
        if not check_estuary:
            est_res = None
        elif estuary_auth_error is not None:
            est_res = AxisResult(UNREACHABLE, f"flowctl auth failed: {estuary_auth_error}")
        else:
            est_res = estuary_axis(suite, scale, data_root)
        rows.append(SuiteSources(suite, s3_res, est_res))
    return rows


def build_sources_table(scale: int, rows: list[SuiteSources]) -> Table:
    """Upstream source readiness (S3 + Estuary) as a rich Table, one row per
    suite. A column is omitted when that axis was disabled for every suite."""

    def _suite_ready(r: SuiteSources) -> bool:
        return all(a.status == READY for a in (r.s3, r.estuary) if a is not None)

    has_s3 = any(r.s3 is not None for r in rows)
    has_estuary = any(r.estuary is not None for r in rows)
    ready = sum(1 for r in rows if _suite_ready(r))

    table = Table(
        title=f"SF{scale} upstream sources — {ready}/{len(rows)} suites ready",
        title_justify="left",
        show_lines=True,
    )
    table.add_column("suite", style="bold", no_wrap=True)
    if has_s3:
        table.add_column("S3 source (vs DB/manifest)", overflow="fold", ratio=1)
    if has_estuary:
        table.add_column("Estuary collections", overflow="fold", ratio=1)
    for r in rows:
        cells = [r.suite]
        if has_s3:
            cells.append(_cell(r.s3) if r.s3 is not None else "[dim]– skipped[/]")
        if has_estuary:
            cells.append(_cell(r.estuary) if r.estuary is not None else "[dim]– skipped[/]")
        table.add_row(*cells)
    return table


# ---------------------------------------------------------------------------
# Per-engine provisioning adapters + remediation
# ---------------------------------------------------------------------------

# Provisioning classes.
PREPROVISIONED = "preprovisioned"  # warehouses pre-created; nothing to write (snowflake)
TFVARS = "tfvars"  # sized by a terraform var the tool writes to the sizing JSON
OUT_OF_BAND = "out_of_band"  # resized via console/API; tfvar (if any) doesn't drive compute

# prepare-scale owns this file and rewrites it wholesale as JSON — Terraform
# auto-loads ``*.auto.tfvars.json`` (after, so overriding, the human-owned
# terraform.tfvars). JSON round-trips reliably; we never parse/edit HCL.
DEFAULT_SIZING_TFVARS_PATH = Path("terraform/sizing.auto.tfvars.json")


@dataclass(frozen=True)
class EngineAdapter:
    """How an engine reaches its compute target."""

    platform: str
    klass: str
    tfvar: str | None = None  # root terraform var name (TFVARS class with a wired var)


# Note: motherduck/clickhouse have tfvars (motherduck_tier, clickhouse_num_replicas)
# but those are declared expectations that don't drive real compute, so they're
# OUT_OF_BAND.
ADAPTERS: dict[str, EngineAdapter] = {
    "snowflake": EngineAdapter("snowflake", PREPROVISIONED),
    "bigquery": EngineAdapter("bigquery", TFVARS, "bigquery_reservation_slot_capacity"),
    "databricks": EngineAdapter("databricks", TFVARS, "databricks_warehouse_size"),
    "fabric": EngineAdapter("fabric", TFVARS, "fabric_capacity_sku"),
    "redshift": EngineAdapter("redshift", TFVARS, "redshift_baseline_rpu"),
    "clickhouse": EngineAdapter("clickhouse", OUT_OF_BAND),
    "motherduck": EngineAdapter("motherduck", OUT_OF_BAND),
}


@dataclass
class TfvarEdit:
    """A pending change to one sizing var in the JSON sizing file."""

    platform: str
    tfvar: str
    old: object | None  # current value in the sizing JSON (None if unset)
    new: object  # run-plan target (native int/str — JSON keeps the type)

    @property
    def is_noop(self) -> bool:
        return self.old == self.new


@dataclass
class RemediationPlan:
    """What to do to bring pending platforms to ready."""

    tfvar_edits: list[TfvarEdit] = field(default_factory=list)  # compute, TFVARS class
    manual_steps: list[str] = field(default_factory=list)  # compute, out-of-band / pre-provisioned
    data_steps: list[str] = field(default_factory=list)  # data-axis gaps


def load_sizing(path: Path = DEFAULT_SIZING_TFVARS_PATH) -> dict:
    """Load the tool-owned sizing JSON, or ``{}`` if it doesn't exist yet."""
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_sizing_edits(edits: list[TfvarEdit], path: Path = DEFAULT_SIZING_TFVARS_PATH) -> dict:
    """Apply edits to the sizing JSON and write it back (wholesale, sorted)."""
    path = Path(path)
    sizing = load_sizing(path)
    for e in edits:
        sizing[e.tfvar] = e.new
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sizing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.info("wrote %d sizing var(s) to %s", len(edits), path)
    return sizing


def _out_of_band_step(platform: str, target: object) -> str:
    if platform == "motherduck":
        return (
            f"motherduck — set the Duckling tier to '{target}' (MotherDuck settings / connection)."
        )
    if platform == "clickhouse":
        return (
            f"clickhouse — scale the service to {target} replicas "
            "(ClickHouse Cloud console → service → scaling)."
        )
    return f"{platform} — resize to {target} (out-of-band)."


def _suite_of(failure: str) -> str:
    """Suite token from a ``validate_engine_loads`` failure (``platform/suite …``)."""
    after = failure.split("/", 1)[1] if "/" in failure else failure
    return after.replace(":", " ").split()[0] if after.strip() else "?"


def _table_of(failure: str) -> str | None:
    """Table token from a failure (``platform/suite table: …``), or None for
    platform-level failures (``platform/suite: connect failed``)."""
    head = failure.split(":", 1)[0]
    after = head.split("/", 1)[1] if "/" in head else head
    parts = after.split()
    return parts[1] if len(parts) > 1 else None


_COUNTS_RE = re.compile(r"got ([\d,]+), expected ([\d,]+)")


def _parse_counts(failure: str) -> tuple[int, int] | None:
    """``(got, expected)`` parsed from a row-count failure, or None if not one."""
    m = _COUNTS_RE.search(failure)
    if not m:
        return None
    return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))


def _human_size(n: int) -> str:
    gib = n / (1024**3)
    return f"{gib:.1f} GiB" if gib >= 1 else f"{n / (1024**2):.0f} MiB"


def _local_source(
    suite: str, scale: int, data_root: Path = Path("data")
) -> tuple[bool, str | None]:
    """Whether the local artifact ``stage-data`` would upload for this suite
    exists, and its human-readable size. ClickBench is the single typed
    ``hits.parquet``; TPC-DS/DSB are the per-scale ``sf{scale}`` parquet dirs."""
    if suite == "clickbench":
        p = data_root / "clickbench" / "hits.parquet"
        return (True, _human_size(p.stat().st_size)) if p.exists() else (False, None)
    sf_dir = data_root / suite / f"sf{scale}"
    files = sorted(sf_dir.glob("*.parquet")) if sf_dir.is_dir() else []
    if not files:
        return False, None
    return True, _human_size(sum(f.stat().st_size for f in files))


def _staged_path(suite: str, table: str, scale: int) -> str:
    """The S3-relative key ``stage-data`` uploads for a (suite, table)."""
    if suite == "clickbench":
        return "data/clickbench/hits.parquet"
    return f"data/{suite}/sf{scale}/{table}.parquet"


def _local_path(suite: str, table: str, scale: int, data_root: Path = Path("data")) -> Path:
    """Local artifact path mirroring ``_staged_path`` (what stage-data uploads)."""
    return data_root / _staged_path(suite, table, scale).removeprefix("data/")


def default_staged_check(suite: str, table: str, scale: int):
    """Live S3 staged-object verdict for a (suite, table). Does I/O (reads); used
    as the ``staged_check`` callable so the remediation reflects what's actually
    staged instead of telling the operator to go look. Returns a ``StagedVerdict``
    (``.ok`` / ``.detail``)."""
    from .staging.s3 import staged_object_verdict

    return staged_object_verdict(
        _staged_path(suite, table, scale), _local_path(suite, table, scale)
    )


def _failing_keys(results: list[PlatformReadiness]) -> set[tuple[str, str]]:
    """All ``(suite, table)`` keys with a data failure across the assessment."""
    keys: set[tuple[str, str]] = set()
    for r in results:
        if r.data.status in (PENDING, UNKNOWN):
            for f in r.data.failures:
                keys.add((_suite_of(f), _table_of(f) or "?"))
    return keys


def _validate_cmd(suite: str, scale: int) -> str:
    """``validate-loads`` invocation. ClickBench is single-scale (one fixed dataset),
    so it takes no ``--scale``; TPC-DS/DSB are per-scale."""
    if suite == "clickbench":
        return "validate-loads --suites clickbench"
    return f"validate-loads --scale {scale} --suites {suite}"


def _load_chain(platform: str, suite: str, scale: int) -> str:
    """The full load chain for a missing suite (regenerate → stage → materialize)."""
    if suite == "clickbench":
        # Single-scale, fixed dataset: download the raw upstream artifact, derive the
        # typed parquet Estuary captures, then stage → materialize → validate. No --scale.
        return (
            f"download-clickbench → derive-clickbench-typed → stage-data → "
            f"flowctl apply ({platform} clickbench) → {_validate_cmd('clickbench', scale)}"
        )
    return (
        f"generate-{suite} --scales {scale} → stage-data → flowctl apply ({platform} {suite}) → "
        f"{_validate_cmd(suite, scale)}"
    )


def _materialize_chain(platform: str, suite: str, scale: int) -> str:
    """Downstream-only chain — used when the staged source is confirmed good, so
    regenerate/stage steps would be wasteful and misleading."""
    return f"flowctl apply ({platform} {suite}) → {_validate_cmd(suite, scale)}"


def _data_remediation(
    platform: str,
    scale: int,
    axis: AxisResult,
    staged: dict[tuple[str, str], object] | None = None,
) -> list[str]:
    """Tailored data-load suggestions, one per affected suite, by failure kind.

    ``staged`` maps ``(suite, table) -> StagedVerdict`` (from the S3 check). When
    a not-loaded suite's source is confirmed staged (``ok``), the remedy collapses
    to the downstream-only chain — no misleading regenerate/stage prefix."""
    staged = staged or {}
    if axis.status == UNKNOWN:
        # Manifest missing => local data was never generated at this scale (the
        # generate step writes the manifest too).
        return [
            f"{platform} — SF{scale} data not generated (no manifest): "
            f"generate-tpcds --scales {scale} / generate-dsb --scales {scale} → "
            f"stage-data → flowctl apply ({platform}) → validate-loads --scale {scale}"
        ]

    # PENDING: group failures by suite, classify the kind, suggest per suite.
    by_suite: dict[str, list[str]] = {}
    for f in axis.failures:
        by_suite.setdefault(_suite_of(f), []).append(f)

    steps: list[str] = []
    for suite, fails in by_suite.items():
        text = " ".join(fails).lower()
        validate = _validate_cmd(suite, scale)
        local_exists, local_size = _local_source(suite, scale)
        counts = _parse_counts(fails[0])

        if "not found" in text or "count_table_rows failed" in text:
            verdict = staged.get((suite, _table_of(fails[0]) or "?"))
            if verdict is not None and verdict.ok:
                # Source confirmed staged & good — don't tell them to regenerate;
                # the gap here is purely that this engine hasn't materialized it.
                steps.append(
                    f"{platform} / {suite} — not materialized here (source staged OK). "
                    f"Run: {_materialize_chain(platform, suite, scale)}"
                )
            else:
                # Table absent at the destination — distinguish "nothing to stage yet"
                # (regenerate first) from "staged but not materialized" (just load).
                src = (
                    f"local source present ({local_size}) — stage + materialize"
                    if local_exists
                    else "local source MISSING — derive/generate before staging"
                )
                steps.append(
                    f"{platform} / {suite} — not loaded; {src}. "
                    f"Run: {_load_chain(platform, suite, scale)}"
                )
        elif counts is not None:
            got, exp = counts
            if got == 0:
                steps.append(
                    f"{platform} / {suite} — 0 rows: the binding exists but delivered "
                    f"nothing. Confirm the {platform} {suite} materialization binding and "
                    f"trigger a backfill (Estuary dashboard) → {validate}"
                )
            elif got < exp:
                pct = 100.0 * got / exp
                steps.append(
                    f"{platform} / {suite} — partial load ({got:,}/{exp:,} = {pct:.1f}%): "
                    f"backfill incomplete or still running. Check the Estuary backfill; "
                    f"let it finish or re-trigger → {validate}"
                )
            else:  # got > exp
                steps.append(
                    f"{platform} / {suite} — over-count ({got:,}/{exp:,}): likely a "
                    f"duplicate load. Truncate the destination + re-materialize "
                    f"(flowctl apply {platform} {suite}) → {validate}"
                )
        else:
            steps.append(f"{platform} / {suite} — {_one_line(fails[0])}")
    return steps


def _cross_engine_notes(
    results: list[PlatformReadiness],
    scale: int,
    staged: dict[tuple[str, str], object] | None = None,
) -> list[str]:
    """When the same suite/table is short on multiple engines, the cause is
    almost always upstream (the shared staged object or the capture), not each
    engine's materialization. Surface that once, up front, before per-engine
    detail.

    ``staged`` maps ``(suite, table) -> StagedVerdict`` (from the S3 check); when
    present, its ``detail`` is appended — so the note reports what's actually
    staged instead of telling the operator to look. When empty, the note just
    names the path to check (keeps this function pure for tests)."""
    staged = staged or {}
    checked = [r for r in results if r.data.status != SKIP]
    n = len(checked)
    by_key: dict[tuple[str, str], set[str]] = {}
    for r in checked:
        if r.data.status not in (PENDING, UNKNOWN):
            continue
        for f in r.data.failures:
            by_key.setdefault((_suite_of(f), _table_of(f) or "?"), set()).add(r.platform)

    notes: list[str] = []
    for (suite, table), plats in sorted(by_key.items()):
        if len(plats) < 2:
            continue
        note = (
            f"{suite}/{table}: short on {len(plats)}/{n} engines "
            f"({', '.join(sorted(plats))}) — likely a shared upstream cause "
            f"(the staged S3 object or the capture), not per-engine materialization."
        )
        verdict = staged.get((suite, table))
        if verdict is not None:
            note += f" Staged object: {verdict.detail}"
        else:
            note += f" Check {_staged_path(suite, table, scale)} before re-loading each engine."
        notes.append(note)
    return notes


def remediation_plan(
    results: list[PlatformReadiness],
    scale: int,
    sizing: dict,
    staged_check: object | None = None,
) -> RemediationPlan:
    """Build the remediation plan from a readiness assessment.

    Pure (no I/O) when ``staged_check`` is None. ``sizing`` is the current
    sizing-JSON dict (for computing old→new diffs). ``staged_check`` is an
    optional ``(suite, table, scale) -> StagedVerdict`` callable that inspects
    the staged S3 object (it does S3 reads); its verdicts feed both the
    cross-engine note and the per-engine remedy (so a confirmed-good source
    collapses "not loaded" steps to downstream-only).
    """
    plan = RemediationPlan()
    rp = get_run_plan()
    # Run the staged-object check once per failing (suite, table) — shared by the
    # cross-engine note and per-engine remedies, so the S3 read isn't repeated.
    staged: dict[tuple[str, str], object] = {}
    if staged_check is not None:
        staged = {(s, t): staged_check(s, t, scale) for (s, t) in _failing_keys(results)}
    # Lead the data steps with cross-engine correlation: a suite/table short on
    # several engines points at the shared source, not each materialization.
    plan.data_steps.extend(_cross_engine_notes(results, scale, staged))
    for r in results:
        if r.compute.status == PENDING:
            target = rp.target(r.platform, scale)
            adapter = ADAPTERS.get(r.platform)
            if adapter is None:
                plan.manual_steps.append(f"{r.platform} — no adapter; resize to {target} manually.")
            elif adapter.klass == TFVARS and adapter.tfvar:
                if r.platform == "bigquery":
                    # Only force a reservation where on-demand is denied (high scales).
                    # Where on-demand is allowed, leave BQ on-demand — write no tfvars.
                    # A slot count alone won't take effect, so also enable the reservation.
                    if rp.on_demand_allowed("bigquery", scale) is False:
                        plan.tfvar_edits.append(
                            TfvarEdit("bigquery", adapter.tfvar, sizing.get(adapter.tfvar), target)
                        )
                        plan.tfvar_edits.append(
                            TfvarEdit(
                                "bigquery",
                                "bigquery_reservation_enabled",
                                sizing.get("bigquery_reservation_enabled"),
                                True,
                            )
                        )
                else:
                    plan.tfvar_edits.append(
                        TfvarEdit(r.platform, adapter.tfvar, sizing.get(adapter.tfvar), target)
                    )
                    if r.platform == "fabric":
                        plan.manual_steps.append(
                            f"fabric — after apply, resize/resume the capacity to {target} (Fabric portal)."
                        )
            elif adapter.klass == OUT_OF_BAND:
                plan.manual_steps.append(_out_of_band_step(r.platform, target))
            elif adapter.klass == PREPROVISIONED:
                plan.manual_steps.append(
                    f"snowflake — expected BENCHMARK_WH_{target}; live did not match. "
                    f"Verify the warehouse exists and is sized {target} (terraform)."
                )
            else:  # TFVARS class but no root tfvar wired (no engine is; defensive)
                plan.manual_steps.append(
                    f"{r.platform} — set compute to {target}: no root tfvar wired; needs a terraform change."
                )
        if r.data.status in (PENDING, UNKNOWN):
            plan.data_steps.extend(_data_remediation(r.platform, scale, r.data, staged))
    return plan

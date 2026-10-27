"""Row-count validation: engine-loaded counts vs the local manifest.

For each enabled suite (tpcds / dsb / clickbench), ``validate-loads`` reads
the per-suite expected-counts manifest, queries each engine's tables in the
matching schema, and reports discrepancies. Iterates per-suite so each block
hits its own schema (TPC-DS -> tpcds_sf1, DSB -> dsb_sf1, ClickBench ->
clickbench) — matching the convention used by the `sql` and `validate-sf1`
CLI commands.

ClickBench is single-scale and has just one ``hits`` table; its expected row
count is hardcoded (99,997,497) since there's no scale-factor manifest.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from ..connectors.registry import create_connector
from ..datagen.tpcds import TPCDS_TABLES
from ..exceptions import ConnectorError
from ..logging_config import bound_platform

logger = logging.getLogger("benchmark.staging.load_check")

CLICKBENCH_HITS_ROWS = 99_997_497  # canonical ClickBench corpus size


class TableProgress(BaseModel):
    """One table's load state for a poll — the structured channel a live display
    (e.g. the CLI's rich progress bars) renders. ``got is None`` means the table
    isn't readable yet (not created / errored), with ``reason`` set."""

    key: str  # "snowflake/clickbench hits" (or "platform/suite" for suite-level errors)
    expected: int  # target row count (0 when unknown)
    got: int | None  # current row count; None if the table can't be counted yet
    done: bool
    reason: str | None = None  # short cause when not done and got is None


class TableTick(BaseModel):
    """One table's count emitted as the check walks a platform's datasets — the
    per-table channel a live display animates. ``suite_index``/``suite_count``
    locate the current dataset within the platform and ``platform_table_total``
    is the platform's table count across all datasets, so a single per-platform
    progress bar can track every dataset on one line. ``got``/``expected`` are the
    row counts; ``got is None`` means the table isn't readable yet, ``reason`` set."""

    key: str  # "motherduck/tpcds call_center"
    platform: str
    suite: str
    table: str
    suite_index: int  # 1-based dataset position within the platform
    suite_count: int  # datasets (suites) for the platform
    table_index: int  # 1-based table position within the suite
    table_count: int  # tables in the suite
    platform_table_total: int  # total tables across all the platform's suites
    got: int | None  # current row count; None if not readable yet
    expected: int  # target row count (0 when unknown)
    done: bool
    reason: str | None = None


def _load_manifest(path: Path, scale_factor: int) -> dict[str, int]:
    if not path.exists():
        raise FileNotFoundError(f"expected_counts manifest missing: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    counts = manifest.get(str(scale_factor))
    if counts is None:
        raise KeyError(f"no entry for SF{scale_factor} in {path}")
    return counts


def _suite_plan(
    suite: str, scale_factor: int, data_root: Path
) -> tuple[str, dict[str, int]] | None:
    """Resolve (schema, expected_counts_map) for a suite, or None to skip."""
    if suite == "tpcds":
        counts = _load_manifest(data_root / "tpcds" / "expected_counts.json", scale_factor)
        return f"tpcds_sf{scale_factor}", counts
    if suite == "dsb":
        counts = _load_manifest(data_root / "dsb" / "expected_counts.json", scale_factor)
        return f"dsb_sf{scale_factor}", counts
    if suite == "clickbench":
        return "clickbench", {"hits": CLICKBENCH_HITS_ROWS}
    return None


def _check_engine_loads_once(
    platforms: list[str],
    scale_factor: int,
    suites: tuple[str, ...],
    data_root: Path,
    *,
    log_detail: bool,
    on_table: Callable[[TableTick], None] | None = None,
) -> tuple[list[str], list[TableProgress]]:
    """One pass of the row-count check.

    Returns ``(failures, records)``: ``failures`` is the human-readable failure
    strings (empty == all good); ``records`` is a
    :class:`TableProgress` per table checked (success included), the structured
    channel a live display renders. ``log_detail`` gates the per-table INFO lines
    so polling rounds don't spam the console; the final round logs in full.
    ``on_table``, when given, fires once per table as it is counted (carrying its
    walk position within the suite) — the finer-grained hook a live display uses.
    """
    failures: list[str] = []
    records: list[TableProgress] = []

    def _short(exc: object) -> str:
        return str(exc).splitlines()[0][:80]

    def _tables_for(suite: str, expected: dict[str, int]) -> list[str]:
        return list(expected) if suite == "clickbench" else list(TPCDS_TABLES)

    # Pre-resolve suite plans once (scale-keyed, platform-independent) so each tick
    # carries stable counters: which dataset of how many, and the platform's table
    # total across all datasets — the totals a per-platform progress bar tracks.
    suite_plans = [(suite, _suite_plan(suite, scale_factor, data_root)) for suite in suites]
    valid_plans = [(suite, plan) for suite, plan in suite_plans if plan is not None]
    suite_count = len(valid_plans)
    platform_table_total = sum(len(_tables_for(suite, plan[1])) for suite, plan in valid_plans)

    def _tick(
        key: str,
        platform: str,
        suite: str,
        table: str,
        suite_index: int,
        table_index: int,
        table_count: int,
        got: int | None,
        expected: int | None,
        done: bool,
        reason: str | None = None,
    ) -> None:
        if on_table is not None:
            on_table(
                TableTick(
                    key=key,
                    platform=platform,
                    suite=suite,
                    table=table,
                    suite_index=suite_index,
                    suite_count=suite_count,
                    table_index=table_index,
                    table_count=table_count,
                    platform_table_total=platform_table_total,
                    got=got,
                    expected=expected or 0,
                    done=done,
                    reason=reason,
                )
            )

    for platform in platforms:
        # Stamp every record emitted during this platform's checks (connector
        # connect/query errors included) so the audit log is queryable by platform.
        with bound_platform(platform):
            suite_index = 0
            for suite, plan in suite_plans:
                if plan is None:
                    failures.append(f"{platform}/{suite}: unsupported suite")
                    records.append(
                        TableProgress(
                            key=f"{platform}/{suite}",
                            expected=0,
                            got=None,
                            done=False,
                            reason="unsupported",
                        )
                    )
                    continue
                suite_index += 1
                schema, expected = plan

                try:
                    connector = create_connector(platform, config={"schema": schema})
                except (KeyError, ValueError) as exc:
                    failures.append(f"{platform}/{suite}: connector not registered — {exc}")
                    records.append(
                        TableProgress(
                            key=f"{platform}/{suite}",
                            expected=0,
                            got=None,
                            done=False,
                            reason="not registered",
                        )
                    )
                    continue

                try:
                    connector.connect()
                except ConnectorError as exc:
                    failures.append(f"{platform}/{suite}: connect failed — {exc}")
                    records.append(
                        TableProgress(
                            key=f"{platform}/{suite}",
                            expected=0,
                            got=None,
                            done=False,
                            reason="connect failed",
                        )
                    )
                    continue

                tables = _tables_for(suite, expected)
                table_count = len(tables)
                try:
                    for table_index, table in enumerate(tables, start=1):
                        key = f"{platform}/{suite} {table}"
                        exp = expected.get(table)
                        try:
                            n = connector.count_table_rows(schema, table)
                        except (ConnectorError, NotImplementedError) as exc:
                            # Table not created yet (or unreadable) — a failure this
                            # pass; the wait loop retries until it appears or times out.
                            failures.append(f"{key}: {exc}")
                            records.append(
                                TableProgress(
                                    key=key,
                                    expected=exp or 0,
                                    got=None,
                                    done=False,
                                    reason=_short(exc),
                                )
                            )
                            msg = f"{key}: {_short(exc)}"
                            row_count: int | None = None
                            _tick(
                                key,
                                platform,
                                suite,
                                table,
                                suite_index,
                                table_index,
                                table_count,
                                None,
                                exp,
                                False,
                                _short(exc),
                            )
                        else:
                            row_count = n
                            if exp is None:
                                msg = f"{key}: missing from manifest"
                                failures.append(msg)
                                records.append(
                                    TableProgress(
                                        key=key,
                                        expected=0,
                                        got=n,
                                        done=False,
                                        reason="missing from manifest",
                                    )
                                )
                                done = False
                            elif n != exp:
                                msg = _mismatch_message(key, n, exp)
                                failures.append(msg)
                                records.append(
                                    TableProgress(key=key, expected=exp, got=n, done=False)
                                )
                                done = False
                            else:
                                msg = f"{key}: {n:,}/{exp:,} rows ok"
                                records.append(
                                    TableProgress(key=key, expected=exp, got=n, done=True)
                                )
                                done = True
                            _tick(
                                key,
                                platform,
                                suite,
                                table,
                                suite_index,
                                table_index,
                                table_count,
                                n,
                                0 if exp is None else exp,
                                done,
                                "missing from manifest" if exp is None else None,
                            )
                        if log_detail:
                            # Structured fields so the JSON audit log is queryable
                            # by block / table; platform is stamped by the active
                            # bound_platform context (the message embeds them too).
                            logger.info(
                                msg,
                                extra={
                                    "block": suite,
                                    "table": table,
                                    "scale_factor": scale_factor,
                                    "row_count": row_count,
                                },
                            )
                finally:
                    connector.disconnect()
    return failures, records


def _mismatch_message(key: str, got: int, expected: int) -> str:
    """The failure line for a table whose row count differs from the manifest's.

    ``_COUNTS_RE`` reads the counts back out of it for the plain-log progress
    report, so the two change together.
    """
    return f"{key}: {got:,}/{expected:,} rows ({100.0 * got / expected:.0f}%, delta {got - expected:,})"


_COUNTS_RE = re.compile(r": ([\d,]+)/([\d,]+) rows")


def _key_and_counts(failure: str) -> tuple[str, int | None, int | None]:
    """``(table_key, got, expected)`` from a failure string. got/expected are
    None when the line isn't a row-count mismatch (e.g. table missing)."""
    key = failure.split(":", 1)[0].strip()
    m = _COUNTS_RE.search(failure)
    if m:
        return key, int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    return key, None, None


def _progress_report(failures: list[str], prev: dict[str, int]) -> tuple[str, dict[str, int]]:
    """Per-table progress lines (got/expected, %, delta since last poll) plus the
    current per-table counts for the next poll's delta. Tables still missing show
    a short reason instead of a count."""
    lines: list[str] = []
    now: dict[str, int] = {}
    for f in failures:
        key, got, exp = _key_and_counts(f)
        if got is not None and exp:
            now[key] = got
            pct = 100.0 * got / exp
            if key in prev:
                delta = got - prev[key]
                move = f", +{delta:,}" if delta > 0 else ", +0 ⚠ stalled"
            else:
                move = ""
            lines.append(f"  {key}: {got:,}/{exp:,} ({pct:.0f}%{move})")
        else:
            reason = (f.split(":", 1)[1].strip() if ":" in f else f).splitlines()[0][:80]
            lines.append(f"  {key}: {reason}")
    return "\n".join(lines), now


def validate_engine_loads(
    platforms: list[str],
    scale_factor: int,
    suites: tuple[str, ...] = ("tpcds", "dsb", "clickbench"),
    data_root: Path = Path("data"),
    *,
    wait_seconds: int = 0,
    poll_interval_seconds: int = 15,
    on_poll: Callable[[list[TableProgress], int], None] | None = None,
    on_table: Callable[[TableTick], None] | None = None,
) -> list[str]:
    """Return a list of human-readable failure strings (empty == all good).

    With ``wait_seconds <= 0`` (the default) this is a single fail-fast pass,
    right for an interactive correctness gate.

    With ``wait_seconds > 0`` it polls: Estuary materializations create their
    target tables and backfill asynchronously after a publish, so a check run
    immediately afterwards races the load (tables missing / row counts still
    ramping). The e2e driver passes a wait so the ``materialize -> validate-loads``
    handoff blocks on the backfill instead of failing on the gap. Each round
    re-checks every table; it returns as soon as all counts match, or returns
    the last round's outstanding failures once the deadline passes.

    ``on_poll(records, seconds_left)`` is an optional callback invoked once per
    round with the structured :class:`TableProgress` records — the hook the CLI
    uses to drive a live progress display. When it's provided, the per-poll text
    summary is suppressed (the display owns the screen); when it's None, the text
    progress report is logged instead (right for non-TTY / log-file runs).

    ``on_table(tick)`` is an optional finer-grained callback fired once per table
    as it is counted — the hook the CLI uses to animate a per-platform progress
    bar. When it is active, every table's row count IS logged each round (the live
    display's scrolling log window renders those lines, bounded by a ring buffer)
    and the coarse per-poll summary is skipped to avoid double reporting.
    """
    if wait_seconds <= 0:
        failures, _ = _check_engine_loads_once(
            platforms, scale_factor, suites, data_root, log_detail=True, on_table=on_table
        )
        return failures

    deadline = time.monotonic() + wait_seconds
    attempt = 0
    prev_counts: dict[str, int] = {}
    while True:
        attempt += 1
        remaining = deadline - time.monotonic()
        last_round = remaining <= 0
        # With a live per-table display (on_table) we log every table each round
        # so the row counts stream into its scrolling log window; otherwise log
        # per-table detail only on the final round so intermediate polls stay quiet.
        failures, records = _check_engine_loads_once(
            platforms,
            scale_factor,
            suites,
            data_root,
            log_detail=on_table is not None or (last_round and on_poll is None),
            on_table=on_table,
        )
        if on_poll is not None:
            on_poll(records, max(0, int(remaining)))
        if not failures:
            logger.info("validate-loads: all tables caught up after %d poll(s)", attempt)
            return failures
        if last_round:
            logger.warning(
                "validate-loads: wait of %ds exhausted; %d table(s) still outstanding",
                wait_seconds,
                len(failures),
            )
            return failures
        sleep_for = min(poll_interval_seconds, max(1, int(remaining)))
        if on_poll is None and on_table is None:
            report, prev_counts = _progress_report(failures, prev_counts)
            logger.info(
                "validate-loads: %d table(s) loading (poll %d, %ds left, retry %ds):\n%s",
                len(failures),
                attempt,
                int(remaining),
                sleep_for,
                report,
            )
        time.sleep(sleep_for)

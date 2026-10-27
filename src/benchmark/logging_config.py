"""Logging configuration: Rich console + JSON file audit trail.

Follows CLAUDE.md §Code Standards (no print(); every module gets its own
`logging.getLogger("benchmark.<area>")`; structured fields go in `extra={}`).
"""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_active_platform: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "benchmark_active_platform", default=None
)


class PlatformContextFilter(logging.Filter):
    """Stamp the active platform (from the surrounding ``bound_platform`` context)
    onto any record that doesn't already carry one, so every line emitted while
    working a platform is queryable by it — not just the calls that remembered
    ``extra={"platform": ...}``. Records that set their own platform win."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "platform"):
            platform = _active_platform.get()
            if platform is not None:
                record.platform = platform
        return True


@contextmanager
def bound_platform(platform: str) -> Iterator[None]:
    """Within the block, stamp ``platform`` onto every emitted log record unless
    the emitting call set its own. Wrap per-platform work with this so connector
    errors, sweep lines, and summaries are all queryable by platform in the audit
    log, without each call site having to remember ``extra={"platform": ...}``."""
    token = _active_platform.set(platform)
    try:
        yield
    finally:
        _active_platform.reset(token)


class JSONFormatter(logging.Formatter):
    """Structured JSON formatter for the audit log file."""

    KNOWN_EXTRA_FIELDS = (
        "platform",
        "query_id",
        "scale_factor",
        "run_type",
        "run_number",
        "experiment",
        "phase",
        "table",
        "row_count",
        "wall_clock_ms",
        "server_time_ms",
        "cost_usd",
        "block",
        "log_file",
        "total_queries",
        "already_completed_pairs",
        "passed",
        "total",
        "success_count",
        "rate_pct",
    )

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        for key in self.KNOWN_EXTRA_FIELDS:
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)
        return json.dumps(log_entry, default=str)


#: Third-party loggers copied into the JSON audit log at INFO. The Databricks SQL
#: connector retries a throttled request (HTTP 429/503) for up to 900 s with
#: backoff of up to 60 s per attempt, and logs each throttled response only here
#: ("Received status code 429 for POST request"). Without it, a query that spent
#: minutes retrying looks, in our rows, like one that ran for minutes.
AUDITED_LIBRARY_LOGGERS = ("databricks.sql.auth.retry",)


def configure_logging(
    level: str = "INFO",
    log_dir: Path | None = None,
    run_label: str | None = None,
) -> Path:
    """Configure root logger for the benchmark harness.

    Returns the path to the JSON audit log so callers can reference it.
    """
    log_dir = log_dir or Path("results/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    label = run_label or datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = log_dir / f"benchmark_{label}.jsonl"

    root = logging.getLogger("benchmark")
    root.setLevel(getattr(logging, level.upper()))
    root.handlers.clear()
    root.propagate = False

    # Stamp the active platform (set via bound_platform) onto every record at the
    # handler boundary, so the audit log is queryable by platform regardless of
    # which call site emitted the line.
    platform_filter = PlatformContextFilter()

    console = RichHandler(
        console=Console(stderr=True),
        rich_tracebacks=True,
        show_time=True,
        show_path=False,
        markup=False,
    )
    console.setLevel(logging.INFO)
    console.addFilter(platform_filter)
    root.addHandler(console)

    file_handler = logging.FileHandler(json_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JSONFormatter())
    file_handler.addFilter(platform_filter)
    root.addHandler(file_handler)

    # Library loggers whose records belong in the audit trail but not on screen:
    # they share the file handler only, so they never reach the console.
    # A handler from an earlier call (a re-configure) is swapped, not stacked.
    for name in AUDITED_LIBRARY_LOGGERS:
        lib = logging.getLogger(name)
        lib.setLevel(logging.INFO)
        for old_handler in [h for h in lib.handlers if isinstance(h, logging.FileHandler)]:
            lib.removeHandler(old_handler)
        lib.addHandler(file_handler)

    root.info("Logging initialized", extra={"log_file": str(json_path)})
    return json_path

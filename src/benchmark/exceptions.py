"""Project-specific exception hierarchy.

CLAUDE.md §Code Standards mandates a single root `BenchmarkError`; all harness
code should raise these rather than bare exceptions so callers can choose
which class of failure to retry, log, or re-raise.
"""


class BenchmarkError(Exception):
    """Base exception for all benchmark errors."""


class ConnectorError(BenchmarkError):
    """Failed to connect to or communicate with an engine."""


class QueryTimeoutError(BenchmarkError):
    """Query exceeded the configured timeout."""


class QueryExecutionError(BenchmarkError):
    """Query failed during execution (syntax error, OOM, etc.)."""


class ValidationError(BenchmarkError):
    """Result validation failed (row count mismatch, hash mismatch)."""


class NotSupportedError(BenchmarkError):
    """Engine does not support the requested capability."""


class DataGenerationError(BenchmarkError):
    """Benchmark input data could not be generated (e.g. the DSB skew generator
    is unavailable and could not be built — we fail rather than silently
    producing non-skewed data)."""


class UnlabelledComputeError(BenchmarkError):
    """The runtime environment yielded no compute-size label.

    Raised rather than recording rows with ``compute_label = NULL``. An unlabelled
    row is INVISIBLE to the size-keyed analysis — the canonical views join on the
    label — so continuing produces a cell that looks absent rather than one that
    looks wrong. That is silent data loss, and it is worse than an aborted cell:
    a fractured cell reports success while part of a pass is silently missing.

    Deliberately NOT covered by the continue-on-failure rule, which exists so one
    failed QUERY cannot abort a run. This is a property of the whole cell.
    """

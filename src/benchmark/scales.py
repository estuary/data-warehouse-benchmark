"""Canonical benchmark scale factors.

The benchmark runs at a fixed set of data scales (SF1 … SF1000). This module is
the single source of truth for them, so the run-plan and CLI ``--scale`` options
validate against one set and a typo like ``--scale 7`` is rejected early.

``Scale`` is the type to annotate scale-typed fields with; ``VALID_SCALES`` is the
runtime set; ``validate_scale`` is the guard (usable as a Typer callback).
"""

from __future__ import annotations

from typing import Literal

Scale = Literal[1, 10, 100, 1000]

VALID_SCALES: tuple[int, ...] = (1, 10, 100, 1000)


def validate_scale(value: int) -> int:
    """Return ``value`` if it is a canonical scale, else raise ``ValueError``.

    Suitable as a Typer ``--scale`` callback and as the run-plan key guard.
    """
    if value not in VALID_SCALES:
        raise ValueError(f"scale must be one of {list(VALID_SCALES)}, got {value}")
    return value

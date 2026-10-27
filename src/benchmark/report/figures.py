"""Number-drift gate: recompute every published table from the shipped database.

Prose review verifies arithmetic; it cannot verify slice selection. The serious
errors in a report like this come from selecting the wrong rows — the wrong
compute tier, the wrong dedup key, the wrong query set — and each one is
individually plausible on the page. Recomputation catches that class.

So each hand-authored table in ``report/sections/*.md`` carries a marker::

    <!-- verify: snowflake_sf1000_ladder -->
    | Tier | Suite time | ... |

and a verifier of the same name here recomputes it from the results DB. `benchmark
verify-figures` compares them cell by cell and fails on drift. What the gate buys is
narrow and specific: a published number cannot disagree with the database it
claims to come from, and the *methodology* each table asserts is executable rather
than described.

The class of defect this catches: a scaling table that claims "each engine at
its designated tier" while a cell quietly comes from another tier; a
cost-per-speedup column computed against superseded cost data; prose quoting a
figure the table beside it has already corrected.

Adding a verifier is the point of work, not the marker. A verifier must encode the
methodology the table *claims*, so that a table computed a different way fails.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

logger = logging.getLogger("benchmark.report.figures")

DEFAULT_SECTIONS_DIR = Path("report/sections")

_MARKER_RE = re.compile(r"<!--\s*verify:\s*([a-z0-9_]+)\s*-->", re.I)
#: First number in a cell, tolerating $ 1,234.5 s / 6.33x / **bold** / — .
_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")
#: Typographic characters that authored prose uses for arithmetic ones. A published
#: "−0.07" (U+2212 MINUS SIGN) must compare equal to a computed "-0.07"; without this
#: the gate reports drift on every negative number and the noise hides real findings.
_NUMERIC_ALIASES = {"\u2212": "-", "\u2013": "-", "\u00d7": "x", "\u2014": "—"}

#: Relative tolerance on numeric comparison. Published tables round to 1-2 decimals,
#: so an exact match is the wrong bar; 1% is tight enough to catch a wrong slice
#: (every real defect found was off by 5% or much more) while ignoring rounding.
DEFAULT_TOLERANCE = 0.01


@dataclass(frozen=True)
class Table:
    """A marked markdown table parsed out of an authored section."""

    name: str
    path: Path
    line: int
    header: list[str]
    rows: list[list[str]]


@dataclass(frozen=True)
class Drift:
    """One cell where the published value disagrees with the database.

    ``kind`` separates two findings that need different responses. A ``value`` drift
    is a defect: a number on the page contradicts the database it claims to come
    from, and it must be corrected before publication. A ``coverage`` finding is an
    editorial decision: the data holds rows the table does not show, or shows a row
    the data no longer has. Those are worth surfacing — a curated table should say
    it is curated — but they are not wrong numbers, so they must not be mixed into
    the same count.
    """

    kind: str
    table: str
    path: Path
    line: int
    row_label: str
    column: str
    published: str
    recomputed: str

    def describe(self) -> str:
        return (
            f"{self.path.name}:{self.line} [{self.table}] "
            f"{self.row_label} / {self.column}: "
            f"published {self.published!r}, recomputed {self.recomputed!r}"
        )


class FigureError(RuntimeError):
    """A marked table has no verifier, or a verifier cannot run."""


def _cells(line: str) -> list[str]:
    """Split one markdown table row into stripped cells."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_marked_tables(path: Path) -> list[Table]:
    """Every ``<!-- verify: name -->`` table in one section file.

    The marker must sit immediately above the table's header row (blank lines
    allowed between). A marker with no table following it is an error rather than a
    silent skip — it means someone moved a table and left the promise behind.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[Table] = []
    for idx, line in enumerate(lines):
        match = _MARKER_RE.search(line)
        if not match:
            continue
        name = match.group(1).lower()
        cursor = idx + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor >= len(lines) or not lines[cursor].lstrip().startswith("|"):
            raise FigureError(
                f"{path}:{idx + 1}: marker 'verify: {name}' is not followed by a table"
            )
        header = _cells(lines[cursor])
        cursor += 1
        if cursor < len(lines) and set(lines[cursor].replace("|", "").strip()) <= {"-", ":", " "}:
            cursor += 1  # the |---|---| separator
        rows: list[list[str]] = []
        while cursor < len(lines) and lines[cursor].lstrip().startswith("|"):
            rows.append(_cells(lines[cursor]))
            cursor += 1
        out.append(Table(name=name, path=path, line=idx + 1, header=header, rows=rows))
    return out


def load_tables(sections_dir: Path = DEFAULT_SECTIONS_DIR) -> list[Table]:
    """All marked tables across the authored sections, in filename order."""
    if not sections_dir.is_dir():
        return []
    tables: list[Table] = []
    for path in sorted(sections_dir.glob("*.md")):
        tables.extend(parse_marked_tables(path))
    return tables


def _number(cell: str) -> float | None:
    """The first number in a cell, or None when it carries no number.

    ``—`` (not measured) and ``*`` (withheld) are legitimately non-numeric and
    compare as strings — conflating "not measured" with 0 is one of the errors this
    gate exists to prevent, so they must never coerce to a number.
    """
    text = cell.strip()
    for alias, plain in _NUMERIC_ALIASES.items():
        text = text.replace(alias, plain)
    if text in {"—", "-", "*", "", "n/a"}:
        return None
    match = _NUMBER_RE.search(text.replace(",", ""))
    return float(match.group()) if match else None


def _censored(cell: str) -> bool:
    """Whether a cell carries the trailing ``+`` censoring marker.

    ``50+`` means "served 50 at the highest level we asked of it" — a lower bound,
    not a limit. That distinction is load-bearing: the published footnote says
    comparing a censored 50 against an uncensored 100 reads as half the capacity
    when it may only mean we never tried 100 there.
    """
    return cell.strip().endswith("+")


def _cell_matches(published: str, recomputed: str, tolerance: float) -> bool:
    """Compare one cell numerically when both sides carry a number, else exactly.

    The censoring marker is compared BEFORE the number, because ``_number`` reads
    ``50+`` as 50.0 and would otherwise call it equal to an uncensored ``50`` —
    letting the gate pass on a stale ``+`` after a top-up un-censors a cell, a
    published lower bound the data no longer supports. A number can be right
    while the claim about it is wrong, so both are checked.
    """
    if _censored(published) != _censored(recomputed):
        return False
    pub_n, rec_n = _number(published), _number(recomputed)
    if pub_n is None or rec_n is None:
        return published.strip() == recomputed.strip()
    if rec_n == 0:
        return abs(pub_n) <= tolerance
    return abs(pub_n - rec_n) / abs(rec_n) <= tolerance


#: name -> (verifier, key_columns). A verifier returns the table body it expects: one
#: list of cells per row. The leading ``key_columns`` cells identify the row (Engine,
#: or Engine + Tier) and are matched, not compared; the rest are compared. Header text
#: is prose and is never compared.
Verifier = Callable[["duckdb.DuckDBPyConnection"], list[list[str]]]
VERIFIERS: dict[str, tuple[Verifier, int]] = {}


def verifier(name: str, *, key_columns: int = 1) -> Callable[[Verifier], Verifier]:
    """Register a verifier under the marker name it checks.

    Args:
        name: the ``<!-- verify: name -->`` marker this checks.
        key_columns: how many leading cells identify a row. Tables keyed on one
            dimension (Tier) use 1; a table keyed on Engine *and* Tier needs 2, or
            every Redshift row would collide on the label "Redshift" and only the
            last would be checked.
    """

    def register(fn: Verifier) -> Verifier:
        VERIFIERS[name] = (fn, key_columns)
        return fn

    return register


def verify(
    con: duckdb.DuckDBPyConnection,
    sections_dir: Path = DEFAULT_SECTIONS_DIR,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> list[Drift]:
    """Recompute every marked table and return the cells that disagree.

    Raises:
        FigureError: a marked table has no registered verifier. Silently skipping
            it would let a table opt out of the gate by deleting one line of code.
    """
    from .views import install_views

    # Verifiers read the shared deduplicated views; `verify-figures` arrives on
    # a fresh connection, so install them once up front.
    install_views(con)
    drifts: list[Drift] = []
    for table in load_tables(sections_dir):
        entry = VERIFIERS.get(table.name)
        if entry is None:
            raise FigureError(
                f"{table.path}:{table.line}: no verifier registered for "
                f"'{table.name}'. Known: {', '.join(sorted(VERIFIERS)) or '(none)'}"
            )
        fn, key_columns = entry

        def _key(row: list[str], n: int = key_columns) -> str:
            return " · ".join(c.strip().strip("*") for c in row[:n])

        expected = fn(con)
        by_label = {_key(row): row for row in expected}
        for published_row in table.rows:
            label = _key(published_row)
            recomputed_row = by_label.get(label)
            if recomputed_row is None:
                drifts.append(
                    Drift(
                        kind="coverage",
                        table=table.name,
                        path=table.path,
                        line=table.line,
                        row_label=label,
                        column="(row)",
                        published="present",
                        recomputed="no such row in the data",
                    )
                )
                continue
            for col_idx in range(key_columns, min(len(published_row), len(recomputed_row))):
                pub = published_row[col_idx]
                rec = recomputed_row[col_idx]
                if not _cell_matches(pub, rec, tolerance):
                    column = (
                        table.header[col_idx] if col_idx < len(table.header) else f"col{col_idx}"
                    )
                    drifts.append(
                        Drift(
                            kind="value",
                            table=table.name,
                            path=table.path,
                            line=table.line,
                            row_label=label,
                            column=column,
                            published=pub,
                            recomputed=rec,
                        )
                    )
        missing = set(by_label) - {_key(r) for r in table.rows}
        for label in sorted(missing):
            drifts.append(
                Drift(
                    kind="coverage",
                    table=table.name,
                    path=table.path,
                    line=table.line,
                    row_label=label,
                    column="(row)",
                    published="absent",
                    recomputed="has data",
                )
            )
    for drift in drifts:
        # Value drift is a defect; coverage is a reporting decision. Logging them at
        # the same level would train the reader to skim past both.
        if drift.kind == "value":
            logger.warning("figure drift: %s", drift.describe())
        else:
            logger.info("figure coverage: %s", drift.describe())
    return drifts

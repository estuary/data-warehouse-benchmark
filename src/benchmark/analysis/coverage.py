"""Coverage audit: each engine's full ladder at every scale, minus what was measured.

Coverage gaps tend to be found *by accident* — a missing ladder while estimating
runtimes, a censored ceiling while chasing a figure, unreconciled cells when a
sizing table comes back empty. Each is visible in the data the whole time while
nothing is asking.

This module asks. It derives expected coverage from ``run_plan.yaml``: the designated
size per scale, and each engine's size ladder, single-user (``sweep``) and under load
(``load_sweep``, else the same ladder), at every scale the plan covers and for every
suite. It subtracts what is recorded as deliberately skipped and reports the
difference in seven kinds:

* **designated-missing** — the plan names a size for a scale and a suite has no warm
  rows there. The strongest signal: a cell the plan asserts and the data denies.
* **sweep-hole** — a single-user ladder size with no rows for a suite at a scale.
* **load-hole** — the same for the concurrency grid.
* **censored** — a concurrency cell whose useful-concurrency equals the highest level
  ever attempted. It passed at our ceiling, so the number is a lower bound, and
  publishing it beside an uncensored cell reads as a smaller capacity rather than a
  shorter ladder.
* **level-gap** — a concurrency ladder missing protocol levels. Levels above the last
  one run are not a gap when that level had timeouts: a sweep ejects a band there by
  rule.
* **short-level** — a protocol level that recorded fewer queries than its suite has
  (a pass cut short), so its latency and failure rate describe a subset.
* **short-pass** — the same for a single-user pass: queries never attempted, which a
  thin-coverage note in a table cannot tell apart from queries that failed.

None of these is automatically a defect: a short ladder can be a deliberate stop. But
it must be a *decided* stop, recorded in ``scale_skips``, rather than one nobody
noticed. A skip matches on every key it carries (scale, suite, compute size, run
type, each '*' for all), so a DSB-only skip hides no TPC-DS gap and a single-user
skip hides no load gap.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    import duckdb

logger = logging.getLogger("benchmark.analysis.coverage")

#: The protocol's concurrency ladder. A cell missing a rung the rest of the corpus has
#: is fitted over fewer points than its peers.
STANDARD_LEVELS: tuple[int, ...] = (1, 5, 10, 20, 50, 100)

DECISION_SUPPORT = ("tpcds", "dsb")

#: Gap kinds in the order the CLI prints them.
KINDS = (
    "designated-missing",
    "sweep-hole",
    "load-hole",
    "censored",
    "level-gap",
    "short-level",
    "short-pass",
)

#: The timeout error the runner records; a ladder whose last level has one was ejected.
TIMEOUT_ERROR_LIKE = "query exceeded timeout%"


class Gap(NamedTuple):
    """One coverage gap, with the command that would close it."""

    kind: str  # one of KINDS
    platform: str
    scale_factor: int
    compute_label: str
    detail: str
    fill_command: str
    block: str = "*"  # the suite(s) the gap is in, comma-joined; '*' when pooled


class Skip(NamedTuple):
    """One ``scale_skips`` row's key; each of the last three may be '*'."""

    platform: str
    scale_factor: int
    block: str
    compute_label: str
    run_type: str


def _skips(con: duckdb.DuckDBPyConnection) -> set[Skip]:
    """Every recorded deliberate omission.

    A database without the ``run_type`` column reads as '*' (every run type).
    """
    try:
        columns = {r[0] for r in con.execute("DESCRIBE scale_skips").fetchall()}
        run_type = "run_type" if "run_type" in columns else "'*'"
        rows = con.execute(
            f"SELECT platform, scale_factor, block, compute_label, {run_type} FROM scale_skips"
        ).fetchall()
    except Exception:
        logger.debug("no scale_skips table", exc_info=True)
        return set()
    return {Skip(p, int(s), b, lbl, rt) for p, s, b, lbl, rt in rows}


def _is_skipped(
    skips: set[Skip],
    platform: str,
    scale: int | None,
    label: str,
    *,
    block: str = "*",
    run_type: str = "*",
    clickbench_tags: set[int] | None = None,
) -> bool:
    """Whether a recorded skip covers this cell.

    A skip covers a cell when every key it carries matches, '*' matching anything.
    ``block`` or ``run_type`` given as '*' asks about a pooled cell (every suite, or
    every run type), which only a skip at '*' for that key covers: a DSB-only skip
    does not make a pooled cell out of scope. ``scale`` None asks about ClickBench,
    whose scale factor is a campaign tag, not a data volume: a skip recorded for
    ClickBench covers it at any tag, and an every-suite skip only at one of the
    platform's ClickBench tags (``clickbench_tags``), the report's convention. An
    every-suite skip at another scale (ClickHouse under load at 1 TB) is about that
    data volume, not the ClickBench table.
    """
    for s in skips:
        if s.platform != platform or (scale is not None and s.scale_factor != scale):
            continue
        if scale is None and not (
            s.block == "clickbench"
            or (s.block == "*" and s.scale_factor in (clickbench_tags or set()))
        ):
            continue
        if s.block not in ("*", block) or s.compute_label not in ("*", label):
            continue
        if s.run_type in ("*", run_type):
            return True
    return False


def _designated_label(platform: str, scale: int, target: Any) -> str:
    """The compute_label the runners stamp for a run-plan target.

    Falls back to the target verbatim when the mapping cannot apply. Sweep ladders
    are not all expressed in the same units: most name a run-plan *target* the runner
    translates (``XSMALL`` -> ``X-Small (Gen2)``, ``8`` -> ``8 RPU``), but
    ClickHouse's ladder is written as the label itself (``1x8GB``), because its knob
    is replica count while its ladder walks memory. Passing a label to a mapper
    expecting a target raises, and a coverage audit must never crash on a naming
    difference — the worst it should do is report a gap that is not one.
    """
    from ..report.canonical import _designated_label as impl
    from ..run_plan import RunPlan

    try:
        return impl(platform, scale, target, RunPlan.load()) or str(target)
    except (ValueError, TypeError, KeyError):
        logger.debug("no label mapping for %s/%s at SF%s", platform, target, scale)
        return str(target)


def _ladder_label(platform: str, scale: int, size: Any) -> str:
    """The compute_label a ladder rung carries.

    As :func:`_designated_label`, except BigQuery: its designated basis at 1 and
    10 GB is on-demand, but a ladder rung is a reservation size at every scale.
    """
    if platform == "bigquery":
        return f"ENTERPRISE / {size} slots"
    return _designated_label(platform, scale, size)


def _fill(kind: str, platform: str, scale: int, blocks: str, size: Any) -> str:
    """The command that would measure a missing ladder cell."""
    cmd = "sql" if kind == "sweep-hole" else "concurrency"
    extra = " --force" if cmd == "sql" else ""
    return (
        f"uv run benchmark {cmd} --scale {scale} --platforms {platform} --blocks {blocks}"
        f" --compute-size {size}{extra}"
    )


def _present(con: duckdb.DuckDBPyConnection) -> set[tuple[str, int, str, str, str]]:
    """``(platform, scale, label, run_type, block)`` with any row.

    A cell that ran and failed counts: it is measured, just not well.
    """
    return {
        (p, int(s), lbl, rt, b)
        for p, s, lbl, rt, b in con.execute(
            "SELECT DISTINCT platform, scale_factor, compute_label, run_type, block"
            " FROM sql_benchmark_results WHERE compute_label IS NOT NULL"
        ).fetchall()
    }


def _clickbench_tags(present: set) -> dict[str, set[int]]:
    """Each platform's ClickBench scale tags; 100 where it has no ClickBench rows."""
    tags: dict[str, set[int]] = {}
    for p, s, _lbl, _rt, b in present:
        if b == "clickbench":
            tags.setdefault(p, set()).add(s)
    return tags


def _designated_gaps(present: set, skipped: set[Skip]) -> list[Gap]:
    """designated-missing: the plan names a size for a scale, a suite has no warm rows."""
    from ..run_plan import RunPlan

    gaps = []
    for platform, eng in RunPlan.load().engines.items():
        for scale, target in eng.sizes.items():
            label = _designated_label(platform, int(scale), target)
            missing = [
                b
                for b in DECISION_SUPPORT
                if (platform, int(scale), label, "warm", b) not in present
                and not _is_skipped(skipped, platform, int(scale), label, block=b, run_type="warm")
            ]
            if missing:
                gaps.append(
                    Gap(
                        "designated-missing",
                        platform,
                        int(scale),
                        label,
                        "run plan designates this size for this scale; no warm rows for "
                        + ", ".join(missing),
                        f"uv run benchmark sql --scale {scale} --platforms {platform}"
                        f" --blocks {','.join(missing)}",
                        ",".join(missing),
                    )
                )
    return gaps


def _ladder_gaps(present: set, skipped: set[Skip]) -> list[Gap]:
    """sweep-hole / load-hole: every ladder size at every scale and suite.

    ClickBench is one fixed dataset, so its rows count at whatever scale tag they
    carry, and a ClickBench skip is one recorded with ``block = 'clickbench'``.
    """
    from ..run_plan import RunPlan

    plan = RunPlan.load()
    clickbench_present = {(p, lbl, rt) for p, _s, lbl, rt, b in present if b == "clickbench"}
    tags = _clickbench_tags(present)
    gaps = []
    for platform, eng in plan.engines.items():
        for run_type, ladder, kind in (
            ("warm", plan.sweep_ladder(platform), "sweep-hole"),
            ("concurrency", plan.load_ladder(platform), "load-hole"),
        ):
            what = "single-user" if run_type == "warm" else "under-load"
            for size in ladder:
                for scale in sorted(eng.sizes):
                    label = _ladder_label(platform, int(scale), size)
                    missing = [
                        b
                        for b in DECISION_SUPPORT
                        if (platform, int(scale), label, run_type, b) not in present
                        and not _is_skipped(
                            skipped, platform, int(scale), label, block=b, run_type=run_type
                        )
                    ]
                    if missing:
                        blocks = ",".join(missing)
                        gaps.append(
                            Gap(
                                kind,
                                platform,
                                int(scale),
                                label,
                                f"no {what} rows for {', '.join(missing)} at this size",
                                _fill(kind, platform, int(scale), blocks, size),
                                blocks,
                            )
                        )
                cb_tags = tags.get(platform, {100})
                cb_scale = min(cb_tags)
                cb_label = _ladder_label(platform, cb_scale, size)
                if (platform, cb_label, run_type) in clickbench_present or _is_skipped(
                    skipped,
                    platform,
                    None,
                    cb_label,
                    block="clickbench",
                    run_type=run_type,
                    clickbench_tags=cb_tags,
                ):
                    continue
                gaps.append(
                    Gap(
                        kind,
                        platform,
                        cb_scale,
                        cb_label,
                        f"no {what} ClickBench rows at this size",
                        _fill(kind, platform, cb_scale, "clickbench", size),
                        "clickbench",
                    )
                )
    return gaps


def _ejected_cells(con: duckdb.DuckDBPyConnection) -> set[tuple[str, int, str]]:
    """``(platform, scale, label)`` whose highest protocol level recorded a timeout.

    A sweep ejects a band after a level with timeouts
    (``scripts/sweeps/redshift_sweep.py``), so the levels above it are unrun by rule.
    """
    return {
        (p, int(s), lbl)
        for p, s, lbl in con.execute(
            f"""
            WITH top AS (
                SELECT platform, scale_factor, compute_label, max(concurrency) AS lvl
                FROM sql_benchmark_results
                WHERE run_type = 'concurrency' AND block IN {DECISION_SUPPORT}
                  AND concurrency IN {STANDARD_LEVELS} AND compute_label IS NOT NULL
                GROUP BY ALL)
            SELECT DISTINCT r.platform, r.scale_factor, r.compute_label
            FROM sql_benchmark_results r JOIN top t
              ON r.platform = t.platform AND r.scale_factor = t.scale_factor
             AND r.compute_label = t.compute_label AND r.concurrency = t.lvl
            WHERE r.run_type = 'concurrency' AND r.block IN {DECISION_SUPPORT}
              AND r.error_message LIKE '{TIMEOUT_ERROR_LIKE}'
            """
        ).fetchall()
    }


def _ladder_shape_gaps(con: duckdb.DuckDBPyConnection, skipped: set[Skip]) -> list[Gap]:
    """censored and level-gap, from the concurrency shapes."""
    from ..analysis.concurrency_shape import shapes

    levels_by_cell: dict[tuple[str, int, str], set[int]] = {}
    for p, s, lbl, lvl in con.execute(
        "SELECT DISTINCT platform, scale_factor, compute_label, concurrency"
        " FROM sql_benchmark_results"
        " WHERE run_type = 'concurrency' AND concurrency IS NOT NULL"
        "   AND compute_label IS NOT NULL"
        f"  AND block IN {DECISION_SUPPORT}"
    ).fetchall():
        levels_by_cell.setdefault((p, int(s), lbl), set()).add(int(lvl))
    ejected = _ejected_cells(con)
    gaps = []
    for cell in shapes(con):
        key = (cell.platform, cell.scale_factor, cell.compute_label)
        if not cell.publishable:
            continue
        if _is_skipped(
            skipped, cell.platform, cell.scale_factor, cell.compute_label, run_type="concurrency"
        ):
            continue
        if cell.censored:
            gaps.append(
                Gap(
                    "censored",
                    cell.platform,
                    cell.scale_factor,
                    cell.compute_label,
                    f"served {cell.useful_concurrency} at the highest level tried "
                    f"({cell.max_level}) — ceiling is a lower bound",
                    f"uv run benchmark concurrency --scale {cell.scale_factor} "
                    f"--platforms {cell.platform} --levels "
                    f"{','.join(str(x) for x in STANDARD_LEVELS if x > cell.max_level) or 'N'}",
                )
            )
        ran = levels_by_cell.get(key, set()) & set(STANDARD_LEVELS)
        top = max(ran, default=0)
        # Levels above an ejecting level are skipped by rule, not by oversight.
        missing = sorted(
            lvl for lvl in set(STANDARD_LEVELS) - ran if lvl < top or key not in ejected
        )
        if missing:
            gaps.append(
                Gap(
                    "level-gap",
                    cell.platform,
                    cell.scale_factor,
                    cell.compute_label,
                    f"ladder missing level(s) {missing}",
                    f"uv run benchmark concurrency --scale {cell.scale_factor} "
                    f"--platforms {cell.platform} --levels "
                    f"{','.join(str(m) for m in missing)}",
                )
            )
    return gaps


def _clickbench_level_gaps(
    con: duckdb.DuckDBPyConnection, skipped: set[Skip], tags: dict[str, set[int]]
) -> list[Gap]:
    """level-gap for ClickBench: a size under load missing protocol levels.

    ClickBench cells are one fixed dataset, so levels pool across its scale tags.
    Every size with any ClickBench load row is checked, publishable or not: a size
    whose ladder ran one level (ClickHouse 3x24GB: one query at one session) is the
    gap. Levels above a last level that had timeouts are ejected by rule.
    """
    gaps = []
    for p, lbl, tag, levels, top_timed_out in con.execute(
        f"""
        WITH lv AS (
            SELECT platform, compute_label, max(scale_factor) AS tag,
                   list(DISTINCT concurrency) AS levels, max(concurrency) AS top
            FROM sql_benchmark_results
            WHERE run_type = 'concurrency' AND block = 'clickbench'
              AND concurrency IN {STANDARD_LEVELS} AND compute_label IS NOT NULL
            GROUP BY 1, 2)
        SELECT lv.platform, lv.compute_label, lv.tag, lv.levels,
               EXISTS (SELECT 1 FROM sql_benchmark_results r
                       WHERE r.platform = lv.platform AND r.compute_label = lv.compute_label
                         AND r.block = 'clickbench' AND r.run_type = 'concurrency'
                         AND r.concurrency = lv.top
                         AND r.error_message LIKE '{TIMEOUT_ERROR_LIKE}')
        FROM lv ORDER BY 1, 2
        """
    ).fetchall():
        if _is_skipped(
            skipped,
            p,
            None,
            lbl,
            block="clickbench",
            run_type="concurrency",
            clickbench_tags=tags.get(p, set()),
        ):
            continue
        ran = {int(x) for x in levels}
        top = max(ran)
        missing = sorted(
            lvl for lvl in set(STANDARD_LEVELS) - ran if lvl < top or not top_timed_out
        )
        if missing:
            gaps.append(
                Gap(
                    "level-gap",
                    p,
                    int(tag),
                    lbl,
                    f"ClickBench ladder missing level(s) {missing}",
                    f"uv run benchmark concurrency --scale {tag} --platforms {p} --blocks clickbench"
                    f" --levels {','.join(str(m) for m in missing)}",
                    "clickbench",
                )
            )
    return gaps


def _short_gaps(
    con: duckdb.DuckDBPyConnection, skipped: set[Skip], tags: dict[str, set[int]]
) -> list[Gap]:
    """short-level and short-pass: a level or pass with fewer queries than its suite.

    A suite's size is the most distinct queries any warm pass recorded for it; an
    attempt counts however it ended, so a re-run that finished covers the cell.
    ClickBench is pooled across its scale tags (one fixed dataset: a pass cut short
    under one tag and re-run under another is covered).
    """
    gaps = []
    for rt, p, s, lbl, b, lvl, n, full in con.execute(
        f"""
        WITH suite AS (
            SELECT block, count(DISTINCT query_id) AS full_n
            FROM sql_benchmark_results WHERE run_type = 'warm' GROUP BY block),
        cells AS (
            SELECT run_type, platform,
                   CASE WHEN block = 'clickbench' THEN NULL ELSE scale_factor END AS sf,
                   max(scale_factor) AS tag_sf, compute_label, block, concurrency,
                   count(DISTINCT query_id) AS n
            FROM sql_benchmark_results
            WHERE compute_label IS NOT NULL
              AND (run_type = 'warm'
                   OR (run_type = 'concurrency' AND concurrency IN {STANDARD_LEVELS}))
            GROUP BY run_type, platform, sf, compute_label, block, concurrency)
        SELECT c.run_type, c.platform, coalesce(c.sf, c.tag_sf), c.compute_label, c.block,
               c.concurrency, c.n, s.full_n
        FROM cells c JOIN suite s USING (block)
        WHERE c.n < s.full_n
        ORDER BY 1, 2, 3, 4, 5, 6
        """
    ).fetchall():
        scale = None if b == "clickbench" else int(s)
        if _is_skipped(
            skipped, p, scale, lbl, block=b, run_type=rt, clickbench_tags=tags.get(p, set())
        ):
            continue
        if rt == "warm":
            gaps.append(
                Gap(
                    "short-pass",
                    p,
                    int(s),
                    lbl,
                    f"{b} single-user pass recorded {n} of {full} queries",
                    f"uv run benchmark sql --scale {s} --platforms {p} --blocks {b}"
                    f" --compute-size {lbl} --force",
                    b,
                )
            )
        else:
            gaps.append(
                Gap(
                    "short-level",
                    p,
                    int(s),
                    lbl,
                    f"{b} at {lvl} sessions recorded {n} of {full} queries (a pass cut short)",
                    f"uv run benchmark concurrency --scale {s} --platforms {p} --blocks {b}"
                    f" --levels {lvl}",
                    b,
                )
            )
    return gaps


def audit(con: duckdb.DuckDBPyConnection) -> list[Gap]:
    """Every coverage gap between the run plan's ladders and the results database."""
    skipped = _skips(con)
    present = _present(con)
    tags = _clickbench_tags(present)
    gaps = [
        *_designated_gaps(present, skipped),
        *_ladder_gaps(present, skipped),
        *_ladder_shape_gaps(con, skipped),
        *_clickbench_level_gaps(con, skipped, tags),
        *_short_gaps(con, skipped, tags),
    ]
    logger.info("coverage audit found %d gaps", len(gaps))
    return gaps

"""Report generator.

Queries ``results/benchmark.duckdb`` directly and produces:
  - ``report/draft.md``   — full markdown report with embedded tables
  - ``report/assets/*.png`` — static chart renders (plotly + kaleido)
  - ``report/assets/*.div.html`` — interactive plotly fragments, inlined by
    the HTML export (which injects plotly.js once)

Designed to be rerun any time new data lands. All content is data-driven;
the only hardcoded text is section headers and methodology boilerplate.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from . import narrative
from .compute_rates import effective_usd_per_hour
from .display import engine_display as _disp

logger = logging.getLogger("benchmark.report.generator")

# Per-series color palette shared by every chart (platforms/blocks cycle through it).
_CHART_COLORS = ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3", "#937860", "#DA8BC3"]


def _plotly():
    """Import plotly lazily; ``None`` (with a warning) when unavailable so the
    report degrades to tables-only."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        logger.warning("plotly not installed; skipping charts")
        return None
    return go, make_subplots


def _write_chart(fig, assets: Path, stem: str) -> None:
    """Write a chart in both renderings: an interactive div fragment
    (``<stem>.div.html``, inlined by the HTML export which injects plotly.js
    once) and a static PNG (``<stem>.png``, referenced by ``draft.md``; needs
    kaleido + a Chrome — on failure the PNG is skipped but the interactive
    HTML still works)."""
    div = assets / f"{stem}.div.html"
    # div_id defaults to a random UUID, which would make every `benchmark report`
    # rewrite every .div.html asset with a one-line phantom diff and break the
    # byte-identical-rebuild guarantee for the asset files. The stem is unique per
    # chart, so it is a stable, collision-free id.
    fig.write_html(div, include_plotlyjs=False, full_html=False, div_id=f"chart-{stem}")
    # plotly omits the trailing newline, which the end-of-file-fixer pre-commit hook
    # would then add, leaving every asset dirty after a render. Write the newline
    # ourselves.
    div.write_text(div.read_text(encoding="utf-8").rstrip("\n") + "\n", encoding="utf-8")
    try:
        import plotly.io as pio

        # Several chart feeds carry exact DECIMAL money sums (see _DECIMAL), which
        # reach the traces as decimal.Decimal. The div path serializes them via
        # plotly's own encoder, but kaleido's PNG engine uses strict orjson and
        # refuses Decimal — so normalize the figure through plotly JSON first.
        fig_json_native = pio.from_json(pio.to_json(fig))
        fig_json_native.write_image(assets / f"{stem}.png", scale=2)
        logger.info("wrote %s", assets / f"{stem}.png")
    except Exception:
        logger.warning(
            "static PNG export failed for %s (kaleido/Chrome missing?); "
            "interactive HTML still written",
            stem,
            exc_info=True,
        )


# Primary latency is server-side execution time ONLY — no wall-clock fallback.
# Wall-clock includes driver + network RTT, which makes cross-engine comparison
# unfair (esp. remote engines like MotherDuck), so it is not a reported metric.
# Every engine can supply server time after `benchmark reconcile`; rows still
# missing it are EXCLUDED from latency aggregates and named by _primary_metric_note.
_PRIMARY_MS = "server_time_ms"

# Concurrency reports the ENGINE's time on a query with its queue wait included
# and the network excluded (`engine_ms` in the `concurrency_results` view; see
# analysis/concurrency_shape.engine_ms_sql). Under load the interesting effect is
# queueing, and three engines' server time is execution only (Databricks,
# Redshift, BigQuery), so their queue time is added back; wall-clock is not used
# because it also carries network and result fetch. Cells whose engine time was
# not captured are withheld (`_capped_keys`).
_CONCURRENCY_MS = "engine_ms"

# Block families. ClickBench is reported SEPARATELY from the decision-support suites:
# it's a single ~100 GB web-analytics scan with vendor per-engine queries (perf-only,
# not correctness-compared) and it dominates bytes_scanned — so mixing it into the
# TPC-DS/DSB tables and cross-block rollups (esp. the sizing curve) distorts them.
DECISION_SUPPORT_BLOCKS = ("tpcds", "dsb")
CLICKBENCH_BLOCKS = ("clickbench",)


#: Cast used on every money/duration value that feeds a displayed SUM or AVG.
#: DuckDB parallelizes aggregation and DOUBLE addition is not associative, so the
#: same database can produce different published figures between `benchmark report`
#: runs: a Redshift suite cost can alternate between $0.73 and $0.74 as the low
#: bits of a 201-term sum move across a rounding boundary. DECIMAL addition
#: is exact, so the aggregate is reproducible regardless of thread scheduling.
#: Every displayed SUM/AVG of money or duration casts its input to this first, so
#: grep for it before adding a new one.
#:
#: A mean must be written ``SUM(CAST(x AS DECIMAL)) / NULLIF(COUNT(x), 0)`` rather
#: than ``AVG(CAST(x AS DECIMAL))``: DuckDB's AVG returns DOUBLE even for a DECIMAL
#: input, so the cast does nothing and the accumulation is order-dependent again.
#: SUM over DECIMAL is exact, COUNT is exact, and one IEEE division of two exact
#: operands is deterministic.
_DECIMAL = "DECIMAL(28,12)"


def _block_in(blocks: tuple[str, ...]) -> str:
    """SQL predicate restricting ``block`` to ``blocks`` (literals are fixed, safe)."""
    return "block IN (" + ", ".join(f"'{b}'" for b in blocks) + ")"


#: Row label for rows whose ``compute_label`` is NULL, in tables keyed on compute size.
UNLABELLED_COMPUTE = "unlabelled"


def _compute_row(label: str | None, layout_default: str | None = None) -> str:
    """Display label for a compute size in a table KEYED ON compute size.

    A NULL ``compute_label`` gets its own bucket. It must never borrow the
    ``engine_environment`` descriptor for its (platform, scale): a
    ``label or layout.get(...)`` fallback collapses two distinct GROUP BY groups
    onto one pivot key, and unless the query orders by ``compute_label``, which
    group survives is arbitrary.

    That makes published figures **nondeterministic**: the Redshift 128 RPU
    concurrency row at 100 GB would alternate between the real 402-row measurement
    (519 ms at 10 sessions) and a 102-row unlabelled sample of unknown compute
    size (1,658 ms) from one `benchmark report` to the next.

    ``layout_default`` is accepted and ignored, so call sites read explicitly as
    "the fallback was considered and rejected here" rather than looking like an
    oversight.
    """
    del layout_default
    return label or UNLABELLED_COMPUTE


def _unlabelled_note(con: duckdb.DuckDBPyConnection, blocks: tuple[str, ...]) -> list[str]:
    """Account for the ``unlabelled`` compute rows wherever they can appear.

    These are rows whose compute size was not captured at run time. They are kept
    as their own bucket rather than folded into a real size (see
    :func:`_compute_row`) or dropped, so the reader can see how much measurement
    sits outside the size-aware comparison instead of having it silently
    redistributed or silently discarded.
    """
    rows = con.execute(
        f"""
        SELECT platform, scale_factor, count(*) AS n
        FROM main.sql_benchmark_results
        WHERE compute_label IS NULL AND {_block_in(blocks)}
        -- platform/scale tiebreak: ties on n render in arbitrary (parallel
        -- hash-aggregate) order, flipping the published sentence between runs.
        GROUP BY 1, 2 ORDER BY n DESC, platform, scale_factor
        """
    ).fetchall()
    if not rows:
        return []
    total = sum(int(r[2]) for r in rows)
    detail = ", ".join(f"{_disp(p)} SF{int(sf)} ({int(n):,})" for p, sf, n in rows)
    return [
        f"_**`unlabelled`** rows are measurements without a usable compute size: the "
        f"label was either not captured at run time or voided by a quarantine, and the "
        f"row's metadata records which ({total:,} rows in this family: {detail}). They keep their own row "
        f"rather than being attributed to a real size, because a size-keyed comparison "
        f"cannot place them and folding them into a neighboring size would change that "
        f"size's published figures._",
        "",
    ]


#: Display names for suite blocks in the policy note.
_BLOCK_DISPLAY = {"tpcds": "TPC-DS", "dsb": "DSB", "clickbench": "ClickBench"}


def _policy_note(con: duckdb.DuckDBPyConnection, blocks: tuple[str, ...]) -> list[str]:
    """State the comparison-admission policy where the comparison tables are.

    A cell the policy excludes must read as *measured and excluded, here is why*,
    never as a gap — the counts come from the data so the note cannot say more or
    less than what was actually held back. Scoped to ``blocks`` so each family's
    note describes its own suites. See ``report/policy.py``.
    """
    from .policy import (
        DNF_MAX_FAILURE_SHARE,
        admitted_floor_headroom,
        single_session_replica_predicate,
    )

    lines: list[str] = []
    dnf = con.execute(f"""
        SELECT platform, block, scale_factor, judged_label, completed, suite_n
        FROM _dnf_cells
        WHERE {_block_in(blocks)} ORDER BY 1, 2, 3
    """).fetchall()
    if dnf:
        detail = "; ".join(
            f"{_disp(platform)} completed {ok} of {suite_n} "
            f"{_BLOCK_DISPLAY.get(block, block)} queries"
            + ("" if scale is None else f" at SF{int(scale)}")
            + ("" if label is None else f" (judged at {label}, its largest measured size)")
            for platform, block, scale, label, ok, suite_n in dnf
        )
        allowance = ""
        headroom = admitted_floor_headroom(con, blocks)
        if headroom is not None:
            worst, incomplete = headroom
            usage = f"every admitted cell is at least {worst:.0%} complete" + (
                f" ({incomplete} carry at least one failed query)" if incomplete else ""
            )
            allowance = (
                f" The allowance beneath the floor is directional: the queries an "
                f"engine drops are disproportionately the expensive ones, so an "
                f"admitted-but-incomplete cell flatters the engine that failed; "
                f"{usage}."
            )
        lines.append(
            f"_**DNF:** failing more than {DNF_MAX_FAILURE_SHARE:.0%} of a suite's "
            f"warm pass at a scale is did-not-finish, whichever suite it is, judged "
            f"at the largest compute size measured, so a small tier that cannot "
            f"finish stays a sizing result rather than an engine verdict. The "
            f"surviving latencies are a survivor statistic and enter no cross-engine "
            f"comparison: the completion figure is the result. Cells below the floor "
            f"here: {detail}. Failure modes are reported in the engine detail and "
            f"Coverage.{allowance}_"
        )
        lines.append("")
    excluded = con.execute(f"""
        SELECT scale_factor, compute_label, COUNT(DISTINCT query_id)
        FROM main.sql_benchmark_results r
        WHERE r.platform = 'clickhouse' AND r.run_type = 'warm'
          AND {_block_in(blocks)}
          AND NOT {single_session_replica_predicate("r")}
        GROUP BY 1, 2 ORDER BY 1
    """).fetchall()
    if excluded:
        detail = ", ".join(
            f"SF{int(sf)} at {label} ({int(n)} queries)" for sf, label, n in excluded
        )
        lines.append(
            f"_**Single-replica policy:** ClickHouse replicas add throughput, not "
            f"single-query speed, so warm comparisons admit only its single-replica "
            f"configurations, the same vertical-sizing axis as every other engine. "
            f"Multi-replica warm measurements excluded here: {detail}. Multi-replica "
            f"configurations appear on the concurrency axis, which measures what "
            f"replicas buy._"
        )
        lines.append("")
    return lines


def _is_clickbench(blocks: tuple[str, ...]) -> bool:
    """True for the ClickBench family, whose reporting dimensions differ.

    ClickBench is one fixed ~100 GB dataset. The ``scale_factor`` stamped on its
    rows records which scale campaign the run rode along with — a tracking
    artifact, not a data volume — so it must be pooled, and compute size becomes
    the axis in its place.
    """
    return tuple(blocks) == CLICKBENCH_BLOCKS


def _scale_expr(blocks: tuple[str, ...]) -> str:
    """The SQL expression to group by as "scale" for this block family.

    Collapses ClickBench's meaningless scale tag to a single value. Without this,
    one measurement of one dataset splits across three columns labelled SF1 /
    SF10 / SF100, and each engine appears only in the columns whose campaign it
    happened to ride along with, which can drop an engine out of ClickBench
    comparisons entirely.
    """
    return "100" if _is_clickbench(blocks) else "scale_factor"


def _bench_view(blocks: tuple[str, ...]) -> str:
    """Which deduplicated view this block family should read.

    ClickBench reads ``bench_cb``, which is size-aware and skips the canonical-tier
    filter. That filter maps a *scale* to each engine's designated compute size,
    which is meaningless for a single-scale workload: applied to ClickBench it would
    keep exactly one size per scale tag and label those sizes as if they were data
    volumes.
    """
    return "bench_cb" if _is_clickbench(blocks) else "bench"


def _present_blocks(con: duckdb.DuckDBPyConnection, candidates: tuple[str, ...]) -> tuple[str, ...]:
    """Subset of ``candidates`` that actually has rows in ``bench`` (for skipping
    an empty family so we don't emit a header with no data)."""
    have = {r[0] for r in con.execute("SELECT DISTINCT block FROM bench").fetchall()}
    return tuple(b for b in candidates if b in have)


def generate_report(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_dir: Path = Path("report"),
    *,
    compose_narrative: bool = True,
) -> Path:
    """Write ``report/draft.md`` and refresh charts.

    ``compose_narrative=False`` skips splicing the hand-authored prose
    (``report/sections/*.md``) and renders only the generated body. The prose's
    ``{{fact:...}}`` placeholders are editorial claims about the *real* results DB
    and fail loud when they cannot compute — correct in production, impossible to
    satisfy from a synthetic test DB. The e2e pipeline smoke uses this to validate
    the machinery without coupling itself to the narrative's data appetite.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    assets = output_dir / "assets"
    assets.mkdir(exist_ok=True)

    if not db_path.exists():
        md_path = output_dir / "draft.md"
        md_path.write_text("# Report\n\n_No results DB found._\n")
        return md_path

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        anchors: dict[str, int] = {}
        sections = _build_sections(con, assets, anchors)
        if compose_narrative:
            # Splice hand-authored prose (report/sections/*.md). A malformed section
            # or an unresolvable {{fact:...}} raises rather than shipping a broken
            # report.
            sections = narrative.compose(sections, con, anchor_positions=anchors)
    finally:
        con.close()

    md_path = output_dir / "draft.md"
    header = [
        "# 2026 Cloud Data Warehouse Benchmark: Draft Report",
        "",
        f"_Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} from `{db_path}`._",
        "",
    ]
    md_path.write_text("\n".join(header + sections) + "\n", encoding="utf-8")
    logger.info("report draft written to %s", md_path)

    # Also emit a single self-contained HTML (charts inlined as base64) so the
    # draft is shareable as one file. Best-effort: a failure here must not lose
    # the markdown draft.
    try:
        from .html_export import markdown_to_html

        markdown_to_html(md_path)
    except Exception:
        logger.warning("HTML export failed; markdown draft is still available", exc_info=True)

    return md_path


def _create_dedup_view(con: duckdb.DuckDBPyConnection) -> None:
    """Install the shared deduplicated views for this render.

    The views themselves (``results_latest``, ``bench``, ``bench_coverage``,
    ``bench_cb``) and the policy/canonical temp tables live in
    :mod:`benchmark.report.views` — one definition shared with the drift gate,
    the fact resolver, and the ``analyze`` exports. This wrapper only adds the
    per-render state reset (the capped-cell cache) and forces a fresh install.
    """
    from .views import install_views

    _capped_cache.clear()
    install_views(con, force=True)


def _build_sections(
    con: duckdb.DuckDBPyConnection, assets: Path, anchors: dict[str, int] | None = None
) -> list[str]:
    """Build every data-derived section.

    ``anchors``, when given, is populated with the line index at which each
    narrative anchor sits, so ``narrative.compose`` can splice hand-authored prose
    into the right place without this module knowing anything about that prose.
    """
    anchors = anchors if anchors is not None else {}
    _create_dedup_view(con)
    lines: list[str] = []
    # Lead with findings, not bookkeeping: TL;DR first, Coverage at the end.
    # The written lead is authored prose (report/sections/00-*, anchor
    # `executive-lead`) so it carries {{fact:...}} placeholders; the bullets stay
    # generated so they are recomputed from the DB on every build.
    lines += ["# Executive Summary", ""]
    anchors["executive-lead"] = len(lines)
    lines += _section_executive_summary(con)
    lines += _section_decision_guide(con)  # the stories the report drives at
    lines += _section_cost_confidence(con)  # global (platform × scale, block-agnostic)
    lines += _section_benchmark_run_cost(con)  # spend to produce this report, per engine
    anchors["after-front-matter"] = len(lines)

    # Decision-support suites (TPC-DS + DSB): scaled, ground-truth-validated.
    ds = _present_blocks(con, DECISION_SUPPORT_BLOCKS)
    if ds:
        anchors["before-decision-support"] = len(lines)
        lines += ["# Decision Support (TPC-DS + DSB)", ""]
        lines += _policy_note(con, ds)
        lines += _section_headlines(con, ds)
        lines += _section_latency(con, assets, ds, "ds")
        lines += _section_cost(con, assets, ds, "ds")
        lines += _section_efficiency(con, assets, ds, "ds")
        lines += _section_profiling(con, ds)
        lines += _section_skew(con, assets)  # TPC-DS vs DSB only
        lines += _section_patterns(con, assets)  # pattern tags are TPC-DS/DSB

    # ClickBench: reported separately (single ~100 GB scan, perf-only queries).
    cb = _present_blocks(con, CLICKBENCH_BLOCKS)
    if cb:
        anchors["before-clickbench"] = len(lines)
        lines += [
            "# ClickBench",
            "",
            "ClickBench is a **web-analytics** benchmark: 43 queries over a single ~100 GB "
            "`hits` table (single scale, not scaled per SF), using the SQL published in "
            "the ClickBench project for each engine. It is **performance-only** (results "
            "are not compared against DuckDB ground truth), and its scans dominate bytes, "
            "so it is reported "
            "apart from the decision-support suites above.",
            "",
        ]
        lines += _policy_note(con, cb)
        lines += _section_headlines(con, cb)
        lines += _section_latency(con, assets, cb, "cb")
        lines += _section_cost(con, assets, cb, "cb")
        lines += _section_efficiency(con, assets, cb, "cb")
        lines += _section_profiling(con, cb)

    anchors["before-methodology"] = len(lines)
    lines += _section_methodology(con)
    lines += _section_row_count_agreement(con)
    lines += _section_coverage(con)  # completeness matrix (appendix — bookkeeping)
    lines += _section_provenance(con)
    return lines


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_table(cols: list[str], rows: list[tuple], *, float_fmt: str = ".2f") -> list[str]:
    """Render DuckDB rows (native Python types) as a markdown table.

    DuckDB returns ``None`` for NULL, ``int`` for counts, and ``float`` only for
    genuine floats, so there's no pandas NaN coercion (which would turn integer
    columns like ``errors``/``scale_factor`` into ``0.0``/``1.0`` and NULLs into
    float NaN). NULL → ``—``; floats use ``float_fmt``; everything else ``str()``.
    """
    if not rows:
        return ["_(no data)_", ""]
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for row in rows:
        cells = []
        for v in row:
            if v is None:
                cells.append("—")
            elif isinstance(v, float):
                cells.append(f"{v:{float_fmt}}")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _query_table(con: duckdb.DuckDBPyConnection, sql: str, *, float_fmt: str = ".2f") -> list[str]:
    """Run ``sql`` and render the result as a markdown table, straight from DuckDB."""
    cur = con.execute(sql)
    return _render_table([d[0] for d in cur.description], cur.fetchall(), float_fmt=float_fmt)


# Cells whose failures were OUR client, not the engine. Resolved once per report run:
# several independent sections read concurrency error rates, and each must withhold
# the same cells the prose says are withheld, or the report contradicts itself in one
# document. Reset by `_create_dedup_view`, which every render calls first.
_capped_cache: dict[tuple[str, tuple[str, ...]], list] = {}


def _capped_cells(
    con: duckdb.DuckDBPyConnection,
    run_type: str = "concurrency",
    blocks: tuple[str, ...] = DECISION_SUPPORT_BLOCKS,
) -> list:
    """Client-capped cells for ``run_type`` within ``blocks``, once per report run.

    Block-scoped on purpose. Every Snowflake client cancellation is in TPC-DS or DSB;
    its ClickBench ladders have none. Detecting at (platform, scale, size) while
    ignoring the block would withhold a clean ClickBench ladder because of the
    decision-support ladder beside it, and over-withholding is its own kind of
    dishonesty about coverage.

    Memoized because detection logs one WARNING per affected cell, and recomputing it
    per section would print the same warnings repeatedly and read as several distinct
    problems in the audit trail.
    """
    from ..analysis.concurrency_shape import capped_cells

    key = (run_type, blocks)
    if key not in _capped_cache:
        _capped_cache[key] = capped_cells(con, run_type=run_type, blocks=blocks)
    return _capped_cache[key]


def _client_cancel() -> str:
    """SQL predicate for failures our client caused (one shared definition)."""
    from ..analysis.concurrency_shape import client_cancel_predicate

    return client_cancel_predicate()


def _engine_ms_sql() -> str:
    """SQL for the engine's time on a query (queue + execution, no network)."""
    from ..analysis.concurrency_shape import engine_ms_sql

    return engine_ms_sql()


def _failure_class_sql() -> str:
    """SQL naming a concurrency failure's cause (one shared definition)."""
    from ..analysis.concurrency_shape import failure_class_sql

    return failure_class_sql()


def _withheld_cells_note(
    con: duckdb.DuckDBPyConnection, blocks: tuple[str, ...] = DECISION_SUPPORT_BLOCKS
) -> list[str]:
    """Name every withheld cell and why, immediately under the table it is missing
    from. A `withheld` marker a reader cannot resolve is worse than a bad number:
    the whole point is that the gap is ours and attributable."""
    from ..analysis.concurrency_shape import MIN_ENGINE_TIME_PCT, engine_time_withheld_cells

    cells = _capped_cells(con, "concurrency", blocks)
    untimed = engine_time_withheld_cells(con, blocks)
    lines: list[str] = []
    if untimed:
        by_platform: dict[str, int] = {}
        for platform, _sf, _lbl in untimed:
            by_platform[platform] = by_platform.get(platform, 0) + 1
        named = ", ".join(f"{_disp(p)} ({n})" for p, n in sorted(by_platform.items()))
        lines += [
            "",
            f"**{len(untimed)} cells marked `withheld` have no engine time to report:** "
            f"{named}. Latency here is the engine's time on a query, its queue wait "
            "included and the network excluded. Databricks, Redshift and BigQuery report "
            "execution only, so their cells need queue time from the engine's history: "
            "`benchmark reconcile` back-fills it for Databricks and BigQuery, and Redshift "
            "keeps seven days of history, so a Redshift cell older than that needs a "
            "re-run. On the other engines a cell is withheld when engine time is missing "
            f"for more than {100 - MIN_ENGINE_TIME_PCT:.0f}% of a level's successful queries. "
            "Their failure rates stand; their latency is not published.",
            "",
        ]
    if not cells:
        return lines
    lines += [
        "",
        f"**{len(cells)} cells marked `withheld` are awaiting re-measurement.** Their "
        "runs were cut short by a client-side limit rather than by the engine, which "
        "makes both the error rate and the latency median a property of the client, so "
        "they are excluded from every chart and aggregate here. The rows remain in the "
        "raw results database with their original error text.",
        "",
    ]
    lines += _render_table(
        ["Engine", "Scale", "Compute", "Rows", "Client-terminated"],
        [
            (
                c.platform,
                f"SF{c.scale_factor}",
                c.compute_label,
                c.rows,
                f"{c.client_cancelled} ({c.client_cancel_pct:.1f}%)",
            )
            for c in cells
        ],
    )
    return lines


def _capped_keys(
    con: duckdb.DuckDBPyConnection,
    run_type: str = "concurrency",
    blocks: tuple[str, ...] = DECISION_SUPPORT_BLOCKS,
) -> set:
    """``{(platform, scale_factor, compute_label)}`` that must not be published as
    engine behaviour. Withholding is whole-cell: a client cancellation biases the
    surviving latency median as well as the error rate, so keeping the levels that
    happened not to cancel would still publish a curve whose shape is partly ours.
    Concurrency cells whose engine time was not captured are withheld the same way."""
    from ..analysis.concurrency_shape import engine_time_withheld_cells

    keys = {
        (c.platform, c.scale_factor, c.compute_label) for c in _capped_cells(con, run_type, blocks)
    }
    if run_type == "concurrency":
        # No engine time, no latency: the cell's median would be something else.
        keys |= set(engine_time_withheld_cells(con, blocks))
    return keys


def _render_pivot(
    row_header: str,
    cells: dict[tuple[str, str], str],
    *,
    row_order: list[str] | None = None,
    col_order: list[str] | None = None,
) -> list[str]:
    """Render ``{(row, col) -> cell}`` as a compact 2-D markdown pivot.

    The report's flat tables collapse every dimension into one long
    row-per-combination list; pivots keep one dimension across the columns
    (typically scale factor) so a row reads as a trajectory. Missing cells
    render as ``—``.
    """
    if not cells:
        return ["_(no data)_", ""]
    rows = row_order or sorted({r for r, _c in cells})
    cols = col_order or sorted({c for _r, c in cells})
    lines = [
        "| " + row_header + " | " + " | ".join(cols) + " |",
        "| " + " | ".join(["---"] * (len(cols) + 1)) + " |",
    ]
    for r in rows:
        lines.append("| " + r + " | " + " | ".join(cells.get((r, c), "—") for c in cols) + " |")
    lines.append("")
    return lines


def _scale_col(blocks: tuple[str, ...], scale: int) -> str:
    """Pivot column label for a scale. ClickBench has one dataset, so its pooled
    column says so rather than reading as a scale factor it does not have."""
    return "one ~100 GB dataset" if _is_clickbench(blocks) else f"SF{scale}"


def _sf_order(cols: set[str]) -> list[str]:
    """Order ``SF…``-prefixed column labels numerically, not lexically."""

    def key(label: str) -> tuple[int, str]:
        head = label.removeprefix("SF").split(" ")[0].split(" ·")[0]
        return (int(head), label) if head.isdigit() else (1 << 30, label)

    return sorted(cols, key=key)


def _section_executive_summary(con: duckdb.DuckDBPyConnection) -> list[str]:
    """The executive summary's generated bullets: the fastest / cheapest
    configuration at the largest scale, the cross-engine takeaway, and what
    coverage is still missing. The written lead above them is authored prose
    (report/sections/00-*, spliced at the `executive-lead` anchor; the summary
    stays hybrid by design — written argument over
    recomputed bullets). The ranking bullets admit only engines whose
    decision-support pass at that scale is complete under the DNF policy — a
    partial suite is cheaper and faster than a full one for the wrong reason.
    Falls back gracefully on partial data."""
    layout = _compute_layout(con)
    ds = _present_blocks(con, DECISION_SUPPORT_BLOCKS)
    lines: list[str] = []
    if not ds:
        lines += ["_No decision-support results yet._", ""]
        return lines

    # (platform, scale, compute) -> (geomean_ms, suite_runtime_s, suite_cost_usd)
    rows = _headline_rows(con, ds)
    pts = []
    for platform, scale, lbl, _q, geo, _p50, _tail, runtime, cost, *_ in rows:
        compute = _compute_row(lbl, layout.get((platform, int(scale))))
        pts.append((platform, int(scale), compute, geo, runtime, cost))
    scales = sorted({p[1] for p in pts})
    plats = sorted({p[0] for p in pts})
    max_sf = scales[-1]

    # An engine with a DNF'd decision-support block at max_sf has a PARTIAL suite
    # aggregate there (the policy strips the failed block), so ranking it on suite
    # cost or geomean would compare different query sets. Name it instead.
    dnf_at_max = {
        str(p)
        for (p,) in con.execute(
            "SELECT DISTINCT platform FROM _dnf_cells "
            "WHERE block IN ('tpcds', 'dsb') AND scale_factor = ?",
            [max_sf],
        ).fetchall()
    }
    top = [p for p in pts if p[1] == max_sf and p[0] not in dnf_at_max]

    def _fmt(p: tuple) -> str:
        return f"**{_disp(p[0])} {p[2]}**"

    bullets: list[str] = [
        f"- **Scope:** {len(plats)} engine(s) ({', '.join(_disp(p) for p in plats)}) across "
        f"{len(scales)} scale factor(s) (SF{'/SF'.join(str(s) for s in scales)}), "
        "decision-support suites (TPC-DS + DSB). Latency is server-side time; "
        "cost is per-query where captured.",
    ]
    fastest = min((p for p in top if p[3] is not None), key=lambda p: p[3], default=None)
    if fastest:
        bullets.append(
            f"- **Fastest at SF{max_sf}:** {_fmt(fastest)}: {fastest[3]:,.0f} ms suite "
            "geomean (per-query median)."
        )
    costed = [p for p in top if p[5] is not None and p[5] > 0]
    cheapest = min(costed, key=lambda p: p[5], default=None)
    if cheapest:
        bullets.append(
            f"- **Cheapest full pass at SF{max_sf}:** {_fmt(cheapest)}: "
            f"${cheapest[5]:,.2f} (sum of per-query costs), vs "
            f"${max(p[5] for p in costed):,.2f} for the priciest costed config."
        )
    # Efficiency crossover: cheapest and fastest disagree ⇒ a real cost/latency tradeoff.
    if fastest and cheapest and fastest[0:3] != cheapest[0:3]:
        bullets.append(
            f"- **The tradeoff is real:** the fastest and cheapest SF{max_sf} configs "
            "differ, so there's no single winner: the Pareto frontier (below) shows "
            "which sizes are worth their price and which are dominated."
        )
    # One bullet owns every policy exclusion (two bullets would name the same
    # engine twice in two vocabularies): an
    # engine DNF'd at the top scale, plus any blank (platform, scale) cell the
    # policy excludes (DNF at another scale, or ClickHouse multi-replica-only
    # data — see report/policy.py). Cells blank for any OTHER reason stay a
    # separate coverage bullet, because a deliberate exclusion is not a gap.
    from .policy import single_session_replica_predicate

    excluded = {
        (str(p), int(sf))
        for p, sf in con.execute(f"""
            SELECT platform, scale_factor FROM _dnf_cells
            WHERE block IN ('tpcds', 'dsb')
            UNION
            SELECT r.platform, r.scale_factor
            FROM main.sql_benchmark_results r
            WHERE r.run_type = 'warm' AND r.block IN ('tpcds', 'dsb')
              AND NOT {single_session_replica_predicate("r")}
            GROUP BY 1, 2
        """).fetchall()
    }
    present = {(p[0], p[1]) for p in pts}
    blank = [(pl, sf) for pl in plats for sf in scales if (pl, sf) not in present]
    policy_excluded = sorted(
        {f"{_disp(pl)} @ SF{max_sf}" for pl in dnf_at_max}
        | {f"{_disp(pl)} @ SF{sf}" for pl, sf in blank if (pl, sf) in excluded}
    )
    excluded_platforms = set(dnf_at_max) | {pl for pl, sf in blank if (pl, sf) in excluded}
    missing = sorted(f"{_disp(pl)} @ SF{sf}" for pl, sf in blank if (pl, sf) not in excluded)
    if policy_excluded:
        bullet = (
            "- **Measured, but excluded from the rankings by the comparison policy:** "
            + ", ".join(policy_excluded)
            + ": a decision-support suite below the completion floor covers fewer "
            "queries than the engines ranked above and would read as cheaper or "
            "faster for the wrong reason; multi-replica-only cells are excluded "
            "from single-session comparison. Completion figures are in the policy "
            "note and the engine profiles."
        )
        if "clickhouse" in excluded_platforms:
            # The exclusion alone reads as a verdict; name the mechanism and
            # the regime the engine wins instead.
            bullet += (
                " For ClickHouse the exclusion marks a fit boundary rather than a "
                "quality verdict: a single query's join memory is capped by one "
                "replica (replicas multiply throughput; by default they do not pool memory "
                "into a shared shuffle), so the 1 TB join suites outgrow even its "
                "largest 120 GB replica, while on wide-table scan aggregates it is "
                "the price-performance leader, serving the ~100 GB ClickBench "
                "table from its smallest 8 GB instance (Part III's regime map)."
            )
        bullets.append(bullet)
    if missing:
        bullets.append(
            "- **Not yet covered (decision-support):** "
            + ", ".join(missing)
            + ". Treat cross-engine comparisons at those scales as incomplete."
        )
    lines += bullets
    lines += [""]
    return lines


def _tier_ordinal(plat: str, label: str) -> float:
    """Provisioned-capacity ordering (NOT price): RPU/slot counts, replicas×GB,
    or the engine's named tier ladder. 'on-demand' is a pricing model, not a
    size — excluded from the ladder (nan)."""
    import re as _re

    if label == "on-demand":
        return float("nan")
    m = _re.search(r"(\d+)\s*(RPU|slots)", label)
    if m:
        return float(m.group(1))
    m = _re.match(r"(\d+)x(\d+)GB", label)
    if m:
        return float(m.group(1)) * float(m.group(2))
    ladders = {
        "snowflake": ["X-Small", "Small", "Medium", "Large", "X-Large"],
        "motherduck": ["Pulse", "Standard", "Jumbo", "Mega", "Giga"],
        "databricks": ["2X-Small", "X-Small", "Small", "Medium", "Large", "X-Large", "2X-Large"],
    }
    for i, name in enumerate(ladders.get(plat, [])):
        if label.startswith(name):
            return float(i)
    m = _re.match(r"F(\d+)", label)  # fabric SKUs
    if m:
        return float(m.group(1))
    return float("nan")


def _sizing_matrix_lines(
    con: duckdb.DuckDBPyConnection,
    *,
    blocks: tuple[str, ...],
    title: str,
    level_cols: tuple[int, ...],
    scale: int | None = 100,
) -> list[str]:
    """The size × concurrency × cost matrix + best-value inversion table for
    one workload family. ``scale=None`` pools across scale tags (ClickBench is
    one dataset; its scale_factor is just the run tier). Each engine's "at
    load" level is the highest it measured — level ladders differ per session
    design — and is named in the table. Cost medians use successful runs only;
    tiers with ≥10% errors at load are disqualified from value scoring."""
    import math as _math

    bf = ", ".join(f"'{b}'" for b in blocks)
    sf = f"AND scale_factor = {scale}" if scale is not None else ""
    raw = con.execute(f"""
        SELECT platform, compute_label, concurrency,
               MEDIAN({_CONCURRENCY_MS})/1000 AS p50_s,
               100.0*COUNT(*) FILTER (WHERE error_message IS NOT NULL)/COUNT(*) AS err,
               MEDIAN(cost_usd) FILTER (WHERE error_message IS NULL)*1000 AS usd1k,
               COUNT(*) AS n
        FROM concurrency_results
        WHERE block IN ({bf}) {sf} AND failure_class IS DISTINCT FROM 'harness'
          AND compute_label IS NOT NULL
        GROUP BY 1, 2, 3
    """).fetchall()
    # (plat, label) -> {level: (p50, err, usd1k)}. Cells with < 10 measured
    # runs are dropped: partial configs (e.g. a truncated run) would otherwise
    # crown junk value leaders from a handful of rows.
    # Client-capped cells are dropped outright rather than shown with an error rate:
    # both their latency and their shed rate measure our 90s socket timeout, and the
    # value scoring below would disqualify a tier for a failure that was ours.
    capped = _capped_keys(con, blocks=blocks)
    grid: dict[tuple[str, str], dict[int, tuple]] = {}
    for plat, lbl, lvl, p50, err, usd, n in raw:
        if _math.isnan(_tier_ordinal(plat, lbl)) or n < 10:
            continue
        if scale is not None and (plat, int(scale), lbl) in capped:
            continue
        if scale is None and any(k[0] == plat and k[2] == lbl for k in capped):
            continue
        # a zero/absent cost median means attribution hasn't landed (e.g.
        # Snowflake's 6-8h delay) — treat as unknown, not free
        usd = usd if usd and usd > 0 else None
        grid.setdefault((plat, lbl), {})[int(lvl)] = (p50, err or 0.0, usd)
    plats = sorted({k[0] for k in grid})
    if not any(len([k for k in grid if k[0] == p]) > 1 for p in plats):
        return []

    lines = ["", f"### {title}", ""]

    def _cell(entry, with_err=True):
        if entry is None:
            return "—"
        p50, err, _ = entry
        s = f"{p50:.1f} s"
        if with_err and err >= 5:
            s += f" ({err:.0f}% err)"
        return s

    max_level: dict[str, int] = {}
    for (plat, _lbl), levels in grid.items():
        max_level[plat] = max(max_level.get(plat, 0), max(levels))

    rows_out = []
    for plat in plats:
        tiers = sorted((k for k in grid if k[0] == plat), key=lambda k: _tier_ordinal(plat, k[1]))
        ml = max_level[plat]
        for key in tiers:
            levels = grid[key]
            load = levels.get(ml)
            rows_out.append(
                (
                    _disp(plat),
                    key[1],
                    *[_cell(levels.get(lc), with_err=False) for lc in level_cols],
                    f"{_cell(load)} @{ml}" if load is not None else "—",
                    "—" if load is None or load[2] is None else f"${load[2]:,.2f}",
                )
            )
    lines += _render_table(
        ["Engine", "Tier", *[f"p50 @{lc}" for lc in level_cols], "at load", "$/1k at load"],
        rows_out,
    )

    lines += ["", "#### Best-value tier: solo vs loaded", ""]
    inv_rows = []
    for plat in plats:
        tiers = [k for k in grid if k[0] == plat]
        ml = max_level[plat]

        def _value(key, level, *, disqualify_err=False):
            e = grid[key].get(level)
            if e is None or e[0] is None or not e[2] or e[2] <= 0:
                return None
            if disqualify_err and e[1] >= 10:
                return None
            return e[0] * e[2]

        solo = min(
            (k for k in tiers if _value(k, 1) is not None), key=lambda k: _value(k, 1), default=None
        )
        load = min(
            (k for k in tiers if _value(k, ml, disqualify_err=True) is not None),
            key=lambda k: _value(k, ml, disqualify_err=True),
            default=None,
        )
        if solo is None and load is None:
            continue
        costs = [grid[k][ml][2] for k in tiers if grid[k].get(ml) and grid[k][ml][2]]
        flat_cost = len(costs) > 1 and max(costs) / min(costs) < 1.1
        loaded = [k for k in tiers if grid[k].get(ml)]
        heavy_err = bool(loaded) and all(grid[k][ml][1] >= 25 for k in loaded)
        if heavy_err:
            reading = f"no tier survives {ml}-way concurrency on this workload"
        elif load is None:
            reading = f"every tier degrades or errors at {ml}: size is not the fix"
        elif flat_cost:
            reading = "same cost at every size: capacity mainly buys concurrency here"
        elif solo is not None and solo[1] == load[1]:
            reading = f"{load[1]} dominates at every load level"
        else:
            up = solo is not None and _tier_ordinal(plat, load[1]) > _tier_ordinal(plat, solo[1])
            direction = (
                "bigger tiers are a concurrency buy"
                if up
                else "the top tier's solo edge doesn't survive queueing: mid tier wins"
            )
            reading = (
                f"inverts under load: {solo[1] if solo else '—'} solo → "
                f"{load[1]} at {ml} streams: {direction}"
            )
        inv_rows.append(
            (_disp(plat), solo[1] if solo else "—", f"{load[1]} (@{ml})" if load else "—", reading)
        )
    lines += _render_table(["Engine", "Value tier solo", "Value tier loaded", "Reading"], inv_rows)
    lines += [
        "",
        "_Value = median latency × cost per 1k successful queries at that level; "
        "tiers with ≥10% errors at load are disqualified. 'At load' is each "
        "engine's highest measured level (ladders differ by session design). "
        "Tier order is provisioned capacity; 'on-demand' is excluded._",
        "",
    ]
    return lines


def _section_decision_guide(con: duckdb.DuckDBPyConnection) -> list[str]:
    """ "Which engine when" — the stories the benchmark drives at, first thing
    after the TL;DR. Editorial verdicts live in ``verdicts.py`` (hand-
    maintained, number-light); every figure rendered here is computed live
    from the ``bench`` view so the section stays current as new data lands.
    Engines with a verdict but no data render as "campaign pending"."""
    from ..queries.registry import QueryRegistry
    from .verdicts import VERDICTS

    lines = ["## Decision Guide: Which Engine When", ""]
    lines += [
        "The benchmark's bottom line: what to pick, per workload shape, once "
        "**ability** (query patterns), **latency** (solo and under load), "
        "**concurrency**, and **cost** are weighed together. Verdicts are "
        "editorial; every number is computed from the results DB at each "
        "engine's designated run-plan compute tier (SF100 unless noted).",
        "",
    ]

    present = set(_platforms(con))

    # Live stats per platform @ SF100: warm p50 / $ per 1k (tpcds+dsb), conc
    # p50 at level 1 and 100 + error rate at 100, DNF% at each platform's max scale.
    stats: dict[str, dict] = {}
    for plat, p50, usd_1k in con.execute("""
        SELECT platform, MEDIAN(server_time_ms)/1000, MEDIAN(cost_usd)*1000
        FROM bench
        WHERE run_type='warm' AND scale_factor=100 AND block IN ('tpcds','dsb')
          AND error_message IS NULL
        GROUP BY 1
    """).fetchall():
        stats.setdefault(plat, {})["p50"] = p50
        stats[plat]["usd_1k"] = usd_1k
    for plat, l1, l100, err100 in con.execute(f"""
        SELECT platform,
               MEDIAN(engine_ms) FILTER (WHERE concurrency=1)/1000,
               MEDIAN(engine_ms) FILTER (WHERE concurrency=100)/1000,
               100.0 * COUNT(*) FILTER (WHERE concurrency=100 AND error_message IS NOT NULL)
                   / NULLIF(COUNT(*) FILTER (WHERE concurrency=100), 0)
        FROM (SELECT *, {_engine_ms_sql()} AS engine_ms, {_failure_class_sql()} AS fc
              FROM bench WHERE run_type='concurrency') b
        WHERE scale_factor=100 AND block IN ('tpcds','dsb') AND fc IS DISTINCT FROM 'harness'
        GROUP BY 1
    """).fetchall():
        stats.setdefault(plat, {}).update(l1=l1, l100=l100, err100=err100)
    for plat, sf, dnf in con.execute("""
        WITH mx AS (
          SELECT platform, MAX(scale_factor) AS sf FROM bench
          WHERE run_type='warm' AND block IN ('tpcds','dsb') GROUP BY 1)
        SELECT b.platform, mx.sf,
               100.0 * COUNT(DISTINCT b.query_id) FILTER (WHERE b.error_message IS NOT NULL)
                   / COUNT(DISTINCT b.query_id)
        FROM bench b JOIN mx ON b.platform = mx.platform AND b.scale_factor = mx.sf
        WHERE b.run_type='warm' AND b.block IN ('tpcds','dsb')
        GROUP BY 1, 2
    """).fetchall():
        stats.setdefault(plat, {}).update(max_sf=sf, dnf=dnf)
    # A DNF cell outranks the per-query failure share: "1% @SF10" (its largest
    # policy-admitted scale) must not stand in for "did not finish the 1 TB suite".
    # DNF is judged per suite; this decision-support column shows the largest DNF
    # scale, with each DNF'd suite's completion named so two engines' cells stay
    # comparable when one failed a single suite and the other failed both.
    for plat, sf, blocks_s, oks_s, ns_s in con.execute("""
        SELECT platform, scale_factor,
               STRING_AGG(block, ',' ORDER BY block DESC) AS blocks,
               STRING_AGG(completed, ',' ORDER BY block DESC) AS oks,
               STRING_AGG(suite_n, ',' ORDER BY block DESC) AS ns
        FROM _dnf_cells WHERE block IN ('tpcds', 'dsb')
        GROUP BY 1, 2
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY platform ORDER BY scale_factor DESC) = 1
    """).fetchall():
        detail = ", ".join(
            f"{_BLOCK_DISPLAY.get(b, b)} {ok}/{n}"
            for b, ok, n in zip(blocks_s.split(","), oks_s.split(","), ns_s.split(","), strict=True)
        )
        stats.setdefault(plat, {}).update(dnf_cell=(int(sf), detail))

    # Punchline table
    def _n(v, fmt=".2f", suffix=""):
        return "—" if v is None else f"{v:{fmt}}{suffix}"

    lines += ["### Pick it when…", ""]
    cols = ["Engine", "Pick it when…", "Solo p50", "p50 @100 users", "$/1k queries", "DNF @max SF"]
    rows = []
    for plat in sorted(present | set(VERDICTS)):
        v = VERDICTS.get(plat, {})
        s = stats.get(plat, {})
        if plat not in present:
            rows.append((_disp(plat), "_campaign pending — no results yet_", "—", "—", "—", "—"))
            continue
        dnf = s.get("dnf")
        dnf_s = "—" if dnf is None else f"{dnf:.0f}% @SF{s.get('max_sf', '?')}"
        if s.get("dnf_cell"):
            cell_sf, detail = s["dnf_cell"]
            dnf_s = f"**DNF @SF{cell_sf}** ({detail} completed)"
        rows.append(
            (
                _disp(plat),
                v.get("pick_when", "_verdict pending_"),
                _n(s.get("p50"), suffix=" s"),
                _n(s.get("l100"), suffix=" s")
                + (f" ({s['err100']:.0f}% err)" if s.get("err100") else ""),
                "$" + _n(s.get("usd_1k")),
                dnf_s,
            )
        )
    lines += _render_table(cols, rows)

    # Per-pattern leaders (fastest / cheapest), computed from pattern tags.
    tag_rows = [(q.id, tag) for q in QueryRegistry().all() for tag in q.patterns]
    con.execute("CREATE OR REPLACE TEMP TABLE _dg_patterns (query_id VARCHAR, pattern VARCHAR)")
    con.executemany("INSERT INTO _dg_patterns VALUES (?, ?)", tag_rows)
    leader_rows = con.execute("""
        WITH per AS (
          SELECT p.pattern, b.platform,
                 MEDIAN(b.server_time_ms)/1000 AS med_s,
                 MEDIAN(b.cost_usd)*1000 AS usd_1k
          FROM bench b JOIN _dg_patterns p USING (query_id)
          -- decision-support only: ClickBench queries carry pattern tags too,
          -- but they are the ClickBench project's per-engine SQL riding a scale TAG of
          -- 100, so they would contaminate an SF100 cross-engine comparison.
          WHERE b.run_type='warm' AND b.scale_factor=100 AND b.error_message IS NULL
            AND b.block IN ('tpcds', 'dsb')
          GROUP BY 1, 2)
        SELECT pattern,
               ARG_MIN(platform, med_s), MIN(med_s),
               ARG_MIN(platform, usd_1k), MIN(usd_1k)
        FROM per GROUP BY 1 ORDER BY 1
    """).fetchall()
    if leader_rows:
        lines += ["", "### Query-pattern leaders (SF100, warm)", ""]
        lines += _render_table(
            ["Pattern", "Fastest", "Median", "Cheapest", "$/1k"],
            [
                (pat, _disp(fp), f"{fs:.2f} s", _disp(cp), f"${cu:.2f}")
                for pat, fp, fs, cp, cu in leader_rows
            ],
        )

    lines += _sizing_matrix_lines(
        con,
        blocks=("tpcds", "dsb"),
        title="The sizing matrix: size × concurrency (decision support, SF100)",
        level_cols=(1, 10, 50),
    )
    lines += _sizing_matrix_lines(
        con,
        blocks=("clickbench",),
        title="The sizing matrix: ClickBench (single 100 GB dataset, scan workload)",
        level_cols=(1, 10, 20),
        scale=None,  # one dataset; scale tags are just run tiers — pool them
    )

    # Narrative blocks: strengths + operating envelope, per engine with data.
    # Framed on what each engine does well and the regime it targets — never on
    # what it "fails" at. Completion and error rates are still published in full
    # in the tables above and in the coverage matrix; the prose states boundaries
    # as design intent. See verdicts.py.
    lines += ["", "### The stories", ""]
    for plat in sorted(present):
        v = VERDICTS.get(plat, {})
        if not v:
            lines += [
                f"**{_disp(plat)}**: _verdict pending (data present; story not yet written)._",
                "",
            ]
            continue
        lines += [
            f"**{_disp(plat)}**. Strengths: {v['strengths']}",
            "",
            f"  Best fit: {v['best_fit']}",
            "",
        ]

    lines += [
        "_Caveats: cost figures carry different confidence tiers (Measured for "
        "BigQuery on-demand, Allocated capacity for Databricks/Redshift/Fabric and "
        "BigQuery reservations, Modeled for ClickHouse/MotherDuck/Snowflake), and "
        "compute sizes follow each engine's run-plan tier rather than being "
        "cost-equalized: the efficiency frontier charts below are the "
        "size-aware view._",
        "",
    ]
    return lines


def _section_benchmark_run_cost(con: duckdb.DuckDBPyConnection) -> list[str]:
    """What it cost to RUN this benchmark per engine — every recorded
    execution (no dedup: superseded re-runs and sweep tiers all spent real
    money), split into the canonical-tier runs the report's outcome sections
    consume vs the size-sweep exploration around them. Reads the raw table on
    purpose. Query-attributed cost only: idle compute between queries, data
    loading, and storage are not in ``cost_usd`` (see confidence tiers above —
    proportional engines allocate capacity-time, so their figures partly
    capture idle; direct/estimated engines' figures do not)."""
    rows = con.execute("""
        SELECT r.platform,
               ROUND(SUM(CAST(r.cost_usd AS DECIMAL(28,12))), 2) AS total_usd,
               COUNT(*) AS execs,
               ROUND(SUM(CAST(r.cost_usd AS DECIMAL(28,12))) FILTER (
                   WHERE c.label IS NULL OR r.compute_label = c.label), 2) AS canonical_usd,
               ROUND(SUM(CAST(r.cost_usd AS DECIMAL(28,12))) FILTER (
                   WHERE c.label IS NOT NULL AND r.compute_label IS NOT NULL
                     AND r.compute_label != c.label), 2) AS sweep_usd,
               ROUND(SUM(CAST(r.cost_usd AS DECIMAL(28,12))) FILTER (WHERE r.run_type = 'warm'), 2) AS warm_usd,
               ROUND(SUM(CAST(r.cost_usd AS DECIMAL(28,12))) FILTER (WHERE r.run_type = 'concurrency'), 2)
                   AS conc_usd,
               -- Every tier actually present, dominant first with the minority's
               -- share. ANY_VALUE could pick the minority tier and make this
               -- column contradict the per-scale confidence table above.
               (SELECT STRING_AGG(
                        CASE WHEN pct >= 50 THEN tier
                             WHEN pct < 1 THEN tier || ' (<1%)'
                             ELSE tier || ' (' || CAST(ROUND(pct) AS INTEGER) || '%)'
                        END, ' + ' ORDER BY pct DESC)
                FROM (SELECT cost_confidence AS tier,
                             100.0 * COUNT(*) / SUM(COUNT(*)) OVER () AS pct
                      FROM main.sql_benchmark_results t
                      WHERE t.platform = r.platform AND t.cost_confidence IS NOT NULL
                      GROUP BY 1)) AS conf
        FROM main.sql_benchmark_results r
        LEFT JOIN _canonical_tier c
          ON r.platform = c.platform AND r.scale_factor = c.scale_factor
         AND c.run_class = CASE WHEN r.run_type = 'concurrency'
                                THEN 'concurrency' ELSE 'warm' END
        GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    if not rows:
        return []
    lines = [
        "## What This Benchmark Cost to Run",
        "",
        "Total query-attributed spend per engine across **every recorded "
        "execution**, including superseded re-runs and all size-sweep tiers, "
        "which the outcome sections deliberately exclude. 'Canonical' is the "
        "designated run-plan tier the report's comparisons consume; 'sweep' is "
        "the exploration around it. Costs exclude idle compute between "
        "queries, data loading, and storage. Confidence lists every tier "
        "present in the engine's rows, dominant first: most rows reconcile to "
        "the vendor's billing source, while the rows that source never "
        "returned (for instance query history a Fabric capacity suspend "
        "discarded before it flushed) keep the inline compute-time × rate "
        "estimate.",
        "",
    ]
    lines += _render_table(
        [
            "Engine",
            "Total",
            "Executions",
            "Canonical tier",
            "Size sweeps",
            "Warm",
            "Concurrency",
            "Confidence",
        ],
        [
            (
                _disp(plat),
                f"${tot:,.2f}",
                f"{ex:,}",
                f"${can or 0:,.2f}",
                f"${sw or 0:,.2f}",
                f"${w or 0:,.2f}",
                f"${cc or 0:,.2f}",
                conf,
            )
            for plat, tot, ex, can, sw, w, cc, conf in rows
        ],
    )
    lines += [""]
    return lines


#: Display names for the stored cost-confidence values, as Part I and the explorer name them.
COST_TIER_LABEL = {"direct": "Measured", "proportional": "Allocated", "estimated": "Modeled"}


def _section_cost_confidence(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Global cost-confidence + spend summary (platform × scale, block-agnostic),
    rendered ONCE instead of duplicated inside each family's cost section. Money is
    $-formatted for consistency with the rest of the report."""
    rows = con.execute(
        """
        SELECT platform, scale_factor, cost_confidence,
               COUNT(*) AS runs,
               SUM(CAST(cost_usd AS DECIMAL(28,12))) AS total_cost,
               SUM(CAST(cost_usd AS DECIMAL(28,12))) / NULLIF(COUNT(cost_usd), 0) AS mean_cost
        FROM bench
        WHERE run_type = 'warm' AND cost_usd IS NOT NULL
        GROUP BY platform, scale_factor, cost_confidence
        ORDER BY platform, scale_factor, cost_confidence
        """
    ).fetchall()
    if not rows:
        return []
    lines = [
        "## Cost Confidence & Spend",
        "",
        "Total and mean warm-run cost per platform × scale, with each platform's "
        "**cost-confidence tier** (Measured, stored as `direct` = the metered per-query "
        "bill; Allocated, `proportional` = list rate × engine time, capped or split "
        "against billed capacity; Modeled, `estimated` = list rate × engine time with "
        "no invoice behind it). Read cross-engine cost gaps in light of the tier: a "
        "Modeled figure carries wider uncertainty than a Measured one. Block-agnostic, "
        "so it appears "
        "once here rather than inside each suite.",
        "",
    ]
    header = ["Engine", "scale", "tier", "runs", "total $", "mean $/run"]
    out = [
        (_disp(p), sf, COST_TIER_LABEL.get(conf, conf), runs, f"${total:,.4f}", f"${mean:,.6f}")
        for p, sf, conf, runs, total, mean in rows
    ]
    lines += _render_table(header, out)
    lines += _cost_caveats(con)
    return lines


def _cost_caveats(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Two properties of the cost data a reader would otherwise misread.

    Both are stated from live counts rather than prose, so they cannot go stale:
    genuinely-zero attributed cost (which looks identical to missing data) and
    cells that carry more than one confidence tier (whose mean silently averages
    measurements of different quality).
    """
    lines: list[str] = []
    zero = con.execute(
        """
        SELECT platform, count(*) AS n
        FROM main.sql_benchmark_results
        WHERE cost_usd = 0 AND error_message IS NULL
        GROUP BY 1 ORDER BY n DESC
        """
    ).fetchall()
    if zero:
        detail = ", ".join(f"{_disp(p)} ({int(n):,})" for p, n in zero)
        lines += [
            "",
            f"_**$0.00 is a measurement, not a gap.** {detail} rows carry a cost of "
            "exactly zero because the engine's billing API attributed no charge to that "
            "query. Snowflake's attribution view, for instance, omits queries that "
            "consumed no billable credits, typically because they were served without "
            "spinning up billable compute. Those rows are kept at zero rather than "
            "voided to NULL, since zero is what the billing source reports. Rows where "
            "cost could not be determined at all are NULL and are excluded from every "
            "cost aggregate._",
        ]
    mixed = con.execute(
        """
        SELECT count(*) FROM (
            SELECT platform, scale_factor, run_type, coalesce(compute_label, '-')
            FROM main.sql_benchmark_results
            WHERE cost_confidence IS NOT NULL
            GROUP BY 1, 2, 3, 4
            HAVING count(DISTINCT cost_confidence) > 1
        )
        """
    ).fetchone()[0]
    if mixed:
        lines += [
            "",
            f"_**{mixed} cells carry more than one cost-confidence tier.** A cell is one "
            "(engine, scale, run type, compute size); where its rows were priced two "
            "different ways, the cell's mean averages measurements of different quality. "
            "This happens when a reconcile pass back-fills authoritative per-query cost "
            "for some rows and not others; the remainder keep the rate-model estimate. "
            "Never compare a mixed cell against a uniformly-`direct` one without "
            "accounting for it._",
        ]
    if lines:
        lines.append("")
    return lines


def _platforms(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute("SELECT DISTINCT platform FROM bench ORDER BY platform").fetchall()
    return [r[0] for r in rows]


def _blocks(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute("SELECT DISTINCT block FROM bench ORDER BY block").fetchall()
    return [r[0] for r in rows]


def _scale_label(con: duckdb.DuckDBPyConnection) -> str:
    """Human label for the scale factor(s) actually present in warm results,
    e.g. ``"SF10"`` or ``"SF10/SF100"``. Avoids hardcoding 'SF1' in titles."""
    rows = con.execute(
        "SELECT DISTINCT scale_factor FROM bench WHERE run_type = 'warm' ORDER BY scale_factor"
    ).fetchall()
    sfs = [str(r[0]) for r in rows]
    return "SF" + "/SF".join(sfs) if sfs else "SF?"


#: A cell completing less than this share of the queries its peers completed at the
#: same (block, scale) has its count published beside the median. Below the bar, a
#: median describes the subset that survived rather than the workload: a median over
#: 3 of 102 queries printed beside engines with 102 is the survivor-set trap this
#: report warns about, in its own tables.
THIN_COMPLETION_SHARE = 0.9


def _completion_counts(
    con: duckdb.DuckDBPyConnection, blocks: tuple[str, ...], view: str
) -> dict[tuple[str, str, int], tuple[int, int]]:
    """``(platform, block, scale) -> (completed queries, peer maximum)``.

    The peer maximum is the most queries any engine completed at that (block, scale),
    which is the honest denominator: it is what the workload was, as measured.
    """
    rows = con.execute(f"""
        SELECT platform, block, scale_factor, COUNT(DISTINCT query_id) AS nq
        FROM {view}
        WHERE run_type = 'warm' AND error_message IS NULL AND {_block_in(blocks)}
        GROUP BY 1, 2, 3
    """).fetchall()
    peak: dict[tuple[str, int], int] = {}
    for _platform, block, scale, nq in rows:
        key = (block, int(scale))
        peak[key] = max(peak.get(key, 0), int(nq))
    return {
        (platform, block, int(scale)): (int(nq), peak[(block, int(scale))])
        for platform, block, scale, nq in rows
    }


def _skipped_cells(con: duckdb.DuckDBPyConnection) -> set[tuple[str, str, int]]:
    """``{(platform, block, scale)}`` recorded as deliberately not measured.

    A recorded skip means *out of scope*, which is a different statement from *thinly
    covered* — and the two must not render the same way. ClickHouse's recorded skips
    cover DSB at 1 TB while TPC-DS runs there; three DSB queries executed anyway, and
    annotating them "(n=3 of 102)" would present a cell nobody intended to measure as
    weak coverage of one they did.
    """
    try:
        # Its callers annotate single-user (warm) tables, so a skip recorded for
        # another run type only (Redshift 8/16 RPU ran the 1 TB grid under load) is
        # not a reason to read a warm cell as out of scope.
        rows = con.execute(
            "SELECT platform, block, scale_factor FROM scale_skips WHERE run_type IN ('*', 'warm')"
        ).fetchall()
    except duckdb.Error:
        return set()
    blocks = [r[0] for r in con.execute("SELECT DISTINCT block FROM bench").fetchall()]
    out: set[tuple[str, str, int]] = set()
    for platform, block, scale in rows:
        for b in blocks if block == "*" else [block]:
            out.add((platform, b, int(scale)))
    return out


def _thin_suffix(
    counts: dict[tuple[str, str, int], tuple[int, int]],
    platform: str,
    block: str,
    scale: int,
    skipped: set[tuple[str, str, int]] | None = None,
) -> str:
    """``" (n=3 of 102)"`` when a cell is thin, ``" (out of scope)"`` when the omission
    was recorded as deliberate, else empty.

    Thin cells are stated rather than suppressed: a missing cell hides that the engine
    was measured at all, and the completion count is itself a finding. A *skipped* cell
    is the opposite case — measurement was never intended, so a completion ratio
    misrepresents it.
    """
    key = (platform, block, int(scale))
    if skipped and key in skipped:
        return " (out of scope)"
    entry = counts.get(key)
    if entry is None:
        return ""
    nq, peer = entry
    if peer and nq < peer * THIN_COMPLETION_SHARE:
        return f" (n={nq} of {peer})"
    return ""


def _primary_metric_note(con: duckdb.DuckDBPyConnection) -> list[str]:
    """A footnote naming platforms EXCLUDED from latency because they report no
    server-side time in warm runs (server-side is the sole latency metric — there
    is no wall-clock fallback). Empty list when every platform reports server time
    (the expected state after `benchmark reconcile`)."""
    rows = con.execute(
        """
        SELECT platform
        FROM bench
        WHERE run_type = 'warm'
        GROUP BY platform
        HAVING COUNT(server_time_ms) = 0
        ORDER BY platform
        """
    ).fetchall()
    plats = [r[0] for r in rows]
    if not plats:
        return [
            "_Latency is **server-side execution time** from each engine's query history "
            "(wall-clock / network RTT is excluded as an unfair cross-engine comparator)._",
            "",
        ]
    return [
        "_Latency is **server-side execution time** only (no wall-clock fallback). "
        f"**{', '.join(_disp(p) for p in plats)}** report no server-side time in warm runs and are therefore "
        "**excluded** from the latency aggregates above; run `benchmark reconcile` to "
        "back-fill their server time (e.g. MotherDuck from `MD_QUERY_HISTORY()`)._",
        "",
    ]


# ---------------------------------------------------------------------------
# Section: Coverage
# ---------------------------------------------------------------------------


#: How the skips table names a skip's run type: the report's own terms.
SKIP_RUN_TYPE_LABELS = {
    "*": "all",
    "warm": "single-user",
    "cold": "cold",
    "concurrency": "under load",
    "sustained": "sustained",
}


def _section_skips(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Engines, suites and compute sizes recorded as deliberately not measured.

    An engine simply absent from the coverage table reads as an oversight. Recording
    the skip and its reason makes the decision part of the published record — the
    difference between "we chose not to measure this" and "we forgot". Rendered at the
    granularity the skips actually carry (suite and compute size, not only scale),
    because collapsing them makes two distinct decisions look like one duplicate row.
    """
    try:
        rows = con.execute(
            "SELECT platform, scale_factor, block, compute_label, run_type, reason"
            " FROM scale_skips ORDER BY platform, scale_factor, block, compute_label, run_type"
        ).fetchall()
    except duckdb.Error:
        return []
    if not rows:
        return []
    scales = {int(r[0]) for r in con.execute("SELECT DISTINCT scale_factor FROM bench").fetchall()}

    # Collapse only a genuine all-scales skip of the same (block, size, run type,
    # reason); every other distinction is a separate decision and keeps its own row.
    grouped: dict[tuple[str, str, str, str, str], set[int]] = {}
    for platform, scale, block, label, run_type, reason in rows:
        grouped.setdefault((platform, block, label, run_type, reason), set()).add(int(scale))
    out = []
    for (platform, block, label, run_type, reason), at in sorted(grouped.items()):
        where = "every scale" if at >= scales else ", ".join(f"SF{s}" for s in sorted(at))
        out.append(
            (
                _disp(platform),
                where,
                "all suites" if block == "*" else block,
                "all sizes" if label == "*" else label,
                SKIP_RUN_TYPE_LABELS.get(run_type, run_type),
                reason,
            )
        )
    return [
        "### Deliberately not measured",
        "",
        "Recorded skips, so an absence in the table above can be told apart from an "
        "oversight. A skip can be narrower than a whole scale (one suite, one compute "
        "size, or single-user only) and is shown at whatever granularity it was decided.",
        "",
        *_render_table(["Engine", "Scales", "Suites", "Compute", "Runs", "Reason"], out),
    ]


def _section_coverage(con: duckdb.DuckDBPyConnection) -> list[str]:
    lines = [
        "## Coverage",
        "",
        "Coverage counts how many distinct queries were executed and how many individual "
        "runs were recorded, grouped by platform, benchmark block, scale factor, and run "
        "type. The **queries** column is the number of unique query IDs that produced at "
        "least one result row. **runs** is the total number of executions (e.g., 3 measured "
        "warm runs per query).",
        "",
        "A platform with fewer queries than expected (99 for TPC-DS, 102 for DSB, 43 for "
        "ClickBench) indicates queries that either timed out or errored on every attempt.",
        "",
        "_Correctness is **not** reported here. The benchmark runners measure latency and "
        "cost only; result correctness is verified separately by `validate-sf1` (a tolerant, "
        "column-aligned comparison against the DuckDB ground truth), which is the single "
        "correctness gate._",
        "",
    ]
    lines += _section_skips(con)
    rows = con.execute(
        """
        -- bench_coverage, not bench: coverage is bookkeeping, so it must show
        -- measurements the comparison policy excludes (DNF cells, ClickHouse
        -- multi-replica warm) rather than letting them read as never-ran.
        SELECT platform, block, scale_factor, run_type,
               COUNT(DISTINCT query_id) AS queries,
               COUNT(*) AS runs
        FROM bench_coverage
        GROUP BY platform, block, scale_factor, run_type
        ORDER BY platform, block, scale_factor, run_type
    """
    ).fetchall()
    cells = {
        (f"{_disp(platform)} · {block}", f"SF{sf} {run_type}"): f"{queries} q / {runs} runs"
        for platform, block, sf, run_type, queries, runs in rows
    }
    lines += _render_pivot("platform · block", cells, col_order=_sf_order({c for _r, c in cells}))
    return lines


# ---------------------------------------------------------------------------
# Section: Latency
# ---------------------------------------------------------------------------


def _section_latency(
    con: duckdb.DuckDBPyConnection, assets: Path, blocks: tuple[str, ...], suffix: str
) -> list[str]:
    bf = _block_in(blocks)
    lines = ["## Latency Analysis", ""]

    lines += [
        "Aggregates below cover **warm** runs (the canonical performance numbers). "
        "Cold and concurrency runs are summarized separately so they don't inflate the "
        "warm percentiles.",
        "",
        "### Aggregate by platform + block (warm, server-side time)",
        "",
        (
            "One row per engine × compute size, ordered by provisioned capacity: "
            "ClickBench is a single fixed ~100 GB dataset, so size is its only "
            "scaling axis. (Full stats, mean/stdev/min/max, stay queryable in the "
            "results DB.)"
            if _is_clickbench(blocks)
            else "Cells are `p50 / p95` ms; a row reads as the latency trajectory across "
            "scale factors. (Full stats, mean/stdev/min/max, stay queryable in the "
            "results DB.)"
        ),
        "",
    ]
    # ClickBench pivots on compute size, not scale — one dataset, so size is the
    # only real axis (see _is_clickbench). Decision support keeps scale columns.
    cb = _is_clickbench(blocks)
    view, dim = _bench_view(blocks), "compute_label" if cb else _scale_expr(blocks)
    agg_rows = con.execute(f"""
        SELECT
            platform,
            block,
            {dim} AS dim,
            ROUND(MEDIAN({_PRIMARY_MS}), 0) AS p50_ms,
            ROUND(APPROX_QUANTILE({_PRIMARY_MS}, 0.95), 0) AS p95_ms
        FROM {view}
        WHERE error_message IS NULL AND run_type = 'warm' AND {bf}
        GROUP BY platform, block, dim
        ORDER BY platform, block, dim
    """).fetchall()
    if cb:
        # A pivot needs a shared column axis, and compute labels don't align across
        # engines ("8 RPU" vs "X-Small" vs "3x120GB"), so this reads as a flat table
        # ordered by engine then provisioned capacity.
        lines += _render_table(
            ["Engine", "Compute", "$/hr", "p50 / p95"],
            [
                (
                    _disp(platform),
                    label,
                    f"${usd:g}" if (usd := effective_usd_per_hour(platform, label)) else "—",
                    f"{p50:,.0f} / {p95:,.0f} ms" if p50 is not None else "—",
                )
                for platform, _block, label, p50, p95 in sorted(
                    (r for r in agg_rows if r[2] is not None),
                    key=lambda r: (r[0], _size_sort_key(r[0], r[2])),
                )
            ],
        )
        lines += [
            "_Rows are engine × compute size. ClickBench is one fixed ~100 GB dataset, so "
            "size is the only scaling axis it has and its `scale_factor` tag is pooled._",
            "",
        ]
    else:
        counts = _completion_counts(con, blocks, view)
        skipped = _skipped_cells(con)
        cells = {
            (f"{_disp(platform)} · {block}", f"SF{dim_val}"): (
                f"{p50:,.0f} / {p95:,.0f} ms"
                + _thin_suffix(counts, platform, block, int(dim_val), skipped)
                if p50 is not None
                else "—"
            )
            for platform, block, dim_val, p50, p95 in agg_rows
            if dim_val is not None
        }
        lines += _render_pivot(
            "platform · block", cells, col_order=_sf_order({c for _r, c in cells})
        )
        lines += [
            "_A cell reading `(n=X of Y)` completed X queries where the best-covered "
            "engine at that scale completed Y. Its median describes the subset that "
            "survived, not the workload, and must not be compared against a full cell._",
            "",
        ]
    lines += _primary_metric_note(con)

    # Per run-type breakdown — ONLY when a non-warm run type exists. With warm-only
    # data this table just restates the warm aggregate above, so it's suppressed.
    run_types = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT run_type FROM {view} WHERE {bf} ORDER BY run_type"
        ).fetchall()
    ]
    if any(rt != "warm" for rt in run_types):
        lines += [
            "### Latency by run type (server-side time)",
            "",
            "Warm = cache-warm steady state. Cold = caches/warehouse flushed between "
            "queries. Concurrency = many queries in flight at once. **errors** counts "
            "runs that failed or timed out (excluded from the percentiles).",
            "",
        ]
        rt_rows = con.execute(f"""
            SELECT platform, block, {dim} AS dim, run_type,
                   SUM(CASE WHEN error_message IS NOT NULL THEN 1 ELSE 0 END) AS errors,
                   ROUND(MEDIAN({_PRIMARY_MS}) FILTER (WHERE error_message IS NULL), 0) AS p50_ms,
                   ROUND(APPROX_QUANTILE({_PRIMARY_MS}, 0.95)
                         FILTER (WHERE error_message IS NULL), 0) AS p95_ms
            FROM {view}
            WHERE {bf}
            GROUP BY platform, block, dim, run_type
            ORDER BY platform, block, dim, run_type
        """).fetchall()
        rt_cells = {}
        # Row label -> raw sort key: the label carries the display name, so the
        # capacity ordering must be captured here, where the raw id is in hand.
        rt_row_keys: dict[str, tuple] = {}
        rt_counts = _completion_counts(con, blocks, view)
        rt_skipped = _skipped_cells(con)
        for platform, block, dim_val, run_type, errors, p50, p95 in rt_rows:
            if dim_val is None:
                continue
            val = f"{p50:,.0f} / {p95:,.0f} ms" if p50 is not None else "—"
            if errors:
                val += f" ({errors} err)"
            # ClickBench rows are engine × compute size; run type stays the column.
            row = f"{_disp(platform)} · {dim_val}" if cb else f"{_disp(platform)} · {block}"
            col = run_type if cb else f"SF{dim_val} {run_type}"
            if not cb and run_type == "warm":
                val += _thin_suffix(rt_counts, platform, block, int(dim_val), rt_skipped)
            rt_cells[(row, col)] = val
            rt_row_keys[row] = (platform, _size_sort_key(platform, str(dim_val)))
        row_order = sorted(rt_row_keys, key=lambda r: rt_row_keys[r]) if cb else None
        lines += _render_pivot(
            "platform · compute" if cb else "platform · block",
            rt_cells,
            row_order=row_order,
            col_order=sorted({c for _r, c in rt_cells})
            if cb
            else _sf_order({c for _r, c in rt_cells}),
        )

    # Concurrency scaling: how latency / error rate degrades as parallelism rises.
    # Size-aware: a grid swept across warehouse bands (--compute-size) must keep
    # one series per band instead of colliding at the same (scale, level). The
    # main `bench` view omits compute_label from its key, so read the size-aware
    # `results_latest` here.
    layout = _compute_layout(con)
    conc_rows = con.execute(
        f"""
        -- `scale_key`, not `AS scale_factor`: SQL binds GROUP BY against the FROM
        -- clause first, so aliasing an expression to an existing column name leaves
        -- the grouping on the BASE column while the projection shows the expression.
        -- For ClickBench that yields three groups (scale tags 1/10/100) all
        -- projecting 100, which collide on one pivot key with an arbitrary winner.
        SELECT platform, {_scale_expr(blocks)} AS scale_key, compute_label,
               concurrency AS level,
               ROUND(
                   SUM(CASE WHEN error_message IS NOT NULL THEN 1 ELSE 0 END) * 100.0
                   / COUNT(*), 0) AS error_pct,
               ROUND(MEDIAN({_CONCURRENCY_MS}) FILTER (WHERE error_message IS NULL), 0)
                     AS p50_ok_ms
        FROM concurrency_results
        WHERE concurrency IS NOT NULL AND {bf} AND failure_class IS DISTINCT FROM 'harness'
        GROUP BY platform, scale_key, compute_label, concurrency
        -- compute_label is IN the ORDER BY: without it, two groups that render to
        -- the same pivot key resolve in arbitrary order and the published number
        -- changes between runs.
        ORDER BY platform, scale_key, compute_label, concurrency
        """
    ).fetchall()
    if conc_rows:
        lines += [
            "### Concurrency scaling",
            "",
            (
                "Rows are platform × compute band (ClickBench is one dataset, so there "
                "is no scale dimension); columns are concurrency levels. "
                if cb
                else "Rows are platform × compute band × scale; columns are concurrency levels. "
            ),
            "Cell is `p50 engine ms (err%)` for successful runs at that level: the "
            "engine's time on a query, its queue wait included and the network "
            "excluded (three engines report execution only, and their queue time is "
            "added back). Read the two together: a low p50 with a high err% means "
            "part of the burst did not complete (timed out, dropped, or errored) "
            "while what did complete was fast, so the band saturated. Swept across "
            "warehouse bands via `--compute-size`.",
            "",
        ]
        capped = _capped_keys(con, blocks=blocks)
        conc_cells: dict[tuple[str, str], str] = {}
        row_keys: dict[str, tuple[int, tuple[int, float, str]]] = {}
        for platform, sf, lbl, level, err_pct, p50 in conc_rows:
            compute = _compute_row(lbl, layout.get((platform, int(sf))))
            row = (
                f"{_disp(platform)} · {compute}"
                if cb
                else f"{_disp(platform)} · {compute} · SF{sf}"
            )
            if (platform, int(sf), lbl) in capped:
                # Withheld, not omitted: the reader sees the gap and where it is.
                cell = "withheld"
            else:
                cell = f"{p50:,.0f} ms" if p50 is not None else "—"
                if err_pct:
                    cell += f" ({err_pct:.0f}% err)"
            conc_cells[(row, f"L{level}")] = cell
            # Order rows by scale then capacity ($/hr) of the band.
            row_keys[row] = (int(sf), _size_sort_key(platform, compute))
        col_order = sorted({c for _r, c in conc_cells}, key=lambda c: int(c.removeprefix("L")))
        lines += _render_pivot(
            "platform · band" if cb else "platform · band · scale",
            conc_cells,
            row_order=sorted(row_keys, key=lambda r: row_keys[r]),
            col_order=col_order,
        )
        lines += _withheld_cells_note(con, blocks)
        publishable_rows = [r for r in conc_rows if (r[0], int(r[1]), r[2]) not in capped]
        if _chart_concurrency(publishable_rows, layout, assets, suffix):
            lines += [
                "",
                "Concurrency scaling: latency and load-shedding vs concurrency level. "
                "Columns are scale factors, top row is median engine time "
                "(queue + execution, network excluded) of successful runs, bottom row "
                "is error/timeout "
                "rate; one line per "
                "warehouse band. A flat latency line with a rising error rate means the "
                "band **saturated** (shedding load rather than slowing down); a rising "
                "latency line with ~0 errors means it absorbed the load by queueing:",
                "",
                f"![Concurrency scaling](assets/concurrency_{suffix}.png)",
                "",
            ]

    if _chart_latency_distribution(con, assets, blocks, suffix):
        lines += [f"![Latency distribution](assets/latency_distribution_{suffix}.png)", ""]

    # Pattern speed radar is TPC-DS/DSB-only (pattern tags don't cover ClickBench).
    if "tpcds" in blocks and _chart_pattern_radar(con, assets, blocks, suffix):
        lines += [
            f"![Pattern speed profile (bigger = faster)](assets/pattern_radar_{suffix}.png)",
            "",
        ]

    return lines


def _chart_concurrency(
    conc_rows: list[tuple], layout: dict[tuple[str, int], str], assets: Path, suffix: str
) -> bool:
    """Concurrency-scaling chart across all three dimensions: latency (top) and
    error rate (bottom) vs concurrency level (x), one column per scale factor, one
    line per warehouse band. Returns False if nothing to plot.

    ``conc_rows`` is the size-aware aggregate the section already built:
    (platform, scale, compute_label, level, error_pct, p50_ok_ms).
    """
    plotly = _plotly()
    if plotly is None:
        return False
    go, make_subplots = plotly

    # (platform, band, scale) -> {level: (p50, err_pct)}
    series: dict[tuple[str, str, int], dict[int, tuple[float | None, float | None]]] = {}
    for platform, sf, lbl, level, err_pct, p50 in conc_rows:
        band = _compute_row(lbl, layout.get((platform, int(sf))))
        if band == UNLABELLED_COMPUTE:
            # A curve of unknown compute size answers no sizing question on a
            # size-keyed chart. The rows stay in
            # the concurrency table, whose unlabelled note accounts for them.
            continue
        series.setdefault((platform, band, int(sf)), {})[int(level)] = (p50, err_pct)
    if not series:
        return False

    scales = sorted({sf for _p, _b, sf in series})
    # Stable colour per (platform, band) so a band is the same colour across scales.
    bands = sorted(
        {(p, b) for p, b, _sf in series},
        key=lambda pb: (pb[0], _size_sort_key(pb[0], pb[1])),
    )
    color = {pb: _CHART_COLORS[i % len(_CHART_COLORS)] for i, pb in enumerate(bands)}

    fig = make_subplots(
        rows=2,
        cols=len(scales),
        shared_xaxes=True,
        subplot_titles=[f"SF{sf}" for sf in scales] + [f"SF{sf} errors" for sf in scales],
        vertical_spacing=0.12,
    )
    legend_done: set[tuple[str, str]] = set()
    for c, sf in enumerate(scales, start=1):
        for platform, band in bands:
            pts = series.get((platform, band, sf))
            if not pts:
                continue
            levels = sorted(pts)
            name = f"{_disp(platform)} · {band}"
            fig.add_trace(
                go.Scatter(
                    x=levels,
                    y=[pts[lv][0] for lv in levels],
                    mode="lines+markers",
                    name=name,
                    legendgroup=name,
                    showlegend=(platform, band) not in legend_done,
                    line={"color": color[(platform, band)]},
                    hovertemplate="level %{x}: %{y:.0f} ms engine time<extra>" + name + "</extra>",
                ),
                row=1,
                col=c,
            )
            fig.add_trace(
                go.Scatter(
                    x=levels,
                    y=[pts[lv][1] for lv in levels],
                    mode="lines+markers",
                    name=name,
                    legendgroup=name,
                    showlegend=False,
                    line={"color": color[(platform, band)]},
                    hovertemplate="level %{x}: %{y:.0f}% err<extra>" + name + "</extra>",
                ),
                row=2,
                col=c,
            )
            legend_done.add((platform, band))
    # Pin x ticks to the actual concurrency levels (log spacing, but auto-ticks
    # render a confusing 1/2/5/10/2/5 for a 1,5,10,20,50,100 series).
    all_levels = sorted({lv for pts in series.values() for lv in pts})
    fig.update_xaxes(
        type="log",
        tickvals=all_levels,
        ticktext=[str(lv) for lv in all_levels],
    )
    for c in range(1, len(scales) + 1):
        fig.update_xaxes(title_text="concurrency level", row=2, col=c)
    fig.update_yaxes(title_text="median engine ms", rangemode="tozero", row=1, col=1)
    fig.update_yaxes(title_text="error / timeout %", rangemode="tozero", row=2, col=1)
    fig.update_layout(
        title_text="Concurrency scaling: engine-time latency (queue wait included) + "
        "load-shedding vs concurrency, by band × scale",
        width=520 * len(scales),
        height=760,
    )
    _write_chart(fig, assets, f"concurrency_{suffix}")
    return True


def _chart_latency_distribution(
    con: duckdb.DuckDBPyConnection, assets: Path, blocks: tuple[str, ...], suffix: str
) -> bool:
    plotly = _plotly()
    if plotly is None:
        return False
    go, make_subplots = plotly

    rows = con.execute(f"""
        SELECT platform, block, scale_factor, query_id, MEDIAN({_PRIMARY_MS}) AS median_ms
        FROM bench
        WHERE error_message IS NULL AND run_type = 'warm' AND {_block_in(blocks)}
        GROUP BY platform, block, scale_factor, query_id
        -- ORDER BY is load-bearing: DuckDB's parallel GROUP BY returns rows in a
        -- nondeterministic order, which would shuffle the per-query points inside
        -- the rendered chart JSON and make every rebuild rewrite this asset.
        ORDER BY platform, block, scale_factor, query_id
    """).fetchall()
    if not rows:
        return False

    platforms = sorted({r[0] for r in rows})
    block_list = sorted({r[1] for r in rows})
    scales = sorted({r[2] for r in rows})
    color = {p: _CHART_COLORS[i % len(_CHART_COLORS)] for i, p in enumerate(platforms)}
    # (block, scale, platform) -> [(query_id, median)] for the boxplot + hover.
    medians: dict[tuple[str, int, str], list[tuple[str, float]]] = {}
    for platform, block, sf, qid, med in rows:
        if med is not None:
            medians.setdefault((block, sf, platform), []).append((qid, med))

    # shared_yaxes=True: panels for the same scale factor (row) share a y-axis,
    # so latency is visually comparable across blocks at that scale.
    fig = make_subplots(
        rows=len(scales),
        cols=len(block_list),
        shared_yaxes=True,
        subplot_titles=[f"{b.upper()} — SF{sf}" for sf in scales for b in block_list],
    )
    legend_done: set[str] = set()
    for r, sf in enumerate(scales, start=1):
        for c, block in enumerate(block_list, start=1):
            for p in platforms:
                pts = medians.get((block, sf, p))
                if not pts:
                    continue
                fig.add_trace(
                    go.Box(
                        y=[m for _q, m in pts],
                        text=[q for q, _m in pts],
                        name=_disp(p),
                        legendgroup=p,
                        showlegend=p not in legend_done,
                        marker_color=color[p],
                        boxpoints="outliers",
                        hovertemplate="%{text}: %{y:.0f} ms<extra>" + _disp(p) + "</extra>",
                    ),
                    row=r,
                    col=c,
                )
                legend_done.add(p)
    fig.update_yaxes(type="log")
    for r in range(1, len(scales) + 1):
        fig.update_yaxes(title_text="Server-side latency (ms)", row=r, col=1)
    fig.update_layout(
        title_text="Query Latency Distribution (server-side time, median per query)",
        width=600 * len(block_list),
        height=500 * len(scales),
    )
    _write_chart(fig, assets, f"latency_distribution_{suffix}")
    return True


# Radar radial axis spans this many orders of magnitude (fastest → this-many-
# decades-slower maps to rim → center). 2 = a 100× window, enough to keep even a
# 10×-dominated SF1 panel legible without clipping realistic gaps.
RADAR_DECADES = 2


def _chart_pattern_radar(
    con: duckdb.DuckDBPyConnection, assets: Path, blocks: tuple[str, ...], suffix: str
) -> bool:
    plotly = _plotly()
    if plotly is None:
        return False
    go, make_subplots = plotly

    from statistics import median as _median

    platforms = _platforms(con)
    if len(platforms) < 2:
        return False

    try:
        from ..queries.registry import QueryRegistry

        registry = QueryRegistry()
    except Exception:
        return False

    tag_rows: list[tuple[str, str]] = []
    for q in registry.all():
        for tag in q.patterns:
            tag_rows.append((q.id, tag))
    if not tag_rows:
        return False

    con.execute("CREATE OR REPLACE TEMP TABLE _radar_patterns (query_id VARCHAR, pattern VARCHAR)")
    con.executemany("INSERT INTO _radar_patterns VALUES (?, ?)", tag_rows)
    rows = con.execute(f"""
        SELECT r.platform, r.block, r.scale_factor, p.pattern,
               MEDIAN({_PRIMARY_MS}) AS median_ms
        FROM bench r
        JOIN _radar_patterns p USING (query_id)
        WHERE r.error_message IS NULL AND r.run_type = 'warm' AND {_block_in(blocks)}
        GROUP BY r.platform, r.block, r.scale_factor, p.pattern
    """).fetchall()
    con.execute("DROP TABLE IF EXISTS _radar_patterns")
    if not rows:
        return False

    scales = sorted({r[2] for r in rows})
    block_list = sorted({r[1] for r in rows})
    # Per-block panel: (scale, block) -> {platform -> {pattern -> median}}.
    block_panel: dict[tuple[int, str], dict[str, dict[str, float]]] = {}
    # "All blocks" panel: median across blocks -> (scale) -> {platform -> {pattern -> [medians]}}.
    all_acc: dict[tuple[int, str, str], list[float]] = {}
    for platform, block, sf, pattern, med in rows:
        if med is None:
            continue
        block_panel.setdefault((sf, block), {}).setdefault(platform, {})[pattern] = med
        all_acc.setdefault((sf, platform, pattern), []).append(med)
    all_panel: dict[int, dict[str, dict[str, float]]] = {}
    for (sf, platform, pattern), vals in all_acc.items():
        all_panel.setdefault(sf, {}).setdefault(platform, {})[pattern] = _median(vals)

    import math

    def _normalize(
        panel: dict[str, dict[str, float]],
    ) -> tuple[dict[str, dict[str, float]], list[str]]:
        """Relative speed per pattern on a **log** scale: fastest platform = 1.0
        (rim), and each ``1/RADAR_DECADES`` step inward is one order of magnitude
        slower (``norm = 1 + log10(fastest/this) / RADAR_DECADES``, clamped to
        [0,1]). A plain ratio collapses every slower engine toward the center as
        soon as one engine dominates (common at SF1, where the fastest is ~10× the
        rest); the log spread keeps them legible. Bigger polygon = faster."""
        patterns = sorted({pat for pv in panel.values() for pat in pv})
        fastest = {}
        for pat in patterns:
            vals = [pv[pat] for pv in panel.values() if pv.get(pat, 0.0) > 0]
            fastest[pat] = min(vals) if vals else 0.0

        def _speed(this: float, fast: float) -> float:
            if this <= 0 or fast <= 0:
                return 0.0
            return max(0.0, min(1.0, 1.0 + math.log10(fast / this) / RADAR_DECADES))

        norm = {
            p: {pat: _speed(pv.get(pat, 0.0), fastest[pat]) for pat in patterns}
            for p, pv in panel.items()
        }
        return norm, patterns

    col_labels = [*block_list, "All blocks"]
    fig = make_subplots(
        rows=len(scales),
        cols=len(col_labels),
        specs=[[{"type": "polar"}] * len(col_labels) for _ in scales],
        subplot_titles=[f"{lbl.upper()} — SF{sf}" for sf in scales for lbl in col_labels],
    )
    color = {p: _CHART_COLORS[i % len(_CHART_COLORS)] for i, p in enumerate(platforms)}
    drew_any = False
    legend_done: set[str] = set()
    for r, sf in enumerate(scales, start=1):
        for c, label in enumerate(col_labels, start=1):
            panel = (
                all_panel.get(sf, {}) if label == "All blocks" else block_panel.get((sf, label), {})
            )
            if not panel:
                continue
            norm, patterns = _normalize(panel)
            if len(patterns) < 3:
                continue
            for p in platforms:
                vals = [norm.get(p, {}).get(pat, 0.0) for pat in patterns]
                # Recover the "×slower than fastest" factor from the log norm for hover.
                factor = [10 ** ((1.0 - v) * RADAR_DECADES) for v in vals]
                fig.add_trace(
                    go.Scatterpolar(
                        # Close the polygon by repeating the first vertex.
                        r=[*vals, vals[0]],
                        theta=[*patterns, patterns[0]],
                        customdata=[*factor, factor[0]],
                        mode="lines+markers",
                        # No fill — overlapping translucent polygons muddy the
                        # read when 3 engines share a panel; outlines stay legible.
                        name=_disp(p),
                        legendgroup=p,
                        showlegend=p not in legend_done,
                        line={"color": color[p], "width": 1.6},
                        marker={"size": 3},
                        opacity=0.9,
                        hovertemplate="%{theta}: %{customdata:.1f}× vs fastest"
                        "<extra>" + _disp(p) + "</extra>",
                    ),
                    row=r,
                    col=c,
                )
                legend_done.add(p)
            drew_any = True

    if not drew_any:
        return False
    # Radial ticks read as speed multiples (log): rim = fastest, each 1/DECADES
    # step inward = 10× slower.
    tickvals = [i / RADAR_DECADES for i in range(RADAR_DECADES + 1)]
    ticktext = [
        "fastest" if v == 1.0 else f"{10 ** ((1.0 - v) * RADAR_DECADES):g}×" for v in tickvals
    ]
    fig.update_polars(
        radialaxis={
            "range": [0, 1.05],
            "tickvals": tickvals,
            "ticktext": ticktext,
            "tickfont": {"size": 8},
        }
    )
    fig.update_layout(
        title_text=(
            "Pattern speed profile — speed relative to fastest per pattern, "
            f"log scale ({RADAR_DECADES} decades; bigger = faster; server-side time, warm)"
        ),
        width=450 * len(col_labels),
        height=450 * len(scales),
        legend={"orientation": "h", "yanchor": "top", "y": -0.05},
    )
    _write_chart(fig, assets, f"pattern_radar_{suffix}")
    return True


# ---------------------------------------------------------------------------
# Section: Cost
# ---------------------------------------------------------------------------


def _section_cost(
    con: duckdb.DuckDBPyConnection, assets: Path, blocks: tuple[str, ...], suffix: str
) -> list[str]:
    bf = _block_in(blocks)
    lines = ["## Cost Analysis", ""]

    # Per-platform × scale cost-confidence + totals live once in the global
    # "Cost Confidence & Spend" section (block-agnostic) — not duplicated per family.
    lines += [
        "### Aggregate cost by platform + block",
        "",
        "Cells are `total $ (mean $/run)` for warm, successful runs; `$0.0000` "
        "means cost isn't captured yet (e.g. Snowflake before `reconcile`).",
        "",
    ]
    cb = _is_clickbench(blocks)
    view, dim = _bench_view(blocks), "compute_label" if cb else _scale_expr(blocks)
    cost_rows = con.execute(f"""
        SELECT
            platform,
            block,
            {dim} AS dim,
            ROUND(SUM(CAST(cost_usd AS DECIMAL(28,12))), 4) AS total_cost,
            ROUND(SUM(CAST(cost_usd AS DECIMAL(28,12))) / NULLIF(COUNT(cost_usd), 0), 6) AS mean_per_run
        FROM {view}
        WHERE cost_usd IS NOT NULL AND error_message IS NULL AND run_type = 'warm' AND {bf}
        GROUP BY platform, block, dim
        ORDER BY platform, block, dim
    """).fetchall()
    if cb:
        # Engine × compute size, ordered by capacity — the per-query cost of a
        # single fixed dataset at each size, which is the ClickBench cost question.
        lines += _render_table(
            ["Engine", "Compute", "$/hr", "Total $", "Mean $/run"],
            [
                (
                    _disp(platform),
                    label,
                    f"${usd:g}" if (usd := effective_usd_per_hour(platform, label)) else "—",
                    f"${total:,.4f}",
                    f"${mean:,.6f}",
                )
                for platform, _block, label, total, mean in sorted(
                    (r for r in cost_rows if r[2] is not None and r[3] is not None),
                    key=lambda r: (r[0], _size_sort_key(r[0], r[2])),
                )
            ],
        )
    else:
        cost_cells = {
            (f"{_disp(platform)} · {block}", f"SF{dim_val}"): f"${total:,.4f} (${mean:,.6f}/run)"
            for platform, block, dim_val, total, mean in cost_rows
            if total is not None and dim_val is not None
        }
        lines += _render_pivot(
            "platform · block", cost_cells, col_order=_sf_order({c for _r, c in cost_cells})
        )

    if _chart_cost_comparison(con, assets, blocks, suffix):
        lines += [f"![Cost per query](assets/cost_per_query_{suffix}.png)", ""]

    return lines


def _chart_cost_comparison(
    con: duckdb.DuckDBPyConnection, assets: Path, blocks: tuple[str, ...], suffix: str
) -> bool:
    plotly = _plotly()
    if plotly is None:
        return False
    go, make_subplots = plotly

    platforms = _platforms(con)
    if len(platforms) < 2:
        return False

    rows = con.execute(f"""
        SELECT platform, block, scale_factor, ROUND(SUM(CAST(cost_usd AS DECIMAL(28,12))), 6) AS total_cost
        FROM bench
        WHERE cost_usd IS NOT NULL AND error_message IS NULL AND run_type = 'warm'
              AND {_block_in(blocks)}
        GROUP BY platform, block, scale_factor
    """).fetchall()
    if not rows:
        return False

    platforms = sorted({r[0] for r in rows})
    block_list = sorted({r[1] for r in rows})
    scales = sorted({r[2] for r in rows})
    cost = {(p, b, sf): (tc or 0.0) for p, b, sf, tc in rows}

    # Row-wise: one row per scale factor (stacked), so the per-block cost bars
    # line up vertically across scales.
    fig = make_subplots(
        rows=len(scales),
        cols=1,
        subplot_titles=[f"Total query cost by block — SF{sf} (warm)" for sf in scales],
    )
    for r, sf in enumerate(scales, start=1):
        for i, p in enumerate(platforms):
            fig.add_trace(
                go.Bar(
                    x=[b.upper() for b in block_list],
                    y=[cost.get((p, b, sf), 0.0) for b in block_list],
                    name=_disp(p),
                    legendgroup=p,
                    showlegend=r == 1,
                    marker_color=_CHART_COLORS[i % len(_CHART_COLORS)],
                    opacity=0.8,
                    hovertemplate="%{x}: $%{y:.4f}<extra>" + _disp(p) + "</extra>",
                ),
                row=r,
                col=1,
            )
        fig.update_yaxes(title_text="Total cost (USD)", row=r, col=1)
    fig.update_layout(barmode="group", width=800, height=400 * len(scales))
    _write_chart(fig, assets, f"cost_per_query_{suffix}")
    return True


# ---------------------------------------------------------------------------
# Section: Skew impact (TPC-DS vs DSB)
# ---------------------------------------------------------------------------


def _section_skew(con: duckdb.DuckDBPyConnection, assets: Path) -> list[str]:
    from .verifiers import returns_qnums

    returns = sorted(returns_qnums())
    returns_list = ", ".join(str(q) for q in returns)
    lines = [
        "## Skew Impact: TPC-DS vs DSB",
        "",
        "DSB uses the same schema as TPC-DS but with skewed data distributions. "
        "Queries 1–99 are identical SQL, so per-query latency ratios isolate the "
        "engine's sensitivity to non-uniform data, except on the "
        f"{len(returns)} queries that touch a `*_returns` table, which DSB also "
        "makes substantially larger (store 2.5×, web 2×, catalog 1.5×). Those "
        "pairs mix a cardinality change into the distribution change, so they are "
        "reported as their own column rather than pooled.",
        "",
    ]

    skew_rows = con.execute(f"""
        WITH per_q AS (
            SELECT platform, scale_factor, block,
                   CAST(REGEXP_EXTRACT(query_id, '(\\d+)$', 1) AS INTEGER) AS qnum,
                   MEDIAN({_PRIMARY_MS}) AS ms
            FROM bench
            WHERE error_message IS NULL AND run_type = 'warm'
              AND block IN ('tpcds', 'dsb') AND {_PRIMARY_MS} > 0
            GROUP BY 1, 2, 3, 4
        ),
        pairs AS (
            SELECT t.platform, t.scale_factor, t.qnum,
                   d.ms / t.ms AS ratio,
                   t.qnum IN ({returns_list}) AS is_returns
            FROM per_q t
            JOIN per_q d USING (platform, scale_factor, qnum)
            WHERE t.block = 'tpcds' AND d.block = 'dsb'
              AND t.qnum <= 99 AND t.ms > 0
        )
        SELECT platform, scale_factor,
               ROUND(MEDIAN(ratio) FILTER (WHERE NOT is_returns), 2) AS clean_median,
               COUNT(*) FILTER (WHERE NOT is_returns) AS clean_n,
               ROUND(MEDIAN(ratio) FILTER (WHERE is_returns), 2) AS returns_median,
               COUNT(*) FILTER (WHERE is_returns) AS returns_n
        FROM pairs
        GROUP BY 1, 2
        ORDER BY 1, 2
    """).fetchall()

    lines += ["### Aggregate skew impact", ""]
    if not skew_rows:
        lines += [
            "_No skew comparison yet: it requires both TPC-DS and DSB results at the same "
            "scale. (DSB not yet benchmarked.)_",
            "",
        ]
    else:
        # Median of per-query DSB/TPC-DS ratios, not a delta of averages: a mean is
        # set by a handful of tail queries and can name a different least-moved
        # engine than the per-query medians do.
        lines += [
            "Cell is the **median per-query DSB/TPC-DS latency ratio**: "
            "`distribution-only (returns-affected)`. Below 1.00 = the skewed data ran "
            "faster than uniform. The first figure is the skew signal (identical "
            "table sizes); the parenthesised figure is the confounded set (bigger "
            "tables and skew together). `n=` marks an engine whose completed pair "
            "count trails its peers.",
            "",
        ]
        scales_present = {r[1] for r in skew_rows}
        max_clean_n = {sf: max(int(r[3]) for r in skew_rows if r[1] == sf) for sf in scales_present}
        skew_cells = {}
        for platform, sf, clean, clean_n, ret, ret_n in skew_rows:
            cell = f"{clean:.2f} ({ret:.2f})"
            if int(clean_n) < max_clean_n[sf]:
                cell += f" n={int(clean_n)}/{int(ret_n)}"
            skew_cells[(_disp(platform), f"SF{int(sf)}")] = cell
        lines += _render_pivot(
            "platform", skew_cells, col_order=_sf_order({c for _r, c in skew_cells})
        )

    if _chart_skew(con, assets):
        lines += ["![Skew impact](assets/skew_impact.png)", ""]

    return lines


def _chart_skew(con: duckdb.DuckDBPyConnection, assets: Path) -> bool:
    """Render the skew chart; return True iff a chart file was written.

    Same definition as the aggregate table above it — per-query DSB/TPC-DS latency
    ratio, split into distribution-only and returns-affected pairs — so the
    section's two artifacts cannot tell different stories. Charts are not
    drift-gated, so sharing the table's definition is what keeps them aligned.
    """
    plotly = _plotly()
    if plotly is None:
        return False
    go, make_subplots = plotly
    from .verifiers import returns_qnums

    returns = returns_qnums()
    rows = con.execute(f"""
        WITH per_q AS (
            SELECT platform, scale_factor, block,
                   CAST(REGEXP_EXTRACT(query_id, '(\\d+)$', 1) AS INTEGER) AS qnum,
                   MEDIAN({_PRIMARY_MS}) AS ms
            FROM bench
            WHERE error_message IS NULL AND run_type = 'warm'
              AND block IN ('tpcds', 'dsb') AND {_PRIMARY_MS} > 0
            GROUP BY 1, 2, 3, 4
        )
        SELECT t.platform, t.scale_factor, t.qnum, d.ms / t.ms AS ratio
        FROM per_q t
        JOIN per_q d USING (platform, scale_factor, qnum)
        WHERE t.block = 'tpcds' AND d.block = 'dsb' AND t.qnum <= 99 AND t.ms > 0
        ORDER BY t.platform, t.scale_factor, t.qnum
    """).fetchall()
    if not rows:
        return False

    platforms = sorted({r[0] for r in rows})
    scales = sorted({r[1] for r in rows})
    # (platform, scale) -> [(qnum, ratio)] ordered by qnum (SQL ORDER BY).
    series: dict[tuple[str, int], list[tuple[int, float]]] = {}
    for platform, sf, qnum, ratio in rows:
        if ratio is not None and ratio > 0:
            series.setdefault((platform, sf), []).append((qnum, ratio))

    clean_color, returns_color = "#4C72B0", "#DD8452"
    fig = make_subplots(
        rows=len(scales),
        cols=len(platforms),
        subplot_titles=[f"{_disp(p)} — SF{sf}" for sf in scales for p in platforms],
    )
    for r, sf in enumerate(scales, start=1):
        for c, p in enumerate(platforms, start=1):
            pts = series.get((p, sf), [])
            if not pts:
                continue
            fig.add_trace(
                go.Bar(
                    x=[q for q, _v in pts],
                    y=[v for _q, v in pts],
                    marker_color=[returns_color if q in returns else clean_color for q, _v in pts],
                    opacity=0.75,
                    showlegend=False,
                    hovertemplate="q%{x}: %{y:.2f}×<extra>" + _disp(p) + "</extra>",
                ),
                row=r,
                col=c,
            )
            fig.add_hline(y=1.0, line_width=1, line_color="#888888", row=r, col=c)
    fig.update_xaxes(title_text="Query number (q01–q99)")
    # Log y: 0.5× and 2× are equal-magnitude moves, and a linear ratio axis would
    # compress everything below 1.0 into a sliver while the table's story lives there.
    fig.update_yaxes(type="log")
    for r in range(1, len(scales) + 1):
        fig.update_yaxes(title_text="DSB / TPC-DS latency ratio (log)", row=r, col=1)
    fig.update_layout(
        title_text=(
            "DSB (skewed) vs TPC-DS (uniform): per-query latency ratio — "
            "blue = distribution-only pairs, orange = returns-affected (bigger tables); "
            "below the line = faster on skewed data"
        ),
        width=700 * len(platforms),
        height=500 * len(scales),
    )
    _write_chart(fig, assets, "skew_impact")
    return True


# ---------------------------------------------------------------------------
# Section: Pattern performance
# ---------------------------------------------------------------------------


def _section_patterns(con: duckdb.DuckDBPyConnection, assets: Path) -> list[str]:
    lines = ["## Query Pattern Performance", ""]

    try:
        from ..queries.registry import QueryRegistry

        registry = QueryRegistry()
    except Exception:
        lines += ["_(query registry unavailable; skipping pattern analysis)_", ""]
        return lines

    tag_rows: list[tuple[str, str]] = []
    for q in registry.all():
        for tag in q.patterns:
            tag_rows.append((q.id, tag))

    if not tag_rows:
        lines += ["_(no pattern tags inferred)_", ""]
        return lines

    con.execute("CREATE OR REPLACE TEMP TABLE _query_patterns (query_id VARCHAR, pattern VARCHAR)")
    con.executemany("INSERT INTO _query_patterns VALUES (?, ?)", tag_rows)

    # Pivot: rows = platform · pattern, cols = scale, cell = `p50 / p95` ms — a row
    # reads as one engine's trajectory on one query shape. (The bar chart below
    # carries the same data grouped for visual comparison.)
    pat_rows = con.execute(
        f"""
        SELECT
            r.platform, p.pattern, r.scale_factor,
            ROUND(MEDIAN({_PRIMARY_MS}), 0) AS median_ms,
            ROUND(APPROX_QUANTILE({_PRIMARY_MS}, 0.95), 0) AS p95_ms
        FROM bench r
        JOIN _query_patterns p USING (query_id)
        -- decision-support only: ClickBench queries carry pattern tags too, but
        -- they are the ClickBench project's per-engine SQL whose scale_factor is a
        -- campaign tag, not a volume — pooled here they would smear each scale
        -- column with ~100 GB ClickBench rows.
        WHERE r.error_message IS NULL AND r.run_type = 'warm'
          AND r.block IN ('tpcds', 'dsb')
        GROUP BY r.platform, p.pattern, r.scale_factor
        ORDER BY r.platform, p.pattern, r.scale_factor
        """
    ).fetchall()
    pat_cells = {
        (f"{_disp(plat)} · {pattern}", f"SF{sf}"): (
            f"{p50:,.0f} / {p95:,.0f} ms" if p50 is not None else "—"
        )
        for plat, pattern, sf, p50, p95 in pat_rows
    }
    lines += _render_pivot(
        "platform · pattern", pat_cells, col_order=_sf_order({c for _r, c in pat_cells})
    )

    _chart_patterns(con, assets)
    lines += ["![Pattern performance](assets/pattern_performance.png)", ""]

    con.execute("DROP TABLE IF EXISTS _query_patterns")
    return lines


def _chart_patterns(con: duckdb.DuckDBPyConnection, assets: Path) -> None:
    plotly = _plotly()
    if plotly is None:
        return
    go, make_subplots = plotly

    rows = con.execute(f"""
        SELECT r.platform, r.scale_factor, p.pattern, MEDIAN({_PRIMARY_MS}) AS median_ms
        FROM bench r
        JOIN _query_patterns p USING (query_id)
        WHERE r.error_message IS NULL AND r.run_type = 'warm'
          AND r.block IN ('tpcds', 'dsb')
        GROUP BY r.platform, r.scale_factor, p.pattern
    """).fetchall()
    if not rows:
        return

    platforms = sorted({r[0] for r in rows})
    scales = sorted({r[1] for r in rows})
    patterns = sorted({r[2] for r in rows})
    med = {(p, sf, pat): m for p, sf, pat, m in rows if m is not None}

    fig = make_subplots(
        rows=1,
        cols=len(scales),
        subplot_titles=[f"Latency by query pattern — SF{sf} (warm)" for sf in scales],
    )
    for c, sf in enumerate(scales, start=1):
        for i, p in enumerate(platforms):
            fig.add_trace(
                go.Bar(
                    x=patterns,
                    y=[med.get((p, sf, pat), 0.0) for pat in patterns],
                    name=_disp(p),
                    legendgroup=p,
                    showlegend=c == 1,
                    marker_color=_CHART_COLORS[i % len(_CHART_COLORS)],
                    opacity=0.8,
                    hovertemplate="%{x}: %{y:.0f} ms<extra>" + _disp(p) + "</extra>",
                ),
                row=1,
                col=c,
            )
    fig.update_yaxes(type="log")
    fig.update_yaxes(title_text="Median server-side latency (ms)", col=1)
    fig.update_xaxes(tickangle=30)
    fig.update_layout(barmode="group", width=1000 * len(scales), height=500)
    _write_chart(fig, assets, "pattern_performance")


# ---------------------------------------------------------------------------
# Section: Compute layout & efficiency
# ---------------------------------------------------------------------------


def _compute_descriptor(env: dict) -> str:
    """A compact, human-readable compute descriptor from an engine_environment
    JSON blob. Best-effort per engine (compute is exposed under different keys);
    falls back to any size/tier it can find."""
    plat = env.get("platform")
    actual = env.get("actual") or {}
    prov = env.get("provisioned") or {}
    if plat == "snowflake":
        size = actual.get("size") or prov.get("size") or "?"
        gen = env.get("warehouse_generation")
        return f"{size}" + (f" (Gen{gen})" if gen else "")
    if plat == "bigquery":
        # Actual reservation (live) decides slots vs on-demand — not the provisioned
        # intent (which would mislabel an on-demand run as reserved).
        slots = actual.get("slot_capacity")
        edition = actual.get("edition")
        return f"{edition or 'reservation'} / {slots} slots" if slots else "on-demand"
    if plat == "motherduck":
        return actual.get("size") or prov.get("tier") or "?"
    if plat == "databricks":
        # Same keys as compute_label_from_environment: cluster_size (live REST
        # reading) / size (how older captures in the results DB record it).
        return actual.get("cluster_size") or actual.get("size") or prov.get("warehouse_size") or "?"
    if plat == "redshift":
        # Same keys as compute_label_from_environment: base_capacity (live
        # boto3 reading) / baseline_rpu (terraform-provisioned slice).
        rpu = actual.get("base_capacity") or prov.get("baseline_rpu")
        return f"{rpu} RPU" if rpu else "?"
    if plat == "fabric":
        # Same keys as compute_label_from_environment: capacity_sku (live ARM reading).
        return actual.get("capacity_sku") or actual.get("sku") or prov.get("capacity") or "?"
    if plat == "clickhouse":
        return actual.get("size") or prov.get("tier") or "?"
    return actual.get("size") or prov.get("tier") or "?"


def _compute_layout(con: duckdb.DuckDBPyConnection) -> dict[tuple[str, int], str]:
    """Map (platform, scale_factor) -> compute descriptor from engine_environment
    (warm run). Used to annotate the efficiency table and render the layout."""
    layout: dict[tuple[str, int], str] = {}
    try:
        rows = con.execute(
            "SELECT platform, scale_factor, environment FROM engine_environment "
            "WHERE run_type = 'warm'"
        ).fetchall()
    except duckdb.Error:
        return layout
    for platform, scale, env_json in rows:
        try:
            env = json.loads(env_json) if isinstance(env_json, str) else (env_json or {})
        except (TypeError, ValueError):
            env = {}
        layout[(platform, int(scale))] = _compute_descriptor(env)
    return layout


def _size_aware_sql(blocks: tuple[str, ...]) -> str:
    """Size-aware warm rows in ``blocks``: one row per (platform, query, scale,
    run_number, concurrency, compute size). Unlike the main ``bench`` view, the
    dedup key INCLUDES ``compute_label`` so the *same* scale run at multiple
    compute sizes keeps one row per size (dropping all but the latest is right
    elsewhere but would hide a sizing sweep) — which is exactly
    ``results_latest``'s key, so this reads that view rather than re-deduping.

    ``scale_factor`` is rewritten through :func:`_scale_expr`, which pools
    ClickBench's tracking tag to one value. Without it, a single measurement of one
    fixed dataset splits across three columns labelled SF1 / SF10 / SF100.

    The comparison-admission policy applies here as it does in ``bench`` (these are
    warm rows by construction): DNF cells and ClickHouse multi-replica rows are
    excluded, or the size tables would republish the cells the policy note above
    them says are withheld. Their failure detail still renders — the error census
    and Coverage read the unfiltered table. (Both predicates exclude whole
    dedup partitions — a replica config, a DNF cell — never individual rows
    within one, so applying them after the dedup is equivalent.)
    """
    from .policy import dnf_exclusion_sql, single_session_replica_predicate

    return f"""
        SELECT * REPLACE ({_scale_expr(blocks)} AS scale_factor)
        FROM results_latest r
        WHERE r.run_type = 'warm' AND {_block_in(blocks)}
          AND {single_session_replica_predicate("r")}
          AND {dnf_exclusion_sql("r", by_label=True)}
    """


def _headline_rows(con: duckdb.DuckDBPyConnection, blocks: tuple[str, ...]) -> list[tuple]:
    """Suite-level headline aggregate: one row per (platform, scale, compute_label).

    Per-query medians (warm, successful, server-time > 0) are the unit of
    aggregation, so run repeats don't overweight any query:
      - geomean_ms      — EXP(mean of LN(ms), summed in exact decimal): the suite
                          speed number; robust to both
                          the handful of monster queries that dominate a SUM and
                          the many trivial ones that dominate a plain median.
      - suite_runtime_s — SUM of per-query medians: "the whole workload takes X s
                          of server time at this size".
      - suite_cost_usd  — SUM of per-query mean costs: the $ to run the suite once.
      - tail_ratio      — p95/p50 of per-query medians: undersized compute degrades
                          tails first (memory pressure → spill), so this inflates a
                          size or two before the median moves.
      - errors/timeouts — from ALL deduped rows (incl. failed): timeout failures on
                          small sizes are frontier signal, not noise.
    """
    from .views import install_views

    install_views(con)  # lazy: direct callers (tests, tools) skip _build_sections
    return con.execute(
        f"""
        WITH d AS ({_size_aware_sql(blocks)}),
        per_q AS (
            SELECT platform, scale_factor, compute_label, query_id,
                   MEDIAN(server_time_ms) AS ms,
                   SUM(CAST(cost_usd AS DECIMAL(28,12))) / NULLIF(COUNT(cost_usd), 0) AS cost
            FROM d
            WHERE error_message IS NULL AND server_time_ms > 0
            GROUP BY platform, scale_factor, compute_label, query_id
        ),
        ok AS (
            SELECT platform, scale_factor, compute_label,
                   COUNT(*) AS queries,
                   -- Geomean via an EXACT decimal log-sum, not AVG(LN(ms)): DuckDB's
                   -- parallel float AVG combines thread partials in nondeterministic
                   -- order, and the resulting low-bit wobble would rewrite the frontier
                   -- chart assets on every rebuild. Same determinism trick as the
                   -- DECIMAL money sums below.
                   EXP(SUM(CAST(LN(ms) AS DECIMAL(28,12))) / COUNT(*)) AS geomean_ms,
                   QUANTILE_CONT(ms, 0.5) AS p50_ms,
                   QUANTILE_CONT(ms, 0.95) / NULLIF(QUANTILE_CONT(ms, 0.5), 0) AS tail_ratio,
                   SUM(CAST(ms AS DECIMAL(28,12))) / 1000.0 AS suite_runtime_s,
                   SUM(CAST(cost AS DECIMAL(28,12))) AS suite_cost_usd
            FROM per_q GROUP BY platform, scale_factor, compute_label
        ),
        everything AS (
            SELECT platform, scale_factor, compute_label,
                   COUNT(*) AS runs,
                   SUM(CASE WHEN error_message IS NOT NULL THEN 1 ELSE 0 END) AS errors,
                   SUM(CASE WHEN error_message ILIKE '%timeout%'
                             OR error_message ILIKE '%timed out%'
                            THEN 1 ELSE 0 END) AS timeouts,
                   -- Failures our own client caused. Reported separately because
                   -- calling them engine timeouts attributes our bug to a named
                   -- vendor, and they pattern-match `%timeout%` so they would
                   -- otherwise be counted twice under the wrong label.
                   SUM(CASE WHEN {_client_cancel()} THEN 1 ELSE 0 END) AS client_cancelled
            FROM d GROUP BY platform, scale_factor, compute_label
        )
        SELECT e.platform, e.scale_factor, e.compute_label,
               o.queries, o.geomean_ms, o.p50_ms, o.tail_ratio,
               o.suite_runtime_s, o.suite_cost_usd,
               e.runs, e.errors, e.timeouts, e.client_cancelled,
               100.0 * e.errors / e.runs AS error_pct
        FROM everything e LEFT JOIN ok o
          ON e.platform = o.platform AND e.scale_factor = o.scale_factor
         AND COALESCE(e.compute_label, '') = COALESCE(o.compute_label, '')
        ORDER BY e.platform, e.scale_factor, e.compute_label
        """
    ).fetchall()


def _section_headlines(con: duckdb.DuckDBPyConnection, blocks: tuple[str, ...]) -> list[str]:
    """Suite-level headline metrics, one row per platform × scale × compute size."""
    layout = _compute_layout(con)
    agg = _headline_rows(con, blocks)
    if not agg:
        return []

    lines = [
        "## Headline Metrics",
        "",
        "One row per platform × compute size, one column per scale factor (warm runs, "
        "size-aware dedup): a row reads as that configuration's trajectory across "
        "data volumes.",
        "",
    ]
    speed_cells: dict[tuple[str, str], str] = {}
    work_cells: dict[tuple[str, str], str] = {}
    incidents: list[str] = []
    for (
        platform,
        scale,
        lbl,
        _queries,
        geo,
        _p50,
        tail,
        runtime,
        cost,
        runs,
        errs,
        tmo,
        ours,
        _pct,
    ) in agg:
        compute = _compute_row(lbl, layout.get((platform, int(scale))))
        usd_hr = effective_usd_per_hour(platform, compute)
        row = f"{_disp(platform)} · {compute}" + (
            f" (${usd_hr:g}/hr)" if usd_hr is not None else ""
        )
        col = _scale_col(blocks, int(scale))
        if geo is not None:
            speed_cells[(row, col)] = f"{geo:,.0f} ms (tail {tail:.1f}×)"
        if runtime is not None:
            cell = f"{runtime:,.0f} s"
            if cost and cost > 0:
                cell += f" · ${cost:,.2f}"
            work_cells[(row, col)] = cell
        if errs or tmo:
            # Our own client cancellations are named as ours, not folded into the
            # engine's timeout count — attributing our bug to a named vendor is the
            # one error in this report that cannot be corrected after publication.
            engine_tmo = max(tmo - (ours or 0), 0)
            detail = f"{engine_tmo} engine timeouts"
            if ours:
                detail += f", {ours} client-terminated"
            incidents.append(
                f"- {_disp(platform)} · {compute} @ SF{scale}: **{errs} errors** ({detail}) of {runs} runs"
            )

    lines += [
        "**Suite speed**: geomean of per-query median server ms (robust to outlier "
        "queries in both directions); `tail` is p95/p50 of the per-query medians, the "
        "undersizing early-warning (tails inflate a size or two before the median moves):",
        "",
    ]
    lines += _render_pivot(
        "platform · compute", speed_cells, col_order=_sf_order({c for _r, c in speed_cells})
    )
    lines += [
        "**Suite work**: total server time to run the suite once (sum of per-query "
        "medians), with measured cost where captured (blank $ = not yet reconciled):",
        "",
    ]
    lines += _render_pivot(
        "platform · compute", work_cells, col_order=_sf_order({c for _r, c in work_cells})
    )
    if incidents:
        lines += ["**Incidents** (errors/timeouts are frontier signal, not noise):", ""]
        lines += [*incidents, ""]
    else:
        lines += ["_No errors or timeouts recorded in warm runs._", ""]
    return lines


def _efficiency_rows(con: duckdb.DuckDBPyConnection, blocks: tuple[str, ...]) -> list[tuple]:
    """Size-aware efficiency aggregate: one row per (platform, block, scale,
    compute_label), deduped by ``_size_aware_sql`` so a sizing sweep keeps one
    row per compute size."""
    from .views import install_views

    install_views(con)  # lazy: direct callers (tests, tools) skip _build_sections
    return con.execute(
        f"""
        WITH d AS ({_size_aware_sql(blocks)})
        SELECT
            platform, block, scale_factor, compute_label,
            COUNT(*) AS runs,
            MEDIAN(server_time_ms) AS median_server_ms,
            SUM(CAST(cost_usd AS DECIMAL(28,12))) / NULLIF(COUNT(cost_usd), 0) AS avg_cost_usd,
            AVG(bytes_scanned) / 1e9 AS gb_per_query,
            -- Only scan-normalize when there's a real scan (>1 MB/query): excludes
            -- MotherDuck (local reads, bytes ≈ 0) and engines with no bytes yet.
            CASE WHEN AVG(bytes_scanned) > 1e6
                 THEN SUM(CAST(cost_usd AS DECIMAL(28,12))) / (SUM(bytes_scanned) / 1e12) END AS cost_per_tb,
            CASE WHEN AVG(bytes_scanned) > 1e6
                 THEN SUM(server_time_ms) / (SUM(bytes_scanned) / 1e9) END AS ms_per_gb
        FROM d
        WHERE error_message IS NULL
        GROUP BY platform, block, scale_factor, compute_label
        ORDER BY platform, block, scale_factor, compute_label
        """
    ).fetchall()


def _section_efficiency(
    con: duckdb.DuckDBPyConnection, assets: Path, blocks: tuple[str, ...], suffix: str
) -> list[str]:
    """Cost + performance efficiency, compute-aware. Surfaces the compute size each
    (platform, scale) ran on — including the *same* scale run at multiple sizes —
    and normalizes by data volume (cost per TB scanned; server-ms per GB scanned) so
    runs on different compute stay comparable. When a scale has been run at more than
    one size, a sizing-curve chart shows the cost/performance frontier."""
    layout = _compute_layout(con)  # (platform, scale) -> descriptor
    agg = _efficiency_rows(con, blocks)
    if not agg:
        return []

    def _compute(platform: str, scale: int, label: str | None) -> str:
        # The row's own auto-derived label, or the `unlabelled` bucket: _compute_row
        # never borrows the engine_environment descriptor (see its docstring).
        return _compute_row(label, layout.get((platform, int(scale))))

    lines = ["## Compute Layout & Efficiency", ""]

    # (a) Compute layout: distinct sizes seen per (platform, scale). Multiple rows for
    # one scale means that scale was swept across compute sizes. $/hr is the published
    # list price of that capacity — the one axis on which heterogeneous size knobs
    # (warehouse size, slots, RPUs, SKUs, tiers) are comparable across engines.
    cb = _is_clickbench(blocks)
    lines += [
        (
            "Every compute size each platform ran ClickBench on. "
            if cb
            else "The compute size each platform ran at each scale (a scale with more "
            "than one row was swept across sizes). "
        )
        + "**$/hr** is the list-price capacity rate for that "
        "size, the common axis across engines' heterogeneous size knobs (warehouse "
        "size, slots, RPUs, SKUs, tiers); blank where capacity has no hourly price "
        "(BigQuery on-demand is scan-priced). When compute differs, raw latency isn't "
        "directly comparable: the normalized efficiency metrics below let you compare "
        "anyway.",
        "",
    ]
    # ClickBench's scale is pooled, so the column would be a constant.
    layout_seen = sorted(
        {(p, sf, _compute(p, sf, lbl)) for p, _b, sf, lbl, *_ in agg},
        key=lambda r: (r[0], r[1], _size_sort_key(r[0], r[2])),
    )
    lines += _render_table(
        ["platform", "compute", "$/hr"] if cb else ["platform", "scale", "compute", "$/hr"],
        [
            (_disp(p), comp, effective_usd_per_hour(p, comp))
            if cb
            else (_disp(p), sf, comp, effective_usd_per_hour(p, comp))
            for p, sf, comp in layout_seen
        ],
    )
    lines += _unlabelled_note(con, blocks)

    # (b) Normalized efficiency per platform × block × compute size, pivoted on scale.
    lines += [
        "",
        "**Efficiency** (warm, successful runs). Cells are `ms/GB_scanned · $/TB_scanned` "
        ": performance and cost efficiency with data volume factored out, so a "
        "bigger-compute run that does proportionally more work looks equal, not "
        "artificially worse. Shown only when a real scan is measured (>1 MB/query): "
        "blank for MotherDuck (local reads, bytes ≈ 0) and for engines whose cost/bytes "
        "aren't captured yet (e.g. Snowflake before `reconcile`).",
        "",
    ]
    eff_cells: dict[tuple[str, str], str] = {}
    for platform, block, scale, lbl, _runs, _med, _cost, _gb, cpt, mpg in agg:
        if mpg is None and cpt is None:
            continue
        row = f"{_disp(platform)} · {_compute(platform, scale, lbl)} · {block}"
        parts = []
        if mpg is not None:
            parts.append(f"{mpg:,.1f} ms/GB")
        if cpt is not None:
            parts.append(f"${cpt:,.2f}/TB")
        eff_cells[(row, _scale_col(blocks, int(scale)))] = " · ".join(parts)
    lines += _render_pivot(
        "platform · compute · block",
        eff_cells,
        col_order=_sf_order({c for _r, c in eff_cells}),
    )

    if _chart_sizing_curve(con, assets, blocks, suffix):
        lines += [
            "",
            "Sizing frontier: latency and cost vs compute size for scales run at "
            "multiple sizes (lower-left is better on both axes):",
            "",
            f"![Sizing frontier](assets/sizing_curve_{suffix}.png)",
            "",
        ]
    if _chart_scaling_grid(con, assets, blocks, suffix):
        lines += [
            "",
            "Scaling surface: median server latency across the data-volume × "
            "compute-size plane (one panel per platform with more than one cell). "
            "Reading down a column shows what bigger compute buys at a fixed data "
            "volume; reading across a row shows how latency grows with data at a "
            "fixed size:",
            "",
            f"![Scaling surface](assets/scaling_grid_{suffix}.png)",
            "",
        ]
    if _chart_frontier_usd_hr(con, assets, blocks, suffix):
        lines += [
            "",
            "Cross-engine frontier: suite speed (geomean of per-query medians) vs "
            "list-price capacity $/hr, one line per platform (one panel per scale). "
            "Points on the same x are directly comparable spends; a flat line means "
            "extra capacity buys nothing for this workload:",
            "",
            f"![Cross-engine frontier](assets/frontier_usd_hr_{suffix}.png)",
            "",
        ]
    if _chart_pareto(con, assets, blocks, suffix):
        lines += [
            "",
            "Pareto frontier: measured suite cost vs suite runtime per (platform, "
            "compute size); one panel per scale. Filled markers are Pareto-optimal; "
            "hollow markers are dominated (some configuration is both faster and "
            "cheaper). Configurations with no measured cost yet (e.g. Snowflake "
            "before `reconcile`) are omitted:",
            "",
            f"![Pareto frontier](assets/pareto_{suffix}.png)",
            "",
        ]
    return lines


def _chart_sizing_curve(
    con: duckdb.DuckDBPyConnection, assets: Path, blocks: tuple[str, ...], suffix: str
) -> bool:
    """Plot median latency + avg cost vs compute size, one panel per (platform, scale)
    that was run at >1 size. Returns False (renders nothing) when no scale has been
    swept across sizes — the common single-size case."""
    plotly = _plotly()
    if plotly is None:
        return False
    go, make_subplots = plotly

    layout = _compute_layout(con)
    # Aggregate across blocks to (platform, scale, compute) -> latency, cost.
    per_size: dict[tuple[str, int], dict[str, tuple[float | None, float | None]]] = {}
    for platform, _block, scale, lbl, _runs, med, cost, *_ in _efficiency_rows(con, blocks):
        compute = _compute_row(lbl, layout.get((platform, int(scale))))
        bucket = per_size.setdefault((platform, int(scale)), {})
        # average the per-block medians/costs into one point per size (coarse but fine
        # for a frontier overview); keep the first non-null.
        prev = bucket.get(compute)
        if prev is None:
            bucket[compute] = (med, cost)
        else:
            pm, pc = prev
            bucket[compute] = (
                med if pm is None else (pm if med is None else (pm + med) / 2),
                cost if pc is None else (pc if cost is None else (pc + cost) / 2),
            )
    swept = {k: v for k, v in per_size.items() if len(v) > 1}
    if not swept:
        return False

    # Facet by platform (rows) × scale (cols): each swept (platform, scale) lands
    # in its own cell, so platforms with different scales don't share an x-axis of
    # incompatible size labels.
    row_platforms = sorted({p for p, _sf in swept})
    col_scales = sorted({sf for _p, sf in swept})
    fig = make_subplots(
        rows=len(row_platforms),
        cols=len(col_scales),
        specs=[[{"secondary_y": True} for _ in col_scales] for _ in row_platforms],
        subplot_titles=[
            f"{_disp(p)} — SF{sf}" if (p, sf) in swept else ""
            for p in row_platforms
            for sf in col_scales
        ],
    )
    first = True
    for (platform, scale), sizes in sorted(swept.items()):
        r = row_platforms.index(platform) + 1
        c = col_scales.index(scale) + 1
        # Order sizes by capacity list price (ascending) so the x-axis reads
        # small→large; unpriceable labels sort after, alphabetically.
        labels = sorted(sizes, key=lambda s: _size_sort_key(platform, s))
        ticks = [_label_with_rate(platform, s).replace("\n", "<br>") for s in labels]
        fig.add_trace(
            go.Scatter(
                x=ticks,
                y=[sizes[s][0] for s in labels],
                mode="lines+markers",
                name="median server ms",
                legendgroup="lat",
                showlegend=first,
                marker={"symbol": "circle"},
                line={"color": _CHART_COLORS[0]},
                hovertemplate="%{y:.0f} ms<extra>%{x}</extra>",
            ),
            row=r,
            col=c,
            secondary_y=False,
        )
        fig.add_trace(
            go.Scatter(
                x=ticks,
                y=[sizes[s][1] for s in labels],
                mode="lines+markers",
                name="avg cost $/query",
                legendgroup="cost",
                showlegend=first,
                marker={"symbol": "square"},
                line={"color": _CHART_COLORS[1]},
                hovertemplate="$%{y:.4f}/query<extra>%{x}</extra>",
            ),
            row=r,
            col=c,
            secondary_y=True,
        )
        fig.update_xaxes(title_text="compute size", tickangle=20, row=r, col=c)
        first = False
    # Anchor both y-axes at 0 so size-to-size deltas aren't visually exaggerated.
    fig.update_yaxes(rangemode="tozero")
    fig.update_yaxes(title_text="median server-side ms", secondary_y=False, col=1)
    fig.update_yaxes(title_text="avg cost $/query", secondary_y=True, col=len(col_scales))
    fig.update_layout(
        title_text="Sizing frontier: latency + cost vs compute size",
        width=600 * len(col_scales),
        height=450 * len(row_platforms),
    )
    _write_chart(fig, assets, f"sizing_curve_{suffix}")
    return True


def _size_sort_key(platform: str, label: str) -> tuple[int, float, str]:
    """Sort compute-size labels by list-price capacity ascending (the natural
    small→large order); labels with no known rate sort after, alphabetically."""
    usd = effective_usd_per_hour(platform, label)
    return (0, usd, label) if usd is not None else (1, 0.0, label)


def _label_with_rate(platform: str, label: str) -> str:
    """Axis label for a compute size, annotated with its $/hr when known."""
    usd = effective_usd_per_hour(platform, label)
    return f"{label}\n${usd:g}/hr" if usd is not None else label


def _headline_points(
    con: duckdb.DuckDBPyConnection, blocks: tuple[str, ...]
) -> list[tuple[str, int, str, float | None, float | None, float | None, float | None]]:
    """Headline rows resolved to chart-ready points:
    (platform, scale, compute, geomean_ms, p50_ms, suite_runtime_s, suite_cost_usd)."""
    layout = _compute_layout(con)
    pts = []
    for platform, scale, lbl, _q, geo, p50, _tail, runtime, cost, *_ in _headline_rows(con, blocks):
        compute = _compute_row(lbl, layout.get((platform, int(scale))))
        pts.append((platform, int(scale), compute, geo, p50, runtime, cost))
    return pts


def _chart_scaling_grid(
    con: duckdb.DuckDBPyConnection, assets: Path, blocks: tuple[str, ...], suffix: str
) -> bool:
    """The scaling surface: a scale-factor × compute-size grid of median server
    latency, one panel per platform that has more than one populated cell.
    Returns False (renders nothing) when no platform has >1 cell."""
    plotly = _plotly()
    if plotly is None:
        return False
    go, make_subplots = plotly
    import math

    cells: dict[str, dict[tuple[int, str], float]] = {}
    for platform, scale, compute, _geo, p50, _rt, _cost in _headline_points(con, blocks):
        if p50 is not None and p50 > 0:
            cells.setdefault(platform, {})[(scale, compute)] = p50
    grids = {p: c for p, c in cells.items() if len(c) > 1}
    if not grids:
        return False

    panels = sorted(grids.items())
    # One shared log color scale across panels (single colorbar, real-ms ticks).
    all_vals = [v for grid in grids.values() for v in grid.values()]
    zmin = math.log10(max(min(all_vals), 1e-3))
    zmax = math.log10(max(all_vals))
    if zmax <= zmin:
        zmax = zmin + 1.0
    tickvals = [zmin + f * (zmax - zmin) for f in (0.0, 0.25, 0.5, 0.75, 1.0)]

    # Row-wise: one row per platform (stacked), so the shared data-volume x-axis
    # lines up vertically across engines.
    fig = make_subplots(rows=len(panels), cols=1, subplot_titles=[_disp(p) for p, _g in panels])
    for i, (platform, grid) in enumerate(panels, start=1):
        scales = sorted({sf for sf, _s in grid})
        sizes = sorted({s for _sf, s in grid}, key=lambda s: _size_sort_key(platform, s))
        fig.add_trace(
            go.Heatmap(
                x=[f"SF{sf}" for sf in scales],
                y=[_label_with_rate(platform, s).replace("\n", "<br>") for s in sizes],
                # Log-scaled color; None cells render blank.
                z=[
                    [math.log10(grid[(sf, s)]) if (sf, s) in grid else None for sf in scales]
                    for s in sizes
                ],
                text=[
                    [f"{grid[(sf, s)]:,.0f}" if (sf, s) in grid else "" for sf in scales]
                    for s in sizes
                ],
                texttemplate="%{text}",
                coloraxis="coloraxis",
                hovertemplate="%{x} · %{y}: %{text} ms<extra>" + _disp(platform) + "</extra>",
            ),
            row=i,
            col=1,
        )
        fig.update_xaxes(title_text="data volume", row=i, col=1)
    fig.update_layout(
        coloraxis={
            "colorscale": "Viridis",
            "reversescale": True,
            "cmin": zmin,
            "cmax": zmax,
            "colorbar": {
                "title": "median server ms (log)",
                "tickvals": tickvals,
                "ticktext": [f"{10**v:,.0f}" for v in tickvals],
            },
        },
        title_text="Scaling surface: median latency across data volume × compute size",
        width=900,
        height=400 * len(panels),
    )
    _write_chart(fig, assets, f"scaling_grid_{suffix}")
    return True


def _chart_frontier_usd_hr(
    con: duckdb.DuckDBPyConnection, assets: Path, blocks: tuple[str, ...], suffix: str
) -> bool:
    """Cross-engine frontier: suite speed (geomean ms) vs list-price capacity $/hr,
    one line per platform, one panel per scale. Only sizes with a known $/hr appear;
    needs ≥2 priceable points in some scale to render."""
    plotly = _plotly()
    if plotly is None:
        return False
    go, make_subplots = plotly

    by_scale: dict[int, dict[str, list[tuple[float, float, str]]]] = {}
    for platform, scale, compute, geo, _p50, _rt, _cost in _headline_points(con, blocks):
        usd = effective_usd_per_hour(platform, compute)
        if usd is None or geo is None or geo <= 0:
            continue
        by_scale.setdefault(scale, {}).setdefault(platform, []).append((usd, geo, compute))
    by_scale = {
        sf: plats for sf, plats in by_scale.items() if sum(len(v) for v in plats.values()) > 1
    }
    if not by_scale:
        return False

    panels = sorted(by_scale.items())
    # Row-wise: one row per scale factor (stacked), each with its own $/hr x-axis
    # range (SF100 spans a wider $/hr band than SF1, so a shared axis would squash it).
    fig = make_subplots(
        rows=len(panels),
        cols=1,
        subplot_titles=[f"SF{scale}" for scale, _p in panels],
    )
    # Stable platform → colour across all rows.
    plat_color = {
        p: _CHART_COLORS[i % len(_CHART_COLORS)]
        for i, p in enumerate(sorted({pl for _sf, plats in panels for pl in plats}))
    }
    legend_done: set[str] = set()
    for i, (_scale, plats) in enumerate(panels, start=1):
        for platform, pts in sorted(plats.items()):
            pts = sorted(pts)
            fig.add_trace(
                go.Scatter(
                    x=[p[0] for p in pts],
                    y=[p[1] for p in pts],
                    text=[p[2] for p in pts],
                    mode="lines+markers+text",
                    textposition="top center",
                    textfont={"size": 8},
                    name=_disp(platform),
                    legendgroup=platform,
                    showlegend=platform not in legend_done,
                    line={"color": plat_color[platform]},
                    hovertemplate="%{text}: $%{x:.2f}/hr → %{y:.0f} ms<extra>"
                    + _disp(platform)
                    + "</extra>",
                ),
                row=i,
                col=1,
            )
            legend_done.add(platform)
        fig.update_xaxes(title_text="capacity list price ($/hr)", row=i, col=1)
        fig.update_yaxes(title_text="suite geomean server ms", row=i, col=1)
    # Linear axes anchored at 0: with a handful of size steps per engine the
    # absolute magnitudes matter more than ratios, and 0-anchoring keeps the
    # speed-per-dollar slopes honest.
    fig.update_xaxes(rangemode="tozero")
    fig.update_yaxes(rangemode="tozero")
    fig.update_layout(
        title_text="Cross-engine frontier: suite speed vs capacity $/hr",
        width=800,
        height=450 * len(panels),
    )
    _write_chart(fig, assets, f"frontier_usd_hr_{suffix}")
    return True


def _chart_pareto(
    con: duckdb.DuckDBPyConnection, assets: Path, blocks: tuple[str, ...], suffix: str
) -> bool:
    """Pareto frontier of measured suite cost vs suite runtime per (platform, size),
    one panel per scale with ≥2 costed points. Points with no measured cost (e.g.
    Snowflake before `reconcile` back-fills credits) are omitted."""
    plotly = _plotly()
    if plotly is None:
        return False
    go, make_subplots = plotly

    by_scale: dict[int, list[tuple[float, float, str, str]]] = {}
    for platform, scale, compute, _geo, _p50, runtime, cost in _headline_points(con, blocks):
        if runtime is None or cost is None or cost <= 0:
            continue
        by_scale.setdefault(scale, []).append((runtime, cost, platform, compute))
    by_scale = {sf: pts for sf, pts in by_scale.items() if len(pts) >= 2}
    if not by_scale:
        return False

    def _pareto(pts: list[tuple[float, float, str, str]]) -> set[int]:
        """Indexes of non-dominated points (no other point is <= on both axes
        and < on at least one)."""
        optimal = set()
        for i, (rt_i, c_i, *_r) in enumerate(pts):
            dominated = any(
                (rt_j <= rt_i and c_j <= c_i) and (rt_j < rt_i or c_j < c_i)
                for j, (rt_j, c_j, *_s) in enumerate(pts)
                if j != i
            )
            if not dominated:
                optimal.add(i)
        return optimal

    panels = sorted(by_scale.items())
    n = len(panels)
    # Row-wise: one row per scale. Each row gets its OWN legend, because the
    # Pareto-optimal set (filled markers) differs per scale — a shared legend
    # would imply one config is "the winner" across scales, which isn't true, and
    # a shared legend toggle would hide a platform in panels where it's optimal.
    fig = make_subplots(rows=n, cols=1, subplot_titles=[f"SF{scale}" for scale, _p in panels])
    # Stable platform → colour across rows (legend swatches stay consistent).
    plat_colors = {
        p: _CHART_COLORS[i % len(_CHART_COLORS)]
        for i, p in enumerate(sorted({pl for _sf, pts in panels for _r, _c, pl, _s in pts}))
    }
    legends: dict[str, dict] = {}
    for i, (_scale, pts) in enumerate(panels, start=1):
        legend_id = "legend" if i == 1 else f"legend{i}"
        optimal = _pareto(pts)
        # One trace per platform; per-point open/filled symbol marks dominated
        # vs Pareto-optimal within THIS scale.
        by_plat: dict[str, list[tuple[int, tuple[float, float, str, str]]]] = {}
        for j, pt in enumerate(pts):
            by_plat.setdefault(pt[2], []).append((j, pt))
        for platform, jpts in sorted(by_plat.items()):
            fig.add_trace(
                go.Scatter(
                    x=[pt[0] for _j, pt in jpts],
                    y=[pt[1] for _j, pt in jpts],
                    text=[pt[3] for _j, pt in jpts],
                    mode="markers+text",
                    textposition="top center",
                    textfont={"size": 8},
                    name=_disp(platform),
                    legend=legend_id,
                    marker={
                        "size": 10,
                        "color": plat_colors[platform],
                        "symbol": ["circle" if j in optimal else "circle-open" for j, _pt in jpts],
                    },
                    hovertemplate="%{text}: %{x:.1f} s → $%{y:.4f}<extra>"
                    + _disp(platform)
                    + "</extra>",
                ),
                row=i,
                col=1,
            )
        front = sorted((pts[j][0], pts[j][1]) for j in optimal)
        if len(front) > 1:
            fig.add_trace(
                go.Scatter(
                    x=[p[0] for p in front],
                    y=[p[1] for p in front],
                    mode="lines",
                    line={"color": "gray", "shape": "hv"},
                    opacity=0.6,
                    showlegend=False,
                    hoverinfo="skip",
                ),
                row=i,
                col=1,
            )
        fig.update_xaxes(title_text="suite runtime (server s)", row=i, col=1)
        fig.update_yaxes(title_text="suite cost ($)", row=i, col=1)
        # Anchor this row's legend beside its panel (paper-y center of the row).
        legends[legend_id] = {
            "x": 1.02,
            "xanchor": "left",
            "y": 1 - (i - 0.5) / n,
            "yanchor": "middle",
            "title": {"text": f"SF{panels[i - 1][0]}"},
        }
    # Anchor at 0/0 so relative positions reflect absolute cost/runtime.
    fig.update_xaxes(rangemode="tozero")
    fig.update_yaxes(rangemode="tozero")
    fig.update_layout(
        title_text="Pareto frontier: measured suite cost vs runtime (filled = optimal)",
        width=800,
        height=450 * n,
        **legends,
    )
    _write_chart(fig, assets, f"pareto_{suffix}")
    return True


# ---------------------------------------------------------------------------
# Section: Engine profiling (bytes / spill / cache / queue)
# ---------------------------------------------------------------------------


def _section_profiling(con: duckdb.DuckDBPyConnection, blocks: tuple[str, ...]) -> list[str]:
    """Per-engine data-read + resource profiling: bytes scanned (efficiency +
    cost driver), bytes spilled (right-sizing signal), cache-hit % (validates
    caching is disabled, golden rule #9), and queue/compile time (queue vs
    compute). Columns are blank for engines that don't expose a given signal.
    Omitted entirely if no engine reports any of them."""
    bf = _block_in(blocks)
    have = con.execute(
        f"""
        SELECT COUNT(*) FROM bench
        WHERE run_type = 'warm' AND {bf}
          AND (bytes_scanned IS NOT NULL OR bytes_spilled IS NOT NULL
               OR cache_hit_pct IS NOT NULL OR queue_ms IS NOT NULL
               OR compile_ms IS NOT NULL)
        """
    ).fetchone()[0]
    if not have:
        return []
    lines = [
        "## Engine Profiling",
        "",
        "Data-read and resource signals pulled from each engine's query history "
        "alongside server time. **bytes_scanned** is the data-read efficiency metric "
        "and a primary cost driver (for MotherDuck this is cloud bytes *downloaded*, "
        "≈0 when the duckling already holds the data locally); **bytes_spilled** flags "
        "queries that exceeded memory (a right-sizing signal); **cache_hit_pct** "
        "validates that result caching is disabled (golden rule #9; expect ~0); "
        "**queue_ms** / **compile_ms** separate waiting/planning from compute. "
        "Blank = the engine doesn't expose that signal.",
        "",
        "Broken out by **compute size** (`bytes_spilled` in particular is meaningless "
        "without it: spill is what an undersized warehouse does, so it must be read "
        "per size, not averaged across a sweep). Uses size-aware dedup, so a swept "
        "scale shows one row per size rather than collapsing to the last-run size.",
        "",
    ]
    layout = _compute_layout(con)
    prof = con.execute(
        f"""
        WITH d AS ({_size_aware_sql(blocks)})
        SELECT
            platform, compute_label, block, scale_factor,
            COUNT(*) AS runs,
            ROUND(AVG(bytes_scanned) / 1e6, 1) AS avg_mb_scanned,
            ROUND(MAX(bytes_spilled) / 1e6, 1) AS max_mb_spilled,
            ROUND(AVG(cache_hit_pct), 1) AS avg_cache_pct,
            ROUND(AVG(queue_ms), 1) AS avg_queue_ms,
            ROUND(AVG(compile_ms), 1) AS avg_compile_ms
        FROM d
        WHERE error_message IS NULL
        GROUP BY platform, compute_label, block, scale_factor
        ORDER BY platform, compute_label, block, scale_factor
        """
    ).fetchall()
    # ClickBench pools its scale tag, so a `scale` column would carry one constant
    # value in every row and read as a dimension it does not have.
    cb = _is_clickbench(blocks)
    header = [
        "platform",
        "compute",
        "block",
        *([] if cb else ["scale"]),
        "runs",
        "avg_mb_scanned",
        "max_mb_spilled",
        "avg_cache_pct",
        "avg_queue_ms",
        "avg_compile_ms",
    ]
    rows = [
        (
            _disp(plat),
            _compute_row(lbl, layout.get((plat, int(sf)))),
            block,
            *(() if cb else (sf,)),
            runs,
            mb,
            sp,
            ch,
            q,
            cm,
        )
        for plat, lbl, block, sf, runs, mb, sp, ch, q, cm in prof
    ]
    lines += _render_table(header, rows, float_fmt=".1f")
    return lines


# ---------------------------------------------------------------------------
# Section: Provenance (run reproducibility)
# ---------------------------------------------------------------------------


def _section_provenance(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Run-provenance ledger: the command, harness version, executed-query hash and
    optimize model behind the results, with the count of result rows each run
    produced. Omitted if the results DB holds no provenance rows. Git SHAs stay in
    ``benchmark_run`` but are not rendered: they name commits outside the
    published history."""
    try:
        have = con.execute("SELECT COUNT(*) FROM benchmark_run").fetchone()[0]
    except duckdb.Error:
        return []
    if not have:
        return []
    lines = [
        "## Run Provenance",
        "",
        "Every measured row links to the invocation that produced it, so results "
        "are reproducible. `query_hash` identifies the exact SQL files executed. "
        "`optimize_model` names the engines whose SQL went through an LLM "
        "optimization pass, and the model that did it, read from the pass's own audit "
        "log; blank means no engine in that run received one.",
        "",
    ]
    lines += _query_table(
        con,
        """
        SELECT
            LEFT(CAST(r.provenance_id AS VARCHAR), 8) AS run,
            r.command, r.scale_factor AS sf, r.run_type,
            r.platforms,
            r.harness_version AS version,
            LEFT(COALESCE(r.query_files_hash, ''), 8) AS query_hash,
            r.optimize_model,
            COUNT(b.provenance_id) AS result_rows,
            CAST(r.created_at AS VARCHAR)[:19] AS created_at
        FROM benchmark_run r
        LEFT JOIN main.sql_benchmark_results b ON b.provenance_id = r.provenance_id
        GROUP BY ALL
        ORDER BY created_at DESC
    """,
    )
    return lines


# ---------------------------------------------------------------------------
# Section: Cross-engine row-count agreement
# ---------------------------------------------------------------------------


def _section_row_count_agreement(con: duckdb.DuckDBPyConnection) -> list[str]:
    """The correctness cross-check above SF1.

    `validate-sf1` is the only real correctness gate and it runs at ~1 GB. This
    section is what the methodology promises in its place: seven engines — six
    dialect translations plus MotherDuck's untranslated DuckDB SQL — one ingest
    path: do they return the same number of rows? It is weaker than an oracle and the section says so, including how many
    cells carry real information versus how many merely agree on `LIMIT 100`.
    """
    from ..analysis.agreement import agreement_on

    report = agreement_on(con)
    if not report.summary:
        return []

    lines = [
        "## Cross-engine row-count agreement",
        "",
        "Correctness is verified against the DuckDB oracle at SF1 only. Above that, "
        "this is the cross-check that stands in its place: for each decision-support "
        "query at each scale, do the engines return the **same number of rows**? "
        "Seven engines (six independent dialect translations plus MotherDuck's "
        "untranslated DuckDB SQL), one byte-identical ingest path: a divergence means "
        "at least one engine (or one of our translations) is wrong.",
        "",
        "**What this does and does not establish.** Agreement is necessary, not "
        "sufficient: two engines can return 100 rows of different values. Most "
        "TPC-DS/DSB templates end in `LIMIT 100`, and a cell that agrees on exactly "
        "100 is weak evidence. The `discriminating` column counts the cells whose "
        "agreed row count is something *other* than the cap, where the count is a "
        "property of the data and agreement therefore carries information.",
        "",
        "Compute sizes and the three measured runs are **pooled**: a row count must "
        "not depend on warehouse size or run index, so every size an engine ran "
        "contributes, and `self-disagree` counts cells where an engine contradicted "
        "itself. ClickBench is excluded: its queries are the per-engine variants "
        "published in the ClickBench project, so engines there are not running "
        "equivalent SQL and a row-count difference would carry no correctness meaning.",
        "",
    ]
    lines += _render_table(
        [
            "Scale",
            "Block",
            "Queries",
            "Engines",
            "Agree",
            "Diverge",
            "Discriminating",
            "Self-disagree",
        ],
        [
            (
                f"SF{s.scale_factor}",
                s.block,
                s.queries,
                f"{s.engines_min}"
                if s.engines_min == s.engines_max
                else f"{s.engines_min}–{s.engines_max}",
                s.unanimous,
                s.divergent,
                s.discriminating,
                s.unstable_cells,
            )
            for s in report.summary
        ],
    )

    if report.divergences:
        lines += [
            f"**{len(report.divergences)} divergent cells.** Each is a correctness "
            "defect in at least one engine or in our translation of that query, and "
            "is listed in full rather than summarized.",
            "",
        ]
        lines += _render_table(
            ["Scale", "Block", "Query", "Row counts by engine"],
            [
                (f"SF{d.scale_factor}", d.block, d.query_id, d.describe())
                for d in report.divergences
            ],
        )
    else:
        lines += [
            f"**No divergence anywhere: {report.total_cells:,} (query, scale) cells, "
            f"zero disagreements, zero engines contradicting themselves.** "
            f"{report.total_discriminating:,} of those cells are discriminating.",
            "",
        ]

    if report.missing:
        lines += [
            "**Not every cell was compared across all seven engines**, and the absences "
            "are load-correlated rather than random: unanimity among four engines is "
            "weaker evidence than unanimity among seven. Cells each engine is absent "
            "from:",
            "",
        ]
        lines += _render_table(
            ["Scale", "Engine", "Cells absent"],
            [(f"SF{m.scale_factor}", _disp(m.platform), m.missing_cells) for m in report.missing],
        )

    return lines


# ---------------------------------------------------------------------------
# Section: Methodology
# ---------------------------------------------------------------------------


def _section_methodology(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Data-driven methodology — reflects the scale, platforms, blocks, run
    types and cost tiers actually present in the results, not a fixed template."""
    scale = _scale_label(con)
    platforms = _platforms(con)
    blocks = _blocks(con)
    run_types = [
        r[0]
        for r in con.execute("SELECT DISTINCT run_type FROM bench ORDER BY run_type").fetchall()
    ]
    block_label = " + ".join(b.upper() for b in blocks) if blocks else "—"

    lines = [
        "## Methodology",
        "",
        f"- **Workload:** {block_label} at {scale}",
        f"- **Platforms:** {', '.join(_disp(p) for p in platforms) if platforms else '—'}",
        f"- **Run types present:** {', '.join(run_types) if run_types else '—'}",
        "- **Execution:** 1 warmup + 3 measured warm runs per query per platform; "
        "cold = 1 run with caches/warehouse flushed; concurrency = N parallel runs per level",
        "- **Latency metric:** Server-side execution time (ms) from each engine's query history, "
        "the sole latency metric. Client wall-clock is **not** reported: it adds driver + network "
        "RTT that varies by client location and penalizes remote engines (e.g. MotherDuck), making "
        "it an unfair cross-engine comparator. Engines expose server time after `benchmark "
        "reconcile` (MotherDuck via the durable `MD_QUERY_HISTORY()`); any row still missing it is "
        "excluded from the latency aggregates, never wall-clock-substituted",
        "- **Correctness:** not measured here; verified separately by `validate-sf1` (a tolerant, "
        "column-aligned comparison vs the DuckDB ground truth), the single correctness gate",
        "- **Deduplication:** latest run per (platform, query, scale, run_type, run_number, "
        "concurrency); re-runs supersede, never double-count",
        "- **Result caching:** disabled where the engine supports it (documented per platform)",
        "",
        "### Cost confidence tiers (platforms in this report)",
        "",
        "| Tier | Platforms | Method | Uncertainty |",
        "|------|-----------|--------|-------------|",
    ]
    tier_rows = con.execute("""
        SELECT cost_confidence, STRING_AGG(DISTINCT platform, ', ' ORDER BY platform) AS platforms
        FROM bench
        WHERE cost_confidence IS NOT NULL
        GROUP BY cost_confidence
        ORDER BY cost_confidence
    """).fetchall()
    # Stored tier value -> (published tier name, method, uncertainty). The published
    # names (Measured / Allocated / Modeled) are the buyer's guide's; the database
    # stores `direct` / `proportional` / `estimated`.
    method = {
        "direct": ("Measured", "The metered per-query bill (BigQuery on-demand)", "±5%"),
        "proportional": (
            "Allocated",
            "List rate × engine time, capped or split against billed capacity",
            "±15%",
        ),
        "estimated": ("Modeled", "List rate × engine time, no invoice behind it", "±30%"),
    }
    for conf, plats in tier_rows:
        tier, m, unc = method.get(conf, (conf.title(), "—", "—"))
        names = ", ".join(_disp(p) for p in plats.split(", "))
        lines.append(f"| {tier} (`{conf}`) | {names} | {m} | {unc} |")
    lines += [
        "",
        "_Snowflake is Modeled (`estimated`): list rate × engine time. Its per-query "
        "attribution view (6–8h latency) is kept as a cross-check, not as the price._",
        "",
    ]
    return lines

"""Verifiers for the number-drift gate — one per marked table.

Each function recomputes the table its marker names, **encoding the methodology the
table itself asserts on the page**. That is the whole point: a table computed a
different way than it claims then fails the gate. A table that says "each engine at
the compute tier it was designated for each scale" is only checked by a verifier that
reads the designated tier from ``run_plan.yaml``.

Two rules apply throughout:

- **Designated tiers come from ``run_plan.yaml``**, resolved through
  ``report/canonical.py`` — never from a plausible-looking ladder.
- **Never sum across differing completion sets.** A per-tier suite total must be
  taken over the queries *every* tier in the comparison completed, with n stated.
  Summing each tier's own completed set understates the fastest tiers, because the
  queries the small tiers dropped are the expensive ones.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import TYPE_CHECKING, NamedTuple

from .display import ENGINE_DISPLAY
from .figures import verifier

if TYPE_CHECKING:
    import duckdb

logger = logging.getLogger("benchmark.report.verifiers")

DECISION_SUPPORT = "block IN ('tpcds', 'dsb')"

#: Display name -> engine id, matching how the authored tables label rows.
#: Derived from the shared display map so the gate and the generator cannot
#: spell an engine two ways.
ENGINE_LABELS = {display: platform for platform, display in ENGINE_DISPLAY.items()}

SCALES = (1, 10, 100, 1000)


def tier_display(label: str) -> str:
    """How the authored tables spell a compute label.

    Snowflake's rows stamp "Medium (Gen2)" but the prose tables write "Medium". The
    gate matches rows by label, so without this every Snowflake row reads as absent
    from the data and the real drift beside it gets lost in the noise.
    """
    return label.removesuffix(" (Gen2)")


def _canonical_table(con: duckdb.DuckDBPyConnection) -> None:
    """Materialize the run-plan designated warm tier per (platform, scale).

    Derived from the shared ``_canonical_tier`` table (installed by
    :func:`benchmark.report.views.install_views`) so the gate and the report
    cannot resolve the designated tier two different ways. Installing the views
    here also guarantees ``results_latest`` exists for every verifier, whichever
    entry point ran it.
    """
    from .views import install_views

    install_views(con)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _verify_canonical AS "
        "SELECT platform, scale_factor, label FROM _canonical_tier "
        "WHERE run_class = 'warm'"
    )


def _suite_seconds_at_designated_tier(
    con: duckdb.DuckDBPyConnection,
) -> dict[tuple[str, int], float]:
    """Suite time per (platform, scale) at the run-plan designated tier.

    Suite time = sum of per-query median server-side ms. Errored rows and rows with
    no server time are excluded (they have no latency to sum), which is why the
    completion count matters and is logged. Reads ``results_latest`` so a
    superseded re-run cannot blend into a median beside the row that replaced it.
    """
    _canonical_table(con)
    rows = con.execute(f"""
        WITH d AS (
            SELECT r.platform, r.scale_factor, r.query_id,
                   MEDIAN(r.server_time_ms) AS ms
            FROM results_latest r
            JOIN _verify_canonical c
              ON r.platform = c.platform
             AND r.scale_factor = c.scale_factor
             AND r.compute_label = c.label
            WHERE r.run_type = 'warm' AND {DECISION_SUPPORT}
              AND r.error_message IS NULL AND r.server_time_ms > 0
            GROUP BY 1, 2, 3
        )
        SELECT platform, scale_factor, COUNT(*) AS nq,
               SUM(CAST(ms AS DECIMAL(28,12))) / 1000 AS suite_s
        FROM d GROUP BY 1, 2
    """).fetchall()
    out: dict[tuple[str, int], float] = {}
    for platform, scale, nq, suite_s in rows:
        out[(platform, int(scale))] = float(suite_s)
        logger.debug("suite %s SF%s: %.1fs over %d queries", platform, scale, suite_s, nq)
    return out


#: Engines deliberately absent from the data-scaling table, with the reason. Kept
#: here rather than only in prose so the gate treats the omission as intended instead
#: of reporting it as missing coverage forever — and so the reason is checkable
#: against the data by whoever next reads the verifier.
SCALING_EXCLUSIONS = {
    "motherduck": (
        "its designated tier changes twice across the ladder (Standard→Jumbo, "
        "Jumbo→Giga), so two of three steps would measure a hardware change"
    ),
    "bigquery": (
        "its designated compute changes billing model, not just size (on-demand at "
        "1 and 10 GB, a slot reservation above), so the steps are not comparable "
        "(the 10→100 GB step reads as 0.9x, i.e. faster on ten times the data)"
    ),
    "clickhouse": (
        "it completes 69 of 201 decision-support queries at 1 TB, so its suite totals "
        "at the two largest scales are sums over different query sets"
    ),
}


@verifier("scaling_at_designated_tier")
def scaling_at_designated_tier(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """Suite time per engine across scales, each at its designated tier, plus the
    per-step multipliers. Encodes the table's own stated basis, so a row that takes
    different scales from different tiers fails.
    """
    suite = _suite_seconds_at_designated_tier(con)
    out: list[list[str]] = []
    for label, platform in ENGINE_LABELS.items():
        if platform in SCALING_EXCLUSIONS:
            continue
        values = [suite.get((platform, sf)) for sf in SCALES]
        if any(v is None for v in values):
            continue  # engine has no complete designated ladder to compare
        cells = [f"{v:.1f} s" for v in values]
        steps = " → ".join(f"{values[i + 1] / values[i]:.1f}×" for i in range(len(values) - 1))
        out.append([label, *cells, steps])
    return out


def _ladder_common_complete(
    con: duckdb.DuckDBPyConnection, platform: str, scale: int
) -> dict[str, tuple[int, float, float]]:
    """Per-tier (n, suite seconds, total cost) on the COMMON-COMPLETE query set.

    The subset is the queries every tier at this (platform, scale) completed. Summing
    each tier's own completed set instead understates the fast tiers: on the Snowflake
    ladder, sums over 197/199/201/201/201 queries read 5.38x where the common-complete
    figure is 6.33x.

    Reads ``results_latest`` (latest-wins per size), so a superseded re-run can
    never blend into the ladder beside the measurement that replaced it.
    """
    from .views import install_views

    install_views(con)
    rows = con.execute(
        f"""
        WITH d AS (
            SELECT compute_label, query_id,
                   MEDIAN(server_time_ms) AS ms,
                   SUM(CAST(cost_usd AS DECIMAL(28,12))) / NULLIF(COUNT(cost_usd), 0) AS cost
            FROM results_latest
            WHERE platform = ? AND scale_factor = ? AND run_type = 'warm'
              AND {DECISION_SUPPORT} AND error_message IS NULL AND server_time_ms > 0
              AND compute_label IS NOT NULL
            GROUP BY 1, 2
        ),
        common AS (
            SELECT query_id FROM d GROUP BY 1
            HAVING COUNT(DISTINCT compute_label) = (SELECT COUNT(DISTINCT compute_label) FROM d)
        )
        SELECT d.compute_label, COUNT(*) AS nq,
               SUM(CAST(d.ms AS DECIMAL(28,12))) / 1000 AS suite_s,
               SUM(CAST(d.cost AS DECIMAL(28,12))) AS total_cost
        FROM d JOIN common USING (query_id)
        GROUP BY 1
        """,
        [platform, scale],
    ).fetchall()
    return {r[0]: (int(r[1]), float(r[2]), float(r[3] or 0.0)) for r in rows}


SNOWFLAKE_TIERS = (
    ("X-Small", "X-Small (Gen2)"),
    ("Small", "Small (Gen2)"),
    ("Medium", "Medium (Gen2)"),
    ("Large", "Large (Gen2)"),
    ("X-Large", "X-Large (Gen2)"),
)


@verifier("snowflake_sf1000_ladder")
def snowflake_sf1000_ladder(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """Suite time, cumulative speedup and marginal gain per doubling, at 1 TB."""
    ladder = _ladder_common_complete(con, "snowflake", 1000)
    base = ladder[SNOWFLAKE_TIERS[0][1]][1]
    out: list[list[str]] = []
    prev: float | None = None
    for display, label in SNOWFLAKE_TIERS:
        _n, suite, _cost = ladder[label]
        gain = "—" if prev is None else f"{prev / suite:.2f}×"
        out.append([display, f"{suite:.1f}", f"{base / suite:.2f}×", gain])
        prev = suite
    return out


@verifier("snowflake_speedup_by_scale")
def snowflake_speedup_by_scale(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """Cumulative speedup vs X-Small at 100 GB and at 1 TB, same ladder.

    The comparison the report leans on: the return on compute depends on whether the
    data is big enough to use it.
    """
    at = {sf: _ladder_common_complete(con, "snowflake", sf) for sf in (100, 1000)}
    base = {sf: at[sf][SNOWFLAKE_TIERS[0][1]][1] for sf in at}
    return [
        [
            display,
            f"{base[100] / at[100][label][1]:.2f}×",
            f"{base[1000] / at[1000][label][1]:.2f}×",
        ]
        for display, label in SNOWFLAKE_TIERS
    ]


@verifier("snowflake_cost_per_speedup")
def snowflake_cost_per_speedup(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """Cost per unit of speedup: (cost / base cost) / (base time / time).

    1.00 means the extra speed was proportionally priced, below 1.00 a bargain,
    above 1.00 an overpay. Uses the same common-complete subset as the speedup, so
    numerator and denominator describe the same queries.
    """
    at = {sf: _ladder_common_complete(con, "snowflake", sf) for sf in (100, 1000)}

    def ratio(sf: int, label: str) -> str:
        _base_n, base_suite, base_cost = at[sf][SNOWFLAKE_TIERS[0][1]]
        _n, suite, cost = at[sf][label]
        if not base_cost or not cost:
            return "—"
        speedup = base_suite / suite
        return f"{(cost / base_cost) / speedup:.2f}"

    return [[display, ratio(100, label), ratio(1000, label)] for display, label in SNOWFLAKE_TIERS]


@verifier("redshift_sf1_inversion")
def redshift_sf1_inversion(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """Redshift's 1 GB inversion: suite time and cost per RPU band."""
    ladder = _ladder_common_complete(con, "redshift", 1)
    bands = sorted(ladder, key=lambda label: int(label.split()[0]))
    return [
        [label, f"{ladder[label][1]:.1f}", f"${ladder[label][2]:.2f}"]
        for label in bands
        if label != "8 RPU"  # the table starts at 16 RPU, its cheapest measured band
    ]


class ClickBenchConfig(NamedTuple):
    """One single-session ClickBench configuration, priced as every figure is priced."""

    platform: str
    label: str
    geomean_ms: float
    #: The tier's list rate (context: the tier's price, not the cost of the work).
    usd_per_hour: float | None
    #: Suite cost: the sum of each query's median per-query cost, as decision support's
    #: "sum of per-query costs"; ``None`` when any completed query is unpriced.
    suite_usd: float | None
    #: Median per-query cost x 1,000, as the decision-support and sizing tables report.
    usd_per_1k: float | None


def _clickbench_geomeans(con: duckdb.DuckDBPyConnection) -> list[ClickBenchConfig]:
    """Every single-session ClickBench configuration with its latency and cost.

    ClickBench is one fixed ~100 GB dataset, so its ``scale_factor`` tag is pooled —
    it records which scale campaign a run rode along with, not a data volume. Reading
    it as a scale would keep one compute size per tag and hide four fifths of the grid.

    Cost comes from the stored per-query cost (list rate x engine time on every
    engine; ARCHITECTURE §7), the same source as every decision-support cost figure,
    so ClickBench is priced the way everything else in the report is; the tier's
    hourly list rate rides along as context only.

    Single-session comparison policy applies (``report/policy.py``): ClickHouse
    admits only single-replica configurations here, so a swept multi-replica config
    can never be promoted into a single-session table, and a DNF engine (more than
    10% of the suite failed) drops out rather than being ranked on its survivors.
    """
    from .compute_rates import effective_usd_per_hour
    from .policy import dnf_exclusion_sql, single_session_replica_predicate
    from .views import install_views

    install_views(con)
    rows = con.execute(f"""
        WITH d AS (
            SELECT * FROM results_latest r
            WHERE r.block = 'clickbench' AND r.run_type = 'warm'
              AND {single_session_replica_predicate("r")}
              AND {dnf_exclusion_sql("r", by_label=True)}
        ),
        per_q AS (
            SELECT platform, compute_label, query_id, MEDIAN(server_time_ms) AS ms,
                   MEDIAN(cost_usd) AS usd
            FROM d
            WHERE error_message IS NULL AND server_time_ms > 0 AND compute_label IS NOT NULL
            GROUP BY 1, 2, 3
        )
        SELECT platform, compute_label, EXP(AVG(LN(ms))) AS geomean_ms,
               CASE WHEN count(usd) = count(*) THEN SUM(usd) END AS suite_usd,
               MEDIAN(usd) * 1000 AS usd_per_1k
        FROM per_q GROUP BY 1, 2
    """).fetchall()
    return [
        ClickBenchConfig(
            platform,
            label,
            float(geo),
            effective_usd_per_hour(platform, label),
            float(suite) if suite is not None else None,
            float(per_1k) if per_1k is not None else None,
        )
        for platform, label, geo, suite, per_1k in rows
    ]


def _money(usd: float | None) -> str:
    return f"${usd:,.2f}" if usd is not None else "—"


def _suite_money(usd: float | None) -> str:
    """A ClickBench suite costs cents; four decimals keep the tiers apart."""
    return f"${usd:,.4f}" if usd is not None else "—"


def _clickbench_row(c: ClickBenchConfig, *, lead: str) -> list[str]:
    display = {v: k for k, v in ENGINE_LABELS.items()}
    engine = display.get(c.platform, c.platform)
    tier, geo = tier_display(c.label), f"{c.geomean_ms:.0f} ms"
    cost, per_1k, rate = _suite_money(c.suite_usd), _money(c.usd_per_1k), _money(c.usd_per_hour)
    if lead == "latency":
        return [engine, geo, tier, cost, per_1k, rate]
    return [engine, cost, tier, geo, per_1k, rate]


@verifier("clickbench_fastest_tier")
def clickbench_fastest_tier(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """Each engine's fastest measured ClickBench configuration — the leaderboard
    reading — with what its suite costs, per query, and the tier's hourly rate."""
    best: dict[str, ClickBenchConfig] = {}
    for c in _clickbench_geomeans(con):
        if c.platform not in best or c.geomean_ms < best[c.platform].geomean_ms:
            best[c.platform] = c
    return [
        _clickbench_row(c, lead="latency")
        for c in sorted(best.values(), key=lambda c: c.geomean_ms)
    ]


@verifier("clickbench_cheapest_suite")
def clickbench_cheapest_suite(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """Each engine's cheapest ClickBench pass: the configuration whose suite costs
    least, the counterpart of decision support's "cheapest full pass". It ranks the
    cost of the work, not hourly rates."""
    cheapest: dict[str, ClickBenchConfig] = {}
    for c in _clickbench_geomeans(con):
        if c.suite_usd is None:
            continue
        if c.platform not in cheapest or c.suite_usd < cheapest[c.platform].suite_usd:
            cheapest[c.platform] = c
    return [
        _clickbench_row(c, lead="cost")
        for c in sorted(cheapest.values(), key=lambda c: c.suite_usd or 0.0)
    ]


# ── skew step: TPC-DS vs DSB at 100 GB ──────────────────────────────────────

#: Tables DSB enlarges beyond TPC-DS (2.50x / 2.00x / 1.50x at every scale —
#: verified against data/*/expected_counts.json). Queries touching them mix a
#: cardinality change into what is otherwise a pure distribution change, so the
#: skew comparison splits on this.
_RETURNS_TABLE_RE = re.compile(r"\b(?:store|web|catalog)_returns\b", re.IGNORECASE)


@lru_cache(maxsize=1)
def returns_qnums() -> frozenset[int]:
    """Query numbers (1-99) whose canonical SQL touches a ``*_returns`` table.

    Derived from the query text itself rather than a hand-kept list, so the split
    cannot drift from the suite. TPC-DS and DSB agree on this classification for
    q1-99 (same SQL by construction; both blocks checked so a divergence fails
    loudly as a wrong-looking count downstream rather than silently biasing one
    side of the pair).
    """
    from ..queries.registry import QueryRegistry

    out: set[int] = set()
    for q in QueryRegistry().all():
        if q.block not in ("tpcds", "dsb"):
            continue
        qnum = int(re.sub(r"\D", "", q.id.rsplit("q", 1)[-1]))
        if qnum <= 99 and _RETURNS_TABLE_RE.search(q.sql):
            out.add(qnum)
    return frozenset(out)


def skew_pair_ratios(con: duckdb.DuckDBPyConnection) -> list[tuple[str, int, float]]:
    """(platform, qnum, DSB/TPC-DS ratio) per paired query, warm, 100 GB.

    The basis the skew table asserts: each engine at its run-plan designated tier,
    latest run wins, median server-side ms per query, DSB q1-99 paired against the
    identical TPC-DS SQL, single-session comparison policy applied (single-replica
    ClickHouse, DNF cells out — see ``report/policy.py``). Only pairs where both
    suites completed contribute — a ratio needs both sides.
    """
    from .policy import dnf_exclusion_sql, single_session_replica_predicate

    _canonical_table(con)
    rows = con.execute(f"""
        WITH d AS (
            SELECT r.platform, r.block, r.query_id, r.server_time_ms, r.error_message
            FROM results_latest r
            JOIN _verify_canonical c
              ON r.platform = c.platform AND r.scale_factor = c.scale_factor
             AND r.compute_label = c.label
            WHERE r.run_type = 'warm' AND r.scale_factor = 100
              AND r.block IN ('tpcds', 'dsb')
              AND {single_session_replica_predicate("r")}
              AND {dnf_exclusion_sql("r", by_label=True)}
        ),
        per_q AS (
            SELECT platform, block,
                   CAST(REGEXP_EXTRACT(query_id, '(\\d+)$', 1) AS INTEGER) AS qnum,
                   MEDIAN(server_time_ms) AS ms
            FROM d
            WHERE error_message IS NULL AND server_time_ms > 0
            GROUP BY 1, 2, 3
        )
        SELECT t.platform, t.qnum, dsb.ms / t.ms
        FROM per_q t
        JOIN per_q dsb USING (platform, qnum)
        WHERE t.block = 'tpcds' AND dsb.block = 'dsb' AND t.qnum <= 99 AND t.ms > 0
        ORDER BY 1, 2
    """).fetchall()
    return [(str(p), int(q), float(r)) for p, q, r in rows]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


@verifier("skew_ratio_by_engine")
def skew_ratio_by_engine(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """Median DSB/TPC-DS ratio per engine at 100 GB, split on the returns confound.

    The clean column is the skew finding — distribution changed, table sizes held.
    The returns column is the 25 pairs where DSB also enlarges the ``*_returns``
    tables, so cardinality and distribution move together. Each engine's median is
    over its own completed pairs with n published beside it, which is what makes
    ClickHouse's shorter coverage visible rather than silently absorbed.
    """
    returns = returns_qnums()
    display = {v: k for k, v in ENGINE_LABELS.items()}
    by_engine: dict[str, dict[bool, list[float]]] = {}
    for platform, qnum, ratio in skew_pair_ratios(con):
        by_engine.setdefault(platform, {True: [], False: []})[qnum in returns].append(ratio)
    out = []
    for platform, groups in by_engine.items():
        clean, ret = groups[False], groups[True]
        out.append(
            [
                display.get(platform, platform),
                f"{_median(clean):.2f}",
                str(len(clean)),
                f"{_median(ret):.2f}",
                str(len(ret)),
            ]
        )
    return sorted(out, key=lambda row: float(row[1]))


@verifier("clickhouse_completion_by_memory")
def clickhouse_completion_by_memory(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """Distinct queries ClickHouse completed per single-replica rung at 100 GB.

    Deliberately NOT filtered by the DNF floor — this table is the evidence for the
    floor verdict, so filtering by it would leave only the rung that passes and erase
    the point. It counts a query as completed when any measured run succeeded, which
    is the floor's own measure (see ``policy.DNF_MAX_FAILURE_SHARE``), so the numbers
    here and the exclusion decision cannot drift apart.

    Rows are ordered by memory ascending; the suite totals come from the data rather
    than constants so adding a block cannot silently change a denominator.
    """
    from .views import install_views

    install_views(con)
    rows = con.execute("""
        WITH d AS (
            SELECT platform, block, query_id, compute_label, error_message
            FROM results_latest
            WHERE platform = 'clickhouse' AND run_type = 'warm'
              AND scale_factor = 100 AND block IN ('tpcds', 'dsb')
              AND compute_label LIKE '1x%'
        ),
        totals AS (
            SELECT block, COUNT(DISTINCT query_id) AS n
            FROM main.sql_benchmark_results WHERE block IN ('tpcds', 'dsb') GROUP BY 1
        )
        SELECT CAST(regexp_extract(d.compute_label, 'x(\\d+)GB', 1) AS INTEGER) AS gb,
               d.block,
               COUNT(DISTINCT CASE WHEN d.error_message IS NULL THEN d.query_id END) AS ok,
               MAX(t.n) AS suite_n
        FROM d JOIN totals t USING (block)
        GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchall()

    by_gb: dict[int, dict[str, str]] = {}
    for gb, block, ok, suite_n in rows:
        by_gb.setdefault(gb, {})[block] = f"{ok} / {suite_n}"
    return [
        [f"{gb} GB", cells.get("tpcds", "—"), cells.get("dsb", "—")]
        for gb, cells in sorted(by_gb.items())
    ]


@verifier("databricks_1tb_completion_by_tier")
def databricks_1tb_completion_by_tier(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """Unfinished share and median engine time at 100 sessions, 1 TB, per Databricks tier.

    Deliberately NOT filtered by the DNF floor or the single-session policy: the point
    of this table is the SHED rate itself, and excluding the configurations that shed
    most would erase the finding. It is one engine at one scale and one level, which is
    why it is presented as a mechanism demonstration rather than a cross-engine ranking.

    Ordered smallest tier first, so the share visibly falls as the warehouse grows.
    Rows the harness failed are left out (``failure_class`` ``harness``): they
    measure the harness, not the warehouse.
    """
    from ..analysis.concurrency_shape import MIN_ENGINE_TIME_PCT, UNFINISHED_CLASSES
    from .views import install_views

    install_views(con)
    order = {"2X-Small": 1, "X-Small": 2, "Small": 3, "Medium": 4, "Large": 5}
    unfinished = ", ".join(f"'{c}'" for c in UNFINISHED_CLASSES)
    rows = con.execute(f"""
        SELECT compute_label,
               100.0 * count(*) FILTER (WHERE failure_class IN ({unfinished})) / count(*),
               quantile_cont(engine_ms, 0.5) FILTER (WHERE error_message IS NULL),
               100.0 * count(engine_ms)
                 / NULLIF(count(*) FILTER (WHERE error_message IS NULL), 0)
        FROM concurrency_results
        WHERE platform = 'databricks' AND scale_factor = 1000 AND concurrency = 100
          AND block IN ('tpcds', 'dsb') AND compute_label IS NOT NULL
          AND failure_class IS DISTINCT FROM 'harness'
        GROUP BY 1
    """).fetchall()
    out = []
    for label, shed, p50, timed in sorted(rows, key=lambda r: order.get(r[0], 9)):
        # Engine time (queue included) or nothing: `*` when it was not captured.
        if timed is not None and timed < MIN_ENGINE_TIME_PCT:
            median = "*"
        else:
            median = f"{p50 / 1000:.1f} s" if p50 else "—"
        out.append([label, f"{shed:.1f}%", median])
    return out


@verifier("queueing_exponent", key_columns=2)
def queueing_exponent(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """The power-law exponent k per (engine, tier, scale): how steeply latency rises
    with sessions. Same source as `useful_concurrency`, so the two tables cannot
    disagree with each other or with the module that defines the fit."""
    from ..analysis.concurrency_shape import shapes

    display = {v: k for k, v in ENGINE_LABELS.items()}
    by_row: dict[tuple[str, str], list[str]] = {}
    for cell in shapes(con):
        row = by_row.setdefault((cell.platform, cell.compute_label), ["—"] * len(SCALES))
        idx = SCALES.index(cell.scale_factor)
        if not cell.publishable:
            row[idx] = "*"
        elif cell.k is not None:
            row[idx] = f"{cell.k:.2f}"
    return [
        [display.get(platform, platform), tier_display(label), *row]
        for (platform, label), row in sorted(by_row.items())
    ]


@verifier("useful_concurrency", key_columns=2)
def useful_concurrency(con: duckdb.DuckDBPyConnection) -> list[list[str]]:
    """Most sessions a configuration serves under the latency + error bar.

    Reads `analysis/concurrency_shape`, so the published table cannot drift from the
    module that defines the metric. A cell whose shedding was client-caused is not an
    engine property and renders as withheld (`*`) rather than a number; a cell with
    no data at all renders `—`. Those two are kept distinct on purpose — conflating
    "not measured" with "served nothing" is its own misstatement.
    """
    from ..analysis.concurrency_shape import shapes

    cells = {(c.platform, c.compute_label, c.scale_factor): c for c in shapes(con)}
    by_row: dict[tuple[str, str], list[str]] = {}
    for (platform, label, scale), cell in cells.items():
        key = (platform, label)
        row = by_row.setdefault(key, ["—"] * len(SCALES))
        # "50+" marks a CENSORED ceiling: the cell passed at the highest level we
        # ever asked of it, so the number is a lower bound. Unmarked, a censored 50
        # reads as half of an uncensored 100 when it may only mean we never tried
        # 100 there.
        if not cell.publishable:
            row[SCALES.index(scale)] = "*"
        else:
            mark = "+" if cell.censored else ""
            row[SCALES.index(scale)] = f"{cell.useful_concurrency}{mark}"
    display = {v: k for k, v in ENGINE_LABELS.items()}
    return [
        [display.get(platform, platform), tier_display(label), *row]
        for (platform, label), row in sorted(by_row.items())
    ]

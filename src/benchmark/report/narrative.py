"""Hand-authored report prose, composed into the generated draft.

``generator.py`` builds every section from the results DB. The argument of the
report — methodology, framing, limitations — is prose, and prose cannot be derived
from a query. This module lets the two live together in one ``benchmark report``
invocation (golden rule 7) without the prose drifting away from the data.

Two mechanisms, and the second is the point:

**1. Anchored composition.** Files in ``report/sections/*.md`` declare where they
belong via an HTML-comment header, and are spliced into the generated section list
at that anchor. Filename prefixes order multiple sections sharing an anchor.

    <!-- anchor: after-front-matter -->

**2. Fact substitution.** A number written into prose by hand is a number that
silently goes stale the next time the campaign is re-analysed — and a report whose
narrative contradicts its own tables is worse than one with no narrative. So prose
does not hardcode computed values. It writes ``{{fact:sf1000_large_speedup}}``
and this module resolves it from the same database the tables are built from.

An unknown or unresolvable fact is a **hard error**, not a silent passthrough:
shipping a literal ``{{fact:...}}`` into a published report, or quietly rendering a
stale number, are both worse than failing the build.
"""

from __future__ import annotations

import itertools
import logging
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import duckdb

    from ..analysis.agreement import AgreementReport
    from .verifiers import ClickBenchConfig

from .display import ENGINE_DISPLAY

logger = logging.getLogger("benchmark.report.narrative")

DEFAULT_SECTIONS_DIR = Path("report/sections")

# Where an authored section may be spliced. Keep this list small and meaningful —
# an anchor is a promise about document structure, so adding one is a deliberate
# act rather than a convenience.
ANCHORS: tuple[str, ...] = (
    "before-everything",
    "executive-lead",
    "after-front-matter",
    "before-decision-support",
    "before-clickbench",
    "before-methodology",
    "after-everything",
)

_ANCHOR_RE = re.compile(r"<!--\s*anchor:\s*([a-z0-9-]+)\s*-->", re.I)
_FACT_RE = re.compile(r"\{\{fact:([a-z0-9_]+)\}\}", re.I)


class Section(NamedTuple):
    """One authored prose file and where it belongs."""

    path: Path
    anchor: str
    body: str


# ── facts ───────────────────────────────────────────────────────────────────
#
# Each entry computes ONE published number from the results DB. Keep them small and
# individually checkable; a fact that needs a paragraph of logic belongs in a
# generated section, not in prose.


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    row = con.execute(sql).fetchone()
    return None if row is None else row[0]


def _engines_with_data(con: duckdb.DuckDBPyConnection) -> str:
    n = _scalar(con, "SELECT count(DISTINCT platform) FROM main.sql_benchmark_results")
    return str(n)


def _total_rows(con: duckdb.DuckDBPyConnection) -> str:
    n = _scalar(con, "SELECT count(*) FROM main.sql_benchmark_results")
    return f"{int(n):,}"


def _distinct_queries(con: duckdb.DuckDBPyConnection) -> str:
    n = _scalar(con, "SELECT count(DISTINCT query_id) FROM main.sql_benchmark_results")
    return str(n)


def _concurrency_levels(con: duckdb.DuckDBPyConnection) -> str:
    """The protocol levels that carry rows. ClickBench's off-ladder development level
    (40) is dropped here as it is from every published concurrency figure."""
    from ..analysis.concurrency_shape import PROTOCOL_LEVELS

    rows = con.execute(
        "SELECT DISTINCT concurrency FROM main.sql_benchmark_results "
        "WHERE concurrency IS NOT NULL ORDER BY concurrency"
    ).fetchall()
    return "/".join(str(int(r[0])) for r in rows if int(r[0]) in PROTOCOL_LEVELS)


def _cold_row_count(con: duckdb.DuckDBPyConnection) -> str:
    n = _scalar(con, "SELECT count(*) FROM main.sql_benchmark_results WHERE run_type = 'cold'")
    return f"{int(n):,}"


def _fabric_row_count(con: duckdb.DuckDBPyConnection) -> str:
    n = _scalar(con, "SELECT count(*) FROM main.sql_benchmark_results WHERE platform = 'fabric'")
    return f"{int(n):,}"


def _agreement(con: duckdb.DuckDBPyConnection) -> AgreementReport:
    """Cross-engine row-count agreement. Recomputed per fact rather than cached:
    the queries are cheap, and a cache keyed on a mutable connection is a worse
    hazard than three extra scans."""
    from ..analysis.agreement import agreement_on

    return agreement_on(con)


def _row_count_cells(con: duckdb.DuckDBPyConnection) -> str:
    return f"{_agreement(con).total_cells:,}"


def _row_count_divergences(con: duckdb.DuckDBPyConnection) -> str:
    return f"{_agreement(con).total_divergent:,}"


def _row_count_discriminating(con: duckdb.DuckDBPyConnection) -> str:
    return f"{_agreement(con).total_discriminating:,}"


# ── ladder facts ────────────────────────────────────────────────────────────
#
# Prose numbers about the Snowflake compute ladder move whenever the underlying
# data is repaired or the comparison subset changes, so they must never be typed
# by hand. Computed
# from the same function the drift gate's verifiers use, so prose and table cannot
# disagree.


def _ladder(
    con: duckdb.DuckDBPyConnection, platform: str, scale: int
) -> dict[str, tuple[int, float, float]]:
    """Per-tier (n, suite seconds, cost) on the common-complete query set.

    Deliberately the *same* function the drift gate's verifiers call, so a prose
    number and the table above it cannot be computed two different ways.
    """
    from .verifiers import _ladder_common_complete

    return _ladder_common_complete(con, platform, scale)


def _sf_ladder_pair(con: duckdb.DuckDBPyConnection, scale: int, tier: str) -> tuple[float, float]:
    """(speedup, cost multiple) of one Snowflake tier vs X-Small at ``scale``."""
    ladder = _ladder(con, "snowflake", scale)
    _n, base_s, base_c = ladder["X-Small (Gen2)"]
    _n2, suite, cost = ladder[tier]
    return base_s / suite, (cost / base_c if base_c else float("nan"))


def _sf1000_large_speedup(con: duckdb.DuckDBPyConnection) -> str:
    return f"{_sf_ladder_pair(con, 1000, 'Large (Gen2)')[0]:.2f}×"


def _sf1000_large_money(con: duckdb.DuckDBPyConnection) -> str:
    return f"{_sf_ladder_pair(con, 1000, 'Large (Gen2)')[1]:.2f}×"


def _sf1000_large_price_per_speed(con: duckdb.DuckDBPyConnection) -> str:
    speed, money = _sf_ladder_pair(con, 1000, "Large (Gen2)")
    return f"{money / speed:.2f}"


def _sf1000_large_cost(con: duckdb.DuckDBPyConnection) -> str:
    return f"${_ladder(con, 'snowflake', 1000)['Large (Gen2)'][2]:.3f}"


def _sf1000_xsmall_cost(con: duckdb.DuckDBPyConnection) -> str:
    return f"${_ladder(con, 'snowflake', 1000)['X-Small (Gen2)'][2]:.3f}"


def _sf100_xlarge_money(con: duckdb.DuckDBPyConnection) -> str:
    return f"{_sf_ladder_pair(con, 100, 'X-Large (Gen2)')[1]:.2f}×"


def _sf100_xlarge_cost_per_speedup(con: duckdb.DuckDBPyConnection) -> str:
    speed, money = _sf_ladder_pair(con, 100, "X-Large (Gen2)")
    return f"{money / speed:.2f}"


def _sf1000_best_single_gain(con: duckdb.DuckDBPyConnection) -> str:
    """Largest gain from any single doubling on the 1 TB ladder.

    Computed rather than typed so the prose can never contradict the ladder
    table it sits beside.
    """
    ladder = _ladder(con, "snowflake", 1000)
    order = [
        "X-Small (Gen2)",
        "Small (Gen2)",
        "Medium (Gen2)",
        "Large (Gen2)",
        "X-Large (Gen2)",
    ]
    gains = [ladder[a][1] / ladder[b][1] for a, b in itertools.pairwise(order)]
    return f"{max(gains):.2f}×"


# ── concurrency-shape facts ─────────────────────────────────────────────────
#
# These move whenever a concurrency cell is added or a withheld one is re-measured,
# so they must not be written by hand.


def _concurrency_cells(con: duckdb.DuckDBPyConnection) -> str:
    from ..analysis.concurrency_shape import shapes

    return str(len(shapes(con)))


def _concurrency_mode_mix(con: duckdb.DuckDBPyConnection) -> str:
    """The failure-mode census, rendered as a clause.

    Every cell is accounted for, including the ones whose curve could not be fitted —
    a census that silently drops a category invites the reader to divide by the wrong
    total.
    """
    from collections import Counter

    from ..analysis.concurrency_shape import shapes

    counts = Counter(c.failure_mode for c in shapes(con))
    phrasing = [
        ("queues", "queue"),
        ("absorbs", "absorb"),
        ("queues+sheds", "do both"),
        ("rejects", "reject"),
    ]
    parts = [f"{counts[key]} {word}" for key, word in phrasing if counts.get(key)]
    if counts.get("unknown"):
        parts.append(f"{counts['unknown']} measured too few levels to fit a curve")
    return ", ".join(parts[:-1]) + (f", and {parts[-1]}" if len(parts) > 1 else "")


def _star_schema_medians(con: duckdb.DuckDBPyConnection, scale: int) -> None:
    """Temp table ``_star_medians``: per (platform, query_id) median server ms, warm,
    at ``scale``, each engine at its run-plan designated tier, latest run wins,
    comparison policy applied (DNF cells and ClickHouse multi-replica rows excluded
    — see ``report/policy.py``).

    The shared slice behind the cross-engine star-schema facts. Pooling every compute
    size an engine ever ran at a scale — Snowflake's X-Large sweep rows beside its
    designated Medium — is the wrong-slice class this report's own verifiers exist
    to catch, so the slice is pinned to the designated tier.
    """
    from .policy import dnf_exclusion_sql, single_session_replica_predicate
    from .verifiers import _canonical_table

    _canonical_table(con)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _star_medians AS
        SELECT r.platform, r.query_id, MEDIAN(r.server_time_ms) AS ms
        FROM results_latest r
        JOIN _verify_canonical c
          ON r.platform = c.platform AND r.scale_factor = c.scale_factor
         AND r.compute_label = c.label
        WHERE r.run_type = 'warm' AND r.scale_factor = ?
          AND r.block IN ('tpcds', 'dsb')
          AND {single_session_replica_predicate("r")}
          AND {dnf_exclusion_sql("r", by_label=True)}
          AND r.error_message IS NULL AND r.server_time_ms > 0
        GROUP BY 1, 2
        """,
        [scale],
    )


def _cross_engine_star(
    con: duckdb.DuckDBPyConnection, scale: int
) -> tuple[list[tuple[str, float]], int]:
    """Per-engine star-schema geomean at ``scale`` on the common-complete query set.

    Restricted to the queries every policy-admitted engine completed — the
    survivor-set trap this report warns about, applied to its own headline ratios.
    """
    _star_schema_medians(con, scale)
    rows = con.execute("""
        WITH common AS (
            SELECT query_id FROM _star_medians GROUP BY 1
            HAVING COUNT(DISTINCT platform) =
                   (SELECT COUNT(DISTINCT platform) FROM _star_medians)
        )
        SELECT platform, COUNT(*) AS nq, EXP(AVG(LN(ms))) AS geomean
        FROM _star_medians JOIN common USING (query_id)
        GROUP BY 1 ORDER BY 3
    """).fetchall()
    return [(r[0], float(r[2])) for r in rows], (int(rows[0][1]) if rows else 0)


def _cross_engine_sf100(con: duckdb.DuckDBPyConnection) -> tuple[list[tuple[str, float]], int]:
    return _cross_engine_star(con, 100)


def _cross_engine_common_n(con: duckdb.DuckDBPyConnection) -> str:
    _ordered, n = _cross_engine_sf100(con)
    return str(n)


def _clickhouse_star_trajectory(con: duckdb.DuckDBPyConnection) -> str:
    """ClickHouse's star-schema gap to the leader at each scale where the policy
    admits it (single-replica designated tiers: 1 and 10 GB), as a clause.

    The 100 GB and 1 TB cells are policy-excluded (multi-replica / DNF), so the
    trajectory deliberately stops where the admissible data stops.
    """
    parts = []
    for scale, unit in ((1, "1 GB"), (10, "10 GB")):
        ordered, n = _cross_engine_star(con, scale)
        by = dict(ordered)
        if "clickhouse" not in by:
            raise NarrativeError(f"clickhouse absent from the SF{scale} comparison")
        parts.append(f"{by['clickhouse'] / ordered[0][1]:.1f}× the leader at {unit} (n={n})")
    return " and ".join(parts)


# ── skew-step facts (TPC-DS vs DSB) ─────────────────────────────────────────
#
# Same computation as the `skew_ratio_by_engine` verifier — imported, not copied —
# so the prose and the gated table cannot disagree about what a ratio is.


def _skew_clean_medians(con: duckdb.DuckDBPyConnection) -> dict[str, float]:
    """Per-engine median DSB/TPC-DS ratio over the clean (non-returns) pairs."""
    from .verifiers import _median, returns_qnums, skew_pair_ratios

    returns = returns_qnums()
    by_engine: dict[str, list[float]] = {}
    for platform, qnum, ratio in skew_pair_ratios(con):
        if qnum not in returns:
            by_engine.setdefault(platform, []).append(ratio)
    return {p: _median(v) for p, v in by_engine.items()}


def _skew_clean_ratio_range(con: duckdb.DuckDBPyConnection) -> str:
    medians = _skew_clean_medians(con)
    return f"{min(medians.values()):.2f} to {max(medians.values()):.2f}"


def _skew_bigquery_clean_ratio(con: duckdb.DuckDBPyConnection) -> str:
    return f"{_skew_clean_medians(con)['bigquery']:.2f}"


def _fmt_ms(ms: float) -> str:
    return f"{ms:,.0f} ms" if ms < 1000 else f"{ms / 1000:,.1f} s"


_ENGINE_DISPLAY = ENGINE_DISPLAY

#: DSB queries with no TPC-DS counterpart (q1-99 pair; above 99 are DSB's own
#: additions — the fact-to-fact join templates).
_UNPAIRED_DSB_SQL = (
    "(query_id LIKE 'dsb_%' AND CAST(REGEXP_EXTRACT(query_id, '(\\d+)$', 1) AS INTEGER) > 99)"
)


def _dsb_q100_spread(con: duckdb.DuckDBPyConnection) -> str:
    """Fastest vs slowest engine on dsb_q100 (the store_sales self-join) at 100 GB,
    designated tiers, as a clause — the widest single-query spread the unpaired
    DSB additions produce."""
    _star_schema_medians(con, 100)
    rows = con.execute(
        "SELECT platform, ms FROM _star_medians WHERE query_id = 'dsb_q100' ORDER BY ms"
    ).fetchall()
    if len(rows) < 2:
        raise NarrativeError("dsb_q100 needs at least two engines to compare")
    (fast_p, fast_ms), (slow_p, slow_ms) = rows[0], rows[-1]
    return (
        f"{_fmt_ms(float(fast_ms))} on {_ENGINE_DISPLAY.get(fast_p, fast_p)} against "
        f"{_fmt_ms(float(slow_ms))} on {_ENGINE_DISPLAY.get(slow_p, slow_p)}"
    )


def _dsb_unpaired_share_range(con: duckdb.DuckDBPyConnection) -> str:
    """Range of the share of each engine's DSB suite time carried by the three
    unpaired queries, at 100 GB designated tiers, as a clause."""
    _star_schema_medians(con, 100)
    rows = con.execute(f"""
        SELECT platform,
               100.0 * SUM(ms) FILTER (WHERE {_UNPAIRED_DSB_SQL}) / SUM(ms) AS pct
        FROM _star_medians WHERE query_id LIKE 'dsb_%'
        GROUP BY 1 HAVING pct IS NOT NULL ORDER BY pct
    """).fetchall()
    if len(rows) < 2:
        raise NarrativeError("unpaired-DSB share needs at least two engines")
    (lo_p, lo), (hi_p, hi) = rows[0], rows[-1]
    return (
        f"{lo:.0f}% of {_ENGINE_DISPLAY.get(lo_p, lo_p)}'s DSB suite time to "
        f"{hi:.0f}% of {_ENGINE_DISPLAY.get(hi_p, hi_p)}'s"
    )


def _join_split(con: duckdb.DuckDBPyConnection) -> tuple[int, int, dict[str, float]]:
    """(heavy n, light n, per-engine heavy/light geomean ratio) on the SF100
    common-complete set, split by the registry's ``complex_join`` tag (three or
    more participating relations)."""
    from ..queries.registry import QueryRegistry

    heavy_ids = {
        q.id
        for q in QueryRegistry().all()
        if q.block in ("tpcds", "dsb") and "complex_join" in q.patterns
    }
    _star_schema_medians(con, 100)
    rows = con.execute("""
        WITH common AS (
            SELECT query_id FROM _star_medians GROUP BY 1
            HAVING COUNT(DISTINCT platform) =
                   (SELECT COUNT(DISTINCT platform) FROM _star_medians)
        )
        SELECT platform, query_id, ms FROM _star_medians JOIN common USING (query_id)
    """).fetchall()
    per: dict[str, dict[str, float]] = {}
    for platform, query_id, ms in rows:
        per.setdefault(platform, {})[query_id] = float(ms)
    qids = {q for v in per.values() for q in v}
    heavy = qids & heavy_ids
    light = qids - heavy_ids

    def geomean(values: list[float]) -> float:
        return math.exp(sum(math.log(v) for v in values) / len(values))

    ratios = {
        p: geomean([v[q] for q in heavy]) / geomean([v[q] for q in light]) for p, v in per.items()
    }
    return len(heavy), len(light), ratios


def _join_heavy_n(con: duckdb.DuckDBPyConnection) -> str:
    return str(_join_split(con)[0])


def _join_light_n(con: duckdb.DuckDBPyConnection) -> str:
    return str(_join_split(con)[1])


def _join_split_ratio_range(con: duckdb.DuckDBPyConnection) -> str:
    ratios = _join_split(con)[2]
    return f"{min(ratios.values()):.2f}× to {max(ratios.values()):.2f}×"


# ── status-page facts ───────────────────────────────────────────────────────
#
# The published status page restates the report's headline figures in hand-written
# HTML, where a typed number drifts silently. These back the status template that
# `render-status --template <file>` renders (the operator's; none ships with the
# repository), so the summary of the report cannot disagree with the report. Each one
# is the same computation the corresponding verifier or section uses.


def _sf1000_full_ladder_speedup(con: duckdb.DuckDBPyConnection) -> str:
    """Speedup from the smallest to the largest Snowflake tier at 1 TB — 16× the
    compute. The headline "doubling does not halve" figure."""
    return f"{_sf_ladder_pair(con, 1000, 'X-Large (Gen2)')[0]:.2f}×"


def _clickbench_best(con: duckdb.DuckDBPyConnection, platform: str) -> ClickBenchConfig:
    """``platform``'s fastest measured single-session ClickBench configuration."""
    from .verifiers import _clickbench_geomeans

    rows = [c for c in _clickbench_geomeans(con) if c.platform == platform]
    if not rows:
        raise NarrativeError(f"no ClickBench data for {platform}")
    return min(rows, key=lambda c: c.geomean_ms)


def _cb_fact(platform: str, want: str) -> Callable[[duckdb.DuckDBPyConnection], str]:
    """Build a fact about one engine's fastest ClickBench configuration: its latency
    (``ms``), its tier's hourly list rate (``rate``), its suite cost (``suite``) or its
    cost per 1,000 queries (``per1k``)."""

    def fact(con: duckdb.DuckDBPyConnection) -> str:
        c = _clickbench_best(con, platform)
        if want == "ms":
            return f"{c.geomean_ms:.0f} ms"
        value = {"rate": c.usd_per_hour, "suite": c.suite_usd, "per1k": c.usd_per_1k}[want]
        if value is None:
            raise NarrativeError(f"{platform}'s fastest ClickBench tier has no {want}")
        return f"${value:,.4f}" if want == "suite" else f"${value:,.2f}"

    return fact


def _cb_price_multiple_min(con: duckdb.DuckDBPyConnection) -> str:
    """The smallest suite-cost multiple any engine faster than ClickHouse pays for its
    fastest ClickBench configuration, against ClickHouse's fastest, floored to a tenth
    so "at least N×" stays true. Priced on suite cost, as every cost comparison is."""
    from .verifiers import _clickbench_geomeans

    best: dict[str, ClickBenchConfig] = {}
    for c in _clickbench_geomeans(con):
        if c.platform not in best or c.geomean_ms < best[c.platform].geomean_ms:
            best[c.platform] = c
    ch = best["clickhouse"]
    if not ch.suite_usd:
        raise NarrativeError("clickhouse's fastest ClickBench tier has no suite cost")
    multiples = [
        c.suite_usd / ch.suite_usd
        for c in best.values()
        if c.geomean_ms < ch.geomean_ms and c.suite_usd is not None
    ]
    if not multiples:
        raise NarrativeError("no engine is faster than clickhouse on ClickBench")
    return f"{math.floor(min(multiples) * 10) / 10:.1f}×"


def _cb_replica_null_result(con: duckdb.DuckDBPyConnection) -> str:
    """The measured justification for the single-replica policy, as a clause:
    ClickHouse's one multi-replica warm ClickBench measurement against the same
    memory size at one replica. Reads the raw table deliberately — the policy-
    filtered views exclude the multi-replica side this fact exists to report.

    Both arms are pinned to rows created before 2026-08-05 (the 2026-08-04
    campaign), when they were measured side by side on the same provisioned
    service. Later 1x32GB rows come from fresh same-size replicas that ran up to 2x
    slower than that provisioning (see Part V), so a latest-wins comparison would
    attribute provisioning variance to the replica count: the confound this
    controlled pair exists to exclude. That date cutoff is also why this fact must NOT read
    ``results_latest``: the dedup there picks the newest row overall, which the
    cutoff then discards, emptying the 1x arm. Era-scoped slices dedup inside
    their own era.
    """
    rows = con.execute("""
        WITH d AS (
            SELECT * EXCLUDE (rn) FROM (
                SELECT r.*, ROW_NUMBER() OVER (
                    PARTITION BY r.platform, r.query_id, r.scale_factor, r.run_type,
                                 r.run_number, COALESCE(r.compute_label, '_')
                    ORDER BY r.created_at DESC) AS rn
                FROM main.sql_benchmark_results r
                WHERE r.block = 'clickbench' AND r.run_type = 'warm'
                  AND r.platform = 'clickhouse'
                  AND r.compute_label IN ('1x32GB', '2x32GB')
                  AND r.created_at < TIMESTAMP '2026-08-05'
            ) WHERE rn = 1
        ),
        per_q AS (
            SELECT compute_label, query_id, MEDIAN(server_time_ms) AS ms
            FROM d WHERE error_message IS NULL AND server_time_ms > 0
            GROUP BY 1, 2
        )
        SELECT compute_label, EXP(AVG(LN(ms))) FROM per_q GROUP BY 1
    """).fetchall()
    geo = {label: float(g) for label, g in rows}
    if set(geo) != {"1x32GB", "2x32GB"}:
        raise NarrativeError("need both 1x32GB and 2x32GB warm ClickBench measurements")
    return (
        f"a {geo['2x32GB']:,.0f} ms geomean on two replicas against {geo['1x32GB']:,.0f} ms on one"
    )


def _cb_cheapest_tier_duel(con: duckdb.DuckDBPyConnection) -> str:
    """The floor-of-the-market comparison, rendered as a clause: each engine's
    cheapest measured ClickBench pass (lowest suite cost), ClickHouse vs MotherDuck."""
    from .verifiers import _clickbench_geomeans, tier_display

    cheapest: dict[str, ClickBenchConfig] = {}
    for c in _clickbench_geomeans(con):
        if c.platform not in ("clickhouse", "motherduck") or c.suite_usd is None:
            continue
        if c.platform not in cheapest or c.suite_usd < cheapest[c.platform].suite_usd:
            cheapest[c.platform] = c
    if set(cheapest) != {"clickhouse", "motherduck"}:
        raise NarrativeError("missing a priced ClickBench pass for clickhouse or motherduck")
    ch, md = cheapest["clickhouse"], cheapest["motherduck"]
    return (
        f"ClickHouse's {tier_display(ch.label)} runs the suite for ${ch.suite_usd:,.4f} at a "
        f"{ch.geomean_ms:,.0f} ms geomean, against MotherDuck {tier_display(md.label)}'s "
        f"${md.suite_usd:,.4f} at {md.geomean_ms:,.0f} ms"
    )


def _redshift_sf1_inversion(con: duckdb.DuckDBPyConnection) -> tuple[float, float]:
    """(slowdown fraction, price multiple) of Redshift's largest 1 GB band vs 16 RPU.

    The over-provisioning finding: at small data the big cluster is slower AND dearer.
    """
    ladder = _ladder(con, "redshift", 1)
    _n, small_s, small_c = ladder["16 RPU"]
    _n2, big_s, big_c = ladder["128 RPU"]
    return (big_s / small_s) - 1.0, big_c / small_c


def _redshift_overprovision_pct(con: duckdb.DuckDBPyConnection) -> str:
    return f"{_redshift_sf1_inversion(con)[0] * 100:.0f}%"


def _redshift_overprovision_price(con: duckdb.DuckDBPyConnection) -> str:
    return f"{_redshift_sf1_inversion(con)[1]:.1f}×"


def _clickhouse_sf1000_completion(con: duckdb.DuckDBPyConnection) -> str:
    """ "69 of 201" — completed, not attempted. The attempted count (104) would
    overstate completion by half."""
    row = con.execute("""
        SELECT COUNT(DISTINCT query_id) FILTER (WHERE error_message IS NULL),
               (SELECT COUNT(DISTINCT query_id) FROM main.sql_benchmark_results
                WHERE block IN ('tpcds', 'dsb'))
        FROM main.sql_benchmark_results
        WHERE platform = 'clickhouse' AND scale_factor = 1000 AND run_type = 'warm'
          AND block IN ('tpcds', 'dsb')
    """).fetchone()
    return f"{int(row[0])} of {int(row[1])}"


def _clickhouse_sf1000_failure_mix(con: duckdb.DuckDBPyConnection) -> str:
    """Failure causes at 1 TB, separating the engine's limits from our own bug."""
    from ..analysis.concurrency_shape import client_cancel_predicate

    row = con.execute(f"""
        SELECT
          COUNT(DISTINCT query_id) FILTER (WHERE error_message LIKE '%code: 241%'),
          COUNT(DISTINCT query_id) FILTER (
            WHERE error_message ILIKE '%timeout%' OR error_message ILIKE '%timed out%'),
          COUNT(DISTINCT query_id) FILTER (
            WHERE error_message LIKE '%Could not set lock%' OR {client_cancel_predicate()})
        FROM main.sql_benchmark_results
        WHERE platform = 'clickhouse' AND scale_factor = 1000 AND run_type = 'warm'
          AND error_message IS NOT NULL
    """).fetchone()
    memory, timeout, ours = (int(v) for v in row)
    text = f"{memory} exceeded per-replica memory and {timeout} timed out"
    if ours:
        text += f"; {ours} failed on a bug in our own harness rather than the engine"
    return text


def _useful_concurrency_span(con: duckdb.DuckDBPyConnection) -> str:
    """ "100→5" — the same Redshift band's useful concurrency at 100 GB and at 1 TB.

    The capacity-planning finding, stated on the widest band so it is a floor rather
    than a cherry-pick.
    """
    from ..analysis.concurrency_shape import shapes

    at = {
        c.scale_factor: c.useful_concurrency
        for c in shapes(con)
        if c.platform == "redshift" and c.compute_label == "128 RPU" and c.publishable
    }
    if 100 not in at or 1000 not in at:
        raise NarrativeError("redshift 128 RPU is missing a 100 GB or 1 TB concurrency cell")
    return f"{at[100]}→{at[1000]}"


def _conc_error_shares(con: duckdb.DuckDBPyConnection) -> dict[str, float]:
    """Per-engine share of concurrency runs that did not complete, whole grid (all
    blocks, every scale and level), latest-wins dedup. The Part IV shed comparisons
    read these; typed copies go stale whenever a cell is re-measured, so they resolve
    from the database. Rows the harness failed (a query sent into a session it had
    already lost) are not the engine's and are left out, as in the grid."""
    from .views import install_views

    install_views(con)
    rows = con.execute("""
        SELECT platform,
               100.0 * count(*) FILTER (WHERE failure_class IS NOT NULL) / count(*)
        FROM concurrency_results
        WHERE failure_class IS DISTINCT FROM 'harness'
        GROUP BY 1
    """).fetchall()
    return {str(p): float(pct) for p, pct in rows}


def _conc_shed_bigquery(con: duckdb.DuckDBPyConnection) -> str:
    return f"{_conc_error_shares(con)['bigquery']:.2f}%"


def _conc_shed_redshift(con: duckdb.DuckDBPyConnection) -> str:
    return f"{_conc_error_shares(con)['redshift']:.2f}%"


def _conc_shed_fabric(con: duckdb.DuckDBPyConnection) -> str:
    return f"{_conc_error_shares(con)['fabric']:.2f}%"


def _conc_shed_next_best(con: duckdb.DuckDBPyConnection) -> str:
    return f"{sorted(_conc_error_shares(con).values())[1]:.2f}%"


def _conc_shed_worst(con: duckdb.DuckDBPyConnection) -> str:
    return f"{max(_conc_error_shares(con).values()):.1f}%"


def _conc_shed_databricks_vs_bigquery(con: duckdb.DuckDBPyConnection) -> str:
    shares = _conc_error_shares(con)
    return f"{shares['databricks'] / shares['bigquery']:.0f}×"


def _conc_cells_counts(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    from ..analysis.concurrency_shape import shapes

    cells = [c for c in shapes(con) if c.publishable]
    return sum(1 for c in cells if c.censored), len(cells)


def _conc_cells_censored(con: duckdb.DuckDBPyConnection) -> str:
    return str(_conc_cells_counts(con)[0])


def _conc_cells_total(con: duckdb.DuckDBPyConnection) -> str:
    return str(_conc_cells_counts(con)[1])


def _bq_sf1000_200_800_speedup(con: duckdb.DuckDBPyConnection) -> str:
    """BigQuery's 200→800-slot suite-time speedup at 1 TB, on the gated ladder
    tables' methodology (per-query median server time, summed per reservation over
    its own completed set). Reads ``results_latest``
    so a superseded re-run cannot blend into the per-query medians."""
    from .views import install_views

    install_views(con)
    row = con.execute("""
        WITH pq AS (
            SELECT compute_label, query_id, MEDIAN(server_time_ms) AS ms
            FROM results_latest
            WHERE platform = 'bigquery' AND scale_factor = 1000 AND run_type = 'warm'
              AND block IN ('tpcds', 'dsb')
              AND error_message IS NULL AND server_time_ms > 0
              AND compute_label IN ('ENTERPRISE / 200 slots', 'ENTERPRISE / 800 slots')
            GROUP BY 1, 2
        )
        SELECT SUM(CAST(ms AS DECIMAL(28,12))) FILTER (WHERE compute_label LIKE '%/ 200%')
             / SUM(CAST(ms AS DECIMAL(28,12))) FILTER (WHERE compute_label LIKE '%/ 800%')
        FROM pq
    """).fetchone()
    if row is None or row[0] is None:
        raise NarrativeError("bigquery SF1000 200/800-slot ladder data missing")
    return f"{float(row[0]):.2f}×"


def _scaling_step_all_engines_range(con: duckdb.DuckDBPyConnection) -> str:
    """The min→max adjacent-scale suite-time multiplier across EVERY engine at its
    designated tiers — the "across all engines measured" span in Part II's summary,
    which includes the engines the gated scaling table deliberately omits (their
    steps carry a tier jump, a billing-model switch, or a survivor-set change; the
    prose cites the span as a bound, not a comparison). Anchored to the same
    computation as the ``scaling_at_designated_tier`` verifier."""
    from .verifiers import _suite_seconds_at_designated_tier

    suite = _suite_seconds_at_designated_tier(con)
    scales = sorted({sf for _p, sf in suite})
    steps = [
        suite[(plat, b)] / suite[(plat, a)]
        for plat in {p for p, _sf in suite}
        for a, b in itertools.pairwise(scales)
        if (plat, a) in suite and (plat, b) in suite
    ]
    if not steps:
        raise NarrativeError("no designated-tier scaling steps found")
    return f"{min(steps):.1f}× to {max(steps):.1f}×"


def _verified_tables(con: duckdb.DuckDBPyConnection) -> str:
    """How many hand-authored tables the drift gate recomputes.

    A count the report makes a claim about, so it resolves from the markers actually
    present rather than being typed — the failure mode being a sentence that says
    "every table" after someone removes a marker.
    """
    from .figures import load_tables

    del con
    return str(len(load_tables()))


def _scale_count(con: duckdb.DuckDBPyConnection) -> str:
    n = _scalar(con, "SELECT count(DISTINCT scale_factor) FROM main.sql_benchmark_results")
    return str(n)


def _scale_factors(con: duckdb.DuckDBPyConnection) -> str:
    rows = con.execute(
        "SELECT DISTINCT scale_factor FROM main.sql_benchmark_results ORDER BY scale_factor"
    ).fetchall()
    return "/".join(f"SF{int(r[0])}" for r in rows)


FACTS: dict[str, Callable[[duckdb.DuckDBPyConnection], str]] = {
    "engines_with_data": _engines_with_data,
    "total_result_rows": _total_rows,
    "distinct_queries": _distinct_queries,
    "concurrency_levels": _concurrency_levels,
    "cold_row_count": _cold_row_count,
    "fabric_row_count": _fabric_row_count,
    "scale_factors": _scale_factors,
    "row_count_agreement_cells": _row_count_cells,
    "row_count_divergences": _row_count_divergences,
    "row_count_discriminating": _row_count_discriminating,
    "sf1000_large_speedup": _sf1000_large_speedup,
    "sf1000_large_money": _sf1000_large_money,
    "sf1000_large_price_per_speed": _sf1000_large_price_per_speed,
    "sf1000_large_cost": _sf1000_large_cost,
    "sf1000_xsmall_cost": _sf1000_xsmall_cost,
    "sf100_xlarge_money": _sf100_xlarge_money,
    "sf100_xlarge_cost_per_speedup": _sf100_xlarge_cost_per_speedup,
    "sf1000_best_single_gain": _sf1000_best_single_gain,
    "concurrency_cells": _concurrency_cells,
    "concurrency_mode_mix": _concurrency_mode_mix,
    "clickhouse_star_trajectory": _clickhouse_star_trajectory,
    "cross_engine_common_n": _cross_engine_common_n,
    "skew_clean_ratio_range": _skew_clean_ratio_range,
    "skew_bigquery_clean_ratio": _skew_bigquery_clean_ratio,
    "join_heavy_n": _join_heavy_n,
    "join_light_n": _join_light_n,
    "join_split_ratio_range": _join_split_ratio_range,
    "dsb_q100_spread": _dsb_q100_spread,
    "dsb_unpaired_share_range": _dsb_unpaired_share_range,
    "cb_price_multiple_min": _cb_price_multiple_min,
    "cb_replica_null_result": _cb_replica_null_result,
    "cb_cheapest_tier_duel": _cb_cheapest_tier_duel,
    "sf1000_full_ladder_speedup": _sf1000_full_ladder_speedup,
    "cb_clickhouse_ms": _cb_fact("clickhouse", "ms"),
    "cb_clickhouse_rate": _cb_fact("clickhouse", "rate"),
    "cb_bigquery_ms": _cb_fact("bigquery", "ms"),
    "cb_bigquery_rate": _cb_fact("bigquery", "rate"),
    "cb_databricks_ms": _cb_fact("databricks", "ms"),
    "cb_databricks_rate": _cb_fact("databricks", "rate"),
    "cb_fabric_ms": _cb_fact("fabric", "ms"),
    "cb_fabric_rate": _cb_fact("fabric", "rate"),
    "cb_clickhouse_suite": _cb_fact("clickhouse", "suite"),
    "cb_bigquery_suite": _cb_fact("bigquery", "suite"),
    "cb_databricks_suite": _cb_fact("databricks", "suite"),
    "cb_fabric_suite": _cb_fact("fabric", "suite"),
    "cb_motherduck_suite": _cb_fact("motherduck", "suite"),
    "cb_redshift_suite": _cb_fact("redshift", "suite"),
    "cb_snowflake_suite": _cb_fact("snowflake", "suite"),
    "redshift_overprovision_pct": _redshift_overprovision_pct,
    "redshift_overprovision_price": _redshift_overprovision_price,
    "clickhouse_sf1000_completion": _clickhouse_sf1000_completion,
    "clickhouse_sf1000_failure_mix": _clickhouse_sf1000_failure_mix,
    "useful_concurrency_span": _useful_concurrency_span,
    "scale_count": _scale_count,
    "verified_tables": _verified_tables,
    "conc_shed_bigquery": _conc_shed_bigquery,
    "conc_shed_fabric": _conc_shed_fabric,
    "conc_shed_next_best": _conc_shed_next_best,
    "conc_shed_redshift": _conc_shed_redshift,
    "conc_shed_worst": _conc_shed_worst,
    "conc_shed_databricks_vs_bigquery": _conc_shed_databricks_vs_bigquery,
    "conc_cells_censored": _conc_cells_censored,
    "conc_cells_total": _conc_cells_total,
    "bq_sf1000_200_800_speedup": _bq_sf1000_200_800_speedup,
    "scaling_step_all_engines_range": _scaling_step_all_engines_range,
}


class NarrativeError(RuntimeError):
    """An authored section is malformed, or references a fact that cannot resolve."""


def load_sections(sections_dir: Path = DEFAULT_SECTIONS_DIR) -> list[Section]:
    """Read authored prose files, in filename order.

    Args:
        sections_dir: Directory of ``*.md`` files, each declaring an anchor.

    Returns:
        Sections in filename order (so a ``01-`` prefix sorts before ``02-``).

    Raises:
        NarrativeError: A file declares no anchor, or an unknown one. Silently
            dropping an authored section would be the worst possible failure —
            the prose would vanish from the report with no signal.
    """
    if not sections_dir.exists():
        return []
    out: list[Section] = []
    for path in sorted(sections_dir.glob("*.md")):
        body = path.read_text(encoding="utf-8")
        match = _ANCHOR_RE.search(body)
        if match is None:
            raise NarrativeError(
                f"{path} declares no anchor. Add a header comment, e.g. "
                f"'<!-- anchor: before-methodology -->'. Valid anchors: {', '.join(ANCHORS)}"
            )
        anchor = match.group(1).lower()
        if anchor not in ANCHORS:
            raise NarrativeError(
                f"{path} declares unknown anchor {anchor!r}. Valid anchors: {', '.join(ANCHORS)}"
            )
        out.append(Section(path, anchor, _ANCHOR_RE.sub("", body, count=1).strip()))
    return out


def resolve_facts(text: str, con: duckdb.DuckDBPyConnection, *, source: Path | None = None) -> str:
    """Substitute every ``{{fact:name}}`` in ``text`` from the results DB.

    Args:
        text: Authored prose.
        con: Open read-only connection to the results DB.
        source: File the text came from, for error messages.

    Returns:
        The prose with every placeholder replaced.

    Raises:
        NarrativeError: An unknown fact name, or one that computes to ``None``.
            Both fail loudly by design — a published report containing a literal
            ``{{fact:...}}``, or a number that quietly went stale, is worse than a
            build that stops.
    """
    from .views import install_views

    # Facts read the shared deduplicated views; `render-status` (and any other
    # standalone caller) arrives on a fresh connection, so install lazily here.
    install_views(con)
    where = f" in {source}" if source else ""

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1).lower()
        fn = FACTS.get(name)
        if fn is None:
            raise NarrativeError(
                f"unknown fact {name!r}{where}. Known facts: {', '.join(sorted(FACTS))}"
            )
        try:
            value = fn(con)
        except Exception as exc:  # a broken fact must name itself, not vanish
            raise NarrativeError(f"fact {name!r}{where} failed to compute: {exc}") from exc
        if value is None or value == "":
            raise NarrativeError(f"fact {name!r}{where} resolved to nothing")
        return str(value)

    return _FACT_RE.sub(_sub, text)


def compose(
    generated: list[str],
    con: duckdb.DuckDBPyConnection,
    *,
    anchor_positions: dict[str, int],
    sections_dir: Path = DEFAULT_SECTIONS_DIR,
) -> list[str]:
    """Splice authored prose into the generated section list.

    Args:
        generated: Lines the generator produced.
        con: Open read-only results DB connection, for fact resolution.
        anchor_positions: Anchor name -> index in ``generated`` to insert before.
            Anchors absent from this mapping are appended at the end, so a section
            is never silently dropped because the generator's shape changed.
        sections_dir: Where authored prose lives.

    Returns:
        The merged line list.
    """
    sections = load_sections(sections_dir)
    if not sections:
        return generated

    by_anchor: dict[str, list[str]] = {}
    for sec in sections:
        rendered = resolve_facts(sec.body, con, source=sec.path)
        by_anchor.setdefault(sec.anchor, []).extend(["", rendered, ""])
        logger.info("composed narrative section %s at anchor %s", sec.path.name, sec.anchor)

    out: list[str] = []
    if "before-everything" in by_anchor:
        out.extend(by_anchor.pop("before-everything"))

    # Walk the generated lines, inserting at each mapped anchor position.
    #
    # Several anchors can resolve to the SAME line index — "after-front-matter" and
    # "before-decision-support" are adjacent, so they collide whenever nothing sits
    # between them. A dict keyed on position would silently keep one and push the
    # rest to the end of the report. Group by position and emit in ANCHORS
    # declaration order, which is document order by definition.
    inserts: dict[int, list[str]] = {}
    for name in ANCHORS:
        pos = anchor_positions.get(name)
        if pos is not None and name in by_anchor:
            inserts.setdefault(pos, []).append(name)

    for i, line in enumerate(generated):
        for name in inserts.get(i, ()):
            out.extend(by_anchor.pop(name))
        out.append(line)

    for name in list(by_anchor):
        if name != "after-everything":
            logger.warning(
                "anchor %r had no position in the generated report; appending at the end", name
            )
            out.extend(by_anchor.pop(name))
    out.extend(by_anchor.pop("after-everything", []))
    return out

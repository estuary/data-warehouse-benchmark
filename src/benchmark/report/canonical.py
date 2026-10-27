"""Canonical compute tier per (platform, scale) — the apples-to-apples basis.

``run_plan.yaml`` declares each engine's designated sizing target per scale.
This module maps those targets onto the ``compute_label`` strings the runners
stamp on result rows, so the report's primary sections compare engines at
their **designated** tier rather than at whichever size happened to run most
recently. (Once size sweeps exist, a label-agnostic latest-wins view picks
arbitrary tiers, e.g. Snowflake's X-Large sweep rows against ClickHouse's
run-plan tier.)

Resolution is data-aware: a designated label is only enforced when rows with
that label actually exist for the (platform, scale), and BigQuery slot targets
with no exact match resolve to the nearest swept slot count (annotated).

.. note:: **NULL ``compute_label`` rows are not admitted as canonical.** A NULL
   label is a capture failure, not an era marker; admitting it would perform a
   silent size inference inside a ``WHERE`` clause and let the same row land in a
   non-size-aware aggregate while being absent from the size-aware one. Rows whose
   label can be reconstructed from run-time telemetry are repaired explicitly and
   carry a ``metadata.compute_label_was_null`` provenance marker (see
   ``analysis/recost.py``); any remaining NULL label falls out of the canonical
   view loudly rather than being absorbed silently.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ..run_plan import RunPlan

if TYPE_CHECKING:
    import duckdb

logger = logging.getLogger("benchmark.report.canonical")

_SF_DISPLAY = {
    "XSMALL": "X-Small",
    "SMALL": "Small",
    "MEDIUM": "Medium",
    "LARGE": "Large",
    "XLARGE": "X-Large",
}

# ClickHouse pins replica MEMORY out-of-band (the run-plan loader allows one
# sizing knob per engine, and num_replicas holds it) — see run_plan.yaml's
# comment block for the same numbers.
_CLICKHOUSE_PINNED_GB = {1: 16, 10: 32, 100: 32, 1000: 120}


def _designated_label(platform: str, scale: int, target: str | int, plan: RunPlan) -> str | None:
    """Format one run-plan target as the compute_label the runners stamp."""
    if platform == "snowflake":
        size = _SF_DISPLAY.get(str(target).upper())
        return f"{size} (Gen2)" if size else None
    if platform == "redshift":
        return f"{target} RPU"
    if platform == "motherduck":
        return str(target).capitalize()
    if platform == "clickhouse":
        mem = _CLICKHOUSE_PINNED_GB.get(scale)
        return f"{int(target)}x{mem}GB" if mem else None
    if platform == "bigquery":
        on_demand = plan.engines["bigquery"].policies.get("on_demand", {})
        if on_demand.get(scale) == "allow" and scale <= 10:
            # low scales ran on-demand by design; the reservation is a
            # big-scale guardrail
            return "on-demand"
        return f"ENTERPRISE / {target} slots"
    if platform == "databricks":
        return str(target)
    if platform == "fabric":
        return str(target)
    return None


def resolve_canonical(
    con: duckdb.DuckDBPyConnection,
) -> dict[tuple[str, int, str], str]:
    """(platform, scale, run_class) -> canonical compute_label, resolved
    against the rows that actually exist (run_class: 'warm' covers warm+cold;
    'concurrency' is separate because concurrency designs may pin different
    compute — e.g. ClickHouse ran concurrency at 3 replicas constant while its
    warm tiers use run-plan replica counts). Entries are only returned where
    enforcement is safe: the designated label (or a nearest-slots fallback for
    BigQuery) is present in that run class's data."""
    plan = RunPlan.load()
    present: dict[tuple[str, int, str], list[str]] = {}
    for plat, sf, cls, label in con.execute(
        "SELECT DISTINCT platform, scale_factor,"
        " CASE WHEN run_type = 'concurrency' THEN 'concurrency' ELSE 'warm' END,"
        " compute_label"
        " FROM main.sql_benchmark_results WHERE compute_label IS NOT NULL"
    ).fetchall():
        present.setdefault((plat, int(sf), cls), []).append(label)

    out: dict[tuple[str, int, str], str] = {}
    for platform, eng in plan.engines.items():
        for scale, target in eng.sizes.items():
            for cls in ("warm", "concurrency"):
                want = _designated_label(platform, int(scale), target, plan)
                if platform == "clickhouse":
                    # concurrency ran 3 replicas CONSTANT (size was the swept
                    # axis); its canonical concurrency tier is 3 x pinned GB.
                    mem = _CLICKHOUSE_PINNED_GB.get(int(scale))
                    if cls == "concurrency" and mem:
                        want = f"3x{mem}GB"
                if want is None:
                    continue
                have = present.get((platform, int(scale), cls), [])
                if want in have:
                    out[(platform, int(scale), cls)] = want
                    continue
                if platform == "bigquery" and have:
                    # nearest swept slot count to the run-plan target (e.g.
                    # plan says 500, sweep ran 200..1000 in steps of 200 -> 400).
                    # Only meaningful when the TARGET itself is a slot count: at
                    # SF1/SF10 the designated compute is "on-demand" by policy, and
                    # once a slot sweep also ran at those scales `have` is non-empty
                    # while `want` is not a slots label — there is no nearest
                    # reservation to an unreserved target, so leave the cell
                    # unenforced rather than inventing one.
                    m = re.match(r"ENTERPRISE / (\d+) slots", want)
                    if m is None:
                        continue
                    slots = [
                        (abs(int(g.group(1)) - int(m.group(1))), g.group(0))
                        for g in (re.match(r"ENTERPRISE / (\d+) slots", h) for h in have)
                        if g
                    ]
                    if slots:
                        nearest = min(slots)[1]
                        logger.info(
                            "bigquery SF%s/%s: run-plan %s not present; using nearest %s",
                            scale,
                            cls,
                            want,
                            nearest,
                        )
                        out[(platform, int(scale), cls)] = nearest
    return out


def create_canonical_table(
    con: duckdb.DuckDBPyConnection,
) -> dict[tuple[str, int, str], str]:
    """Materialize the canonical map as temp table ``_canonical_tier`` and
    return it. The ``bench`` view joins against this on (platform, scale,
    run_class) to keep only designated-tier rows (NULL labels are not admitted;
    see module docstring)."""
    mapping = resolve_canonical(con)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _canonical_tier"
        " (platform VARCHAR, scale_factor INTEGER, run_class VARCHAR, label VARCHAR)"
    )
    # DuckDB's executemany raises on an empty parameter list, and the mapping
    # IS empty on a results DB with no tiered compute recorded (fresh DBs, the
    # e2e integration test) — the empty temp table is the correct output there.
    if mapping:
        con.executemany(
            "INSERT INTO _canonical_tier VALUES (?, ?, ?, ?)",
            [(p, s, cls, label) for (p, s, cls), label in mapping.items()],
        )
    return mapping

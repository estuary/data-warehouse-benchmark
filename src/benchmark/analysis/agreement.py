"""Cross-engine row-count agreement — the correctness evidence above SF1.

`validate-sf1` compares full result sets against the DuckDB oracle, but only at
SF1: hashing a 1 TB result locally is not tractable. That leaves the report's
weakest claim — correctness is *verified* at ~1 GB and merely *assumed* above it.

This module closes part of that gap from data already recorded. Every measured
row carries `row_count`, so for each (query, scale) we can ask whether all seven
engines returned the **same number of rows**. That is a genuine cross-check with
no oracle required: seven independent engines, six independent dialect
translations plus MotherDuck's canonical DuckDB SQL, one byte-identical ingest
path. Divergence means at least one engine is wrong. Agreement does not prove
correctness — two engines can return 100 rows of different values — so the
strength of the evidence is published alongside it (see `discriminating` below).

**Scope: decision-support blocks only** (TPC-DS + DSB). ClickBench is excluded
on purpose: its 43 queries are the ClickBench project's per-engine variants, so the
engines are not running equivalent SQL and a row-count difference there would
carry no correctness meaning. See `docs/ARCHITECTURE.md`.

Two dimensions are pooled deliberately:

- **Compute size.** A query's row count must not depend on warehouse size, so
  every size an engine ran contributes, and a within-engine disagreement across
  sizes is itself a finding (`unstable_engines`).
- **Run number.** Same argument across the 3 measured runs.

Concurrency rows are excluded — they execute the same SQL, but the pass exists
to measure queuing and its error rows are load-shed, which would mix a
completion signal into a correctness one.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
from pydantic import BaseModel, Field

logger = logging.getLogger("benchmark.analysis.agreement")

DECISION_SUPPORT_BLOCKS = ("tpcds", "dsb")

# The TPC-DS/DSB templates overwhelmingly end in `LIMIT 100`. A cell whose
# agreed row count is exactly the cap is weak evidence — every engine returns
# 100 rows whether or not they are the *same* 100 rows. Cells that agree on any
# other value are `discriminating`: the count is a property of the data, so
# agreement there is real (if partial) evidence the engines computed the same
# result. Published as a strength qualifier rather than used as a filter.
_LIMIT_CAP = 100


class EngineCount(BaseModel):
    """One engine's row count for a (query, scale) cell."""

    platform: str
    row_count: int
    runs: int = Field(description="measured rows pooled into this count")
    distinct_counts: int = Field(
        description="distinct row_count values this engine produced across its "
        "runs and compute sizes; >1 means the engine is unstable on this query"
    )


class Divergence(BaseModel):
    """A (query, scale) where engines did not agree on the row count.

    This is the output that matters. An empty divergence list is the finding;
    a non-empty one is a correctness defect in at least one engine (or in our
    translation of one query for one engine) and must be investigated before
    publication, never summarized away.
    """

    block: str
    query_id: str
    scale_factor: int
    engines: list[EngineCount]

    def describe(self) -> str:
        """One-line rendering for logs and the report table."""
        return ", ".join(f"{e.platform}={e.row_count}" for e in self.engines)


class AgreementSummary(BaseModel):
    """Agreement rolled up to one (scale, block) cell."""

    scale_factor: int
    block: str
    queries: int = Field(description="(query, scale) cells with >=2 engines reporting")
    engines_min: int
    engines_max: int
    unanimous: int
    divergent: int
    discriminating: int = Field(
        description="unanimous cells whose agreed row count is not the LIMIT 100 "
        "cap — i.e. where agreement carries real information"
    )
    unstable_cells: int = Field(
        description="cells where some engine disagreed with ITSELF across runs or compute sizes"
    )


class MissingEngine(BaseModel):
    """How many cells an engine is absent from, per scale.

    Unanimity among fewer engines is weaker than among all seven, and the absences are
    load-correlated (ClickHouse's 1 TB memory ceiling), so this is published
    beside the summary rather than left implicit in `engines_min`.
    """

    scale_factor: int
    platform: str
    missing_cells: int


class AgreementReport(BaseModel):
    """The full artifact: rollup, absences, and every divergence."""

    summary: list[AgreementSummary]
    missing: list[MissingEngine]
    divergences: list[Divergence]

    @property
    def total_cells(self) -> int:
        return sum(s.queries for s in self.summary)

    @property
    def total_divergent(self) -> int:
        return sum(s.divergent for s in self.summary)

    @property
    def total_discriminating(self) -> int:
        return sum(s.discriminating for s in self.summary)


def _create_views(con: duckdb.DuckDBPyConnection) -> None:
    """Latest-wins per-engine row count per (block, query, scale).

    Built on the shared ``results_latest`` view, whose dedup key includes
    `compute_label`, so a size sweep contributes one row per size rather than
    the sweep's last size winning the whole cell. Not tier- or policy-filtered
    on purpose: a row count must not depend on compute size, so every measured
    configuration is admissible evidence here.
    """
    from ..report.views import install_views

    install_views(con)
    blocks = ", ".join(f"'{b}'" for b in DECISION_SUPPORT_BLOCKS)
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW _agree_rows AS
        SELECT * FROM results_latest r
        WHERE r.run_type = 'warm'
          AND r.error_message IS NULL
          AND r.row_count IS NOT NULL
          AND r.block IN ({blocks})
    """)
    con.execute("""
        CREATE OR REPLACE TEMP VIEW _agree_engine AS
        SELECT block, query_id, scale_factor, platform,
               MODE(row_count) AS row_count,
               COUNT(*) AS runs,
               COUNT(DISTINCT row_count) AS distinct_counts
        FROM _agree_rows
        GROUP BY 1, 2, 3, 4
    """)


def row_count_agreement(
    db_path: Path = Path("results/benchmark.duckdb"),
) -> AgreementReport:
    """Compute the cross-engine row-count agreement report from a DB path."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return agreement_on(con)
    finally:
        con.close()


def agreement_on(con: duckdb.DuckDBPyConnection) -> AgreementReport:
    """Compute the report on an already-open connection.

    Separate from `row_count_agreement` so the report generator and the
    `{{fact:...}}` resolver can reuse their existing read-only connection
    instead of opening a second one against the same file.
    """
    _create_views(con)

    summary_sql = f"""
        WITH per_cell AS (
            SELECT block, query_id, scale_factor,
                   COUNT(*) AS engines,
                   COUNT(DISTINCT row_count) AS distinct_counts,
                   MODE(row_count) AS agreed,
                   MAX(CASE WHEN distinct_counts > 1 THEN 1 ELSE 0 END) AS unstable
            FROM _agree_engine
            GROUP BY 1, 2, 3
            HAVING COUNT(*) >= 2
        )
        SELECT scale_factor, block,
               COUNT(*),
               MIN(engines), MAX(engines),
               SUM(CASE WHEN distinct_counts = 1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN distinct_counts > 1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN distinct_counts = 1 AND agreed <> {_LIMIT_CAP} THEN 1 ELSE 0 END),
               SUM(unstable)
        FROM per_cell
        GROUP BY 1, 2
        ORDER BY 1, 2
    """
    summary = [
        AgreementSummary(
            scale_factor=int(sf),
            block=block,
            queries=int(queries),
            engines_min=int(emin),
            engines_max=int(emax),
            unanimous=int(unanimous),
            divergent=int(divergent),
            discriminating=int(discriminating),
            unstable_cells=int(unstable),
        )
        for sf, block, queries, emin, emax, unanimous, divergent, discriminating, unstable in (
            con.execute(summary_sql).fetchall()
        )
    ]

    missing = [
        MissingEngine(scale_factor=int(sf), platform=plat, missing_cells=int(n))
        for sf, plat, n in con.execute("""
            WITH engines AS (SELECT DISTINCT platform FROM _agree_engine),
                 cells AS (SELECT DISTINCT block, query_id, scale_factor FROM _agree_engine)
            SELECT c.scale_factor, e.platform, COUNT(*)
            FROM cells c
            CROSS JOIN engines e
            LEFT JOIN _agree_engine a
                   ON a.block = c.block AND a.query_id = c.query_id
                  AND a.scale_factor = c.scale_factor AND a.platform = e.platform
            WHERE a.platform IS NULL
            GROUP BY 1, 2
            ORDER BY 1, 2
        """).fetchall()
    ]

    divergent_keys = con.execute("""
        SELECT block, query_id, scale_factor
        FROM _agree_engine
        GROUP BY 1, 2, 3
        HAVING COUNT(*) >= 2 AND COUNT(DISTINCT row_count) > 1
        ORDER BY scale_factor, block, query_id
    """).fetchall()

    divergences: list[Divergence] = []
    for block, query_id, sf in divergent_keys:
        engines = [
            EngineCount(platform=plat, row_count=int(rc), runs=int(runs), distinct_counts=int(dc))
            for plat, rc, runs, dc in con.execute(
                """
                SELECT platform, row_count, runs, distinct_counts
                FROM _agree_engine
                WHERE block = ? AND query_id = ? AND scale_factor = ?
                ORDER BY platform
                """,
                [block, query_id, sf],
            ).fetchall()
        ]
        divergences.append(
            Divergence(block=block, query_id=query_id, scale_factor=int(sf), engines=engines)
        )

    report = AgreementReport(summary=summary, missing=missing, divergences=divergences)
    # Divergence is a correctness defect, not a statistic: log every one at
    # WARNING so a run that introduces one is impossible to miss in the audit
    # trail, and never collapse them into a count.
    for div in report.divergences:
        logger.warning(
            "row-count divergence: %s at SF%s — %s",
            div.query_id,
            div.scale_factor,
            div.describe(),
            extra={"query_id": div.query_id, "scale_factor": div.scale_factor},
        )
    logger.info(
        "row-count agreement: %d cells, %d divergent, %d discriminating",
        report.total_cells,
        report.total_divergent,
        report.total_discriminating,
    )
    return report


def emit_row_count_agreement(
    db_path: Path = Path("results/benchmark.duckdb"),
    output_path: Path = Path("results/exports/row_count_agreement.parquet"),
) -> Path:
    """Emit the per-engine row counts as Parquet so a reader can recompute the
    agreement table under their own key."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        _create_views(con)
        con.execute(
            f"COPY (SELECT * FROM _agree_engine ORDER BY scale_factor, block, query_id, platform)"
            f" TO '{output_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    logger.info("row-count agreement emitted to %s", output_path)
    return output_path

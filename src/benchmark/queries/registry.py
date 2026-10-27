"""Query registry — loads the 244 canonical queries from disk.

`QueryRegistry` enumerates the 244-query suite (99 TPC-DS + 102 DSB + 43
ClickBench) and infers pattern tags. The registry skips (with a warning) any
block whose directory hasn't been populated, e.g. before the ClickBench queries
are downloaded (``benchmark.datagen.clickbench``).

Files are loaded from <repo_root>/queries/<dialect>/<block>/query_NN.sql,
where dialect is "duckdb" by default (the canonical source-of-truth dialect).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("benchmark.queries.registry")


# Repo root resolves to two dirs up from this file: src/benchmark/queries -> repo
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
QUERIES_ROOT = REPO_ROOT / "queries"


@dataclass(frozen=True)
class BenchmarkQuery:
    id: str  # e.g. "tpcds_q01"
    block: str  # "tpcds" | "dsb" | "clickbench"
    number: int
    sql: str
    patterns: tuple[str, ...]


# A comma-separated relation list — "FROM store_sales, date_dim, item" — is an
# implicit inner join and is how TPC-DS and DSB overwhelmingly express joins.
# Counting only the JOIN keyword misses them entirely.
_FROM_CLAUSE_RE = re.compile(
    r"\bFROM\b(.*?)(?=\bWHERE\b|\bGROUP\b|\bORDER\b|\bHAVING\b|$)", re.I | re.S
)

# 3+ participating relations = "complex join".
COMPLEX_JOIN_MIN_RELATIONS = 3


def _relation_count(upper: str) -> int:
    """Rough count of relations joined in the largest FROM clause.

    Counts comma-separated entries plus explicit ``JOIN`` keywords. Deliberately
    crude (nested subqueries inflate it slightly), but it counts a seven-table
    star-schema query written in the comma style as seven relations, not one.
    """
    best = 0
    for match in _FROM_CLAUSE_RE.finditer(upper):
        clause = match.group(1)
        # Ignore commas inside parentheses (function args, IN-lists, subqueries).
        depth, commas = 0, 0
        for ch in clause:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                commas += 1
        best = max(best, commas + 1 + clause.count("JOIN"))
    return best


def _infer_patterns(sql: str) -> tuple[str, ...]:
    """Tag a query with coarse pattern labels.

    Tags are heuristic — meant for slicing the report, not for correctness.

    .. note:: ``complex_join`` counts **participating relations**, never ``JOIN``
       keywords: TPC-DS and DSB write joins as comma-separated relation lists, so
       a seven-table star-schema query containing no ``JOIN`` keyword would score
       zero under a keyword test (which would tag ~10 of 201 decision-support
       queries as join-heavy when 160 join three or more tables).
    """
    patterns: list[str] = []
    upper = sql.upper()
    if "OVER (" in upper or "OVER(" in upper:
        patterns.append("window")
    if "WITH RECURSIVE" in upper:
        patterns.append("recursive_cte")
    if "LATERAL" in upper or "CROSS JOIN" in upper:
        patterns.append("lateral_join")
    if "LIKE" in upper or "SUBSTRING" in upper:
        patterns.append("text_search")
    if _relation_count(upper) >= COMPLEX_JOIN_MIN_RELATIONS:
        patterns.append("complex_join")
    if "GROUP BY" in upper:
        patterns.append("aggregation")
    if upper.count("SELECT") >= 3:
        patterns.append("subquery")
    if "UNION" in upper or "INTERSECT" in upper or "EXCEPT" in upper:
        patterns.append("set_op")
    return tuple(patterns)


class QueryRegistry:
    """Loads canonical benchmark queries from disk and groups them by block."""

    BLOCKS: tuple[tuple[str, int], ...] = (
        ("tpcds", 99),
        ("dsb", 102),
        ("clickbench", 43),
    )

    def __init__(self, dialect: str = "duckdb") -> None:
        self.dialect = dialect
        self.queries: dict[str, BenchmarkQuery] = {}
        self._load()

    def _load(self) -> None:
        for block, expected_count in self.BLOCKS:
            block_dir = QUERIES_ROOT / self.dialect / block
            if not block_dir.exists():
                logger.warning(
                    "queries directory missing, skipping",
                    extra={"block": block, "path": str(block_dir)},
                )
                continue
            files = sorted(block_dir.glob("query_*.sql"))
            for path in files:
                # query_07.sql -> 7; query_100.sql -> 100
                stem = path.stem.replace("query_", "")
                try:
                    number = int(stem)
                except ValueError:
                    logger.warning(
                        "unparseable query filename, skipping", extra={"path": str(path)}
                    )
                    continue
                sql = path.read_text(encoding="utf-8")
                qid = f"{block}_q{number:02d}" if number < 100 else f"{block}_q{number}"
                self.queries[qid] = BenchmarkQuery(
                    id=qid,
                    block=block,
                    number=number,
                    sql=sql,
                    patterns=_infer_patterns(sql),
                )
            logger.info(
                "loaded queries for block",
                extra={
                    "block": block,
                    "loaded": len(files),
                    "expected": expected_count,
                },
            )

        logger.info("query registry ready", extra={"total": len(self.queries)})

    def all(self) -> list[BenchmarkQuery]:
        return sorted(self.queries.values(), key=lambda q: (q.block, q.number))

    def by_block(self, block: str) -> list[BenchmarkQuery]:
        return [q for q in self.all() if q.block == block]

    def get(self, query_id: str) -> BenchmarkQuery | None:
        return self.queries.get(query_id)

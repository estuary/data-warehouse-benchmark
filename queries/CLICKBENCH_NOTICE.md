# ClickBench queries: license and attribution

The queries under `queries/*/clickbench/` (every engine, including the canonical
`queries/duckdb/clickbench/`) come from [ClickBench](https://github.com/ClickHouse/ClickBench),
by the ClickBench authors, and are licensed under the
[Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-nc-sa/4.0/)
license. They are **not** covered by this repository's Apache License.

Each engine's queries are taken from the ClickBench project's files for that engine
and may be adapted to this repository's schema names and dialect fixes.
`queries/fabric/clickbench/` is a T-SQL translation of `queries/duckdb/clickbench/`,
with one hand-maintained rewrite (`query_29.sql`). These files and any adaptations
of them remain under CC BY-NC-SA 4.0.

ClickBench asks that results produced with it reference the original benchmark:
<https://github.com/ClickHouse/ClickBench>.

"""Per-engine optimization prompt: guidance registry + injection."""

from __future__ import annotations

from benchmark.optimize_pass import (
    ENGINE_GUIDANCE,
    OPTIMIZATION_PROMPT,
    _guidance_audit,
    _guidance_block,
)

KNOWN_ENGINES = {
    "snowflake",
    "databricks",
    "bigquery",
    "redshift",
    "fabric",
    "clickhouse",
    "motherduck",
}


def _render(engine: str, sql: str = "SELECT 1") -> str:
    return OPTIMIZATION_PROMPT.format(
        engine=engine, guidance_block=_guidance_block(engine), sql=sql
    )


def test_all_known_engines_have_a_registry_entry():
    assert set(ENGINE_GUIDANCE) >= KNOWN_ENGINES


def test_prompt_states_duckdb_reference_semantics():
    # The verified reference facts must be in the preamble for every engine.
    p = _render("bigquery")
    assert "NULLs LAST for both ASC and DESC" in p
    assert "true\n(fractional) division" in p or "true (fractional) division" in p
    assert "1e-4" in p


def test_prompt_forbids_tiebreaker_columns():
    # The constraint: don't change query logic to mask LIMIT tie-breaks.
    assert "do not add" in _render("snowflake").lower()
    assert "tiebreaker" in _render("snowflake").lower()


def test_correctness_and_performance_sections_render():
    p = _render("snowflake")
    assert "CORRECTNESS" in p and "PERFORMANCE" in p
    for rule in ENGINE_GUIDANCE["snowflake"].correctness:
        assert rule in p
    for rule in ENGINE_GUIDANCE["snowflake"].performance:
        assert rule in p


def test_snowflake_keeps_validated_findings():
    joined = " ".join(ENGINE_GUIDANCE["snowflake"].correctness).lower()
    assert "division" in joined and "scale" in joined  # dsb_q83
    assert "union" in joined  # dsb_q75


def test_null_ordering_rules_target_the_right_default_per_engine():
    # Reference is NULLS LAST always. Snowflake/Redshift diverge on DESC;
    # Databricks/BigQuery/Fabric diverge on ASC. ClickHouse already matches.
    def corr(e: str) -> str:
        return " ".join(ENGINE_GUIDANCE[e].correctness).lower()

    for e in ("snowflake", "redshift"):
        assert "desc" in corr(e) and "nulls last" in corr(e)
    for e in ("databricks", "bigquery", "fabric"):
        assert "asc" in corr(e) and "nulls last" in corr(e)


def test_integer_division_rule_only_where_it_diverges():
    # int/int truncates only on Fabric (T-SQL) and Redshift; the others' `/`
    # already returns a fractional value like the reference.
    for e in ("fabric", "redshift"):
        assert "integer division" in " ".join(ENGINE_GUIDANCE[e].correctness).lower()


def test_clickhouse_correctness_landmines_present():
    joined = " ".join(ENGINE_GUIDANCE["clickhouse"].correctness).lower()
    assert "union" in joined  # bare UNION doesn't dedup
    assert "join_use_nulls" in joined  # outer-join NULL fill


def test_no_physical_design_keys_in_query_rewrite_guidance():
    # Distribution/sort/cluster-key *selection* is physical design, not a query
    # rewrite — it must not leak into the prompt guidance.
    for g in ENGINE_GUIDANCE.values():
        blob = " ".join(g.correctness + g.performance).lower()
        assert "distkey" not in blob
        assert "sortkey" not in blob
        assert "create table" not in blob


def test_prompt_clean_when_engine_has_no_guidance(monkeypatch):
    # Defensive: an unknown engine yields no guidance block and still renders.
    p = _render("nope")
    assert "Engine-specific rules" not in p
    assert "{guidance_block}" not in p
    assert "SELECT 1" in p


def test_guidance_audit_is_json_serializable():
    import json

    for e in KNOWN_ENGINES:
        audit = _guidance_audit(e)
        assert set(audit) == {"correctness", "performance"}
        json.dumps(audit)  # must not raise


def _perf(engine: str) -> str:
    return " ".join(ENGINE_GUIDANCE[engine].performance).lower()


def test_performance_guidance_is_enriched():
    # Each SQL engine (not MotherDuck, which is the reference) carries a
    # substantive set of performance rules.
    for e in KNOWN_ENGINES - {"motherduck"}:
        assert len(ENGINE_GUIDANCE[e].performance) >= 6


def test_qualify_only_where_supported():
    # QUALIFY exists in Snowflake/Databricks/BigQuery; Redshift and Fabric T-SQL
    # have no QUALIFY clause — proposing it there would be a syntax error.
    for e in ("snowflake", "databricks", "bigquery"):
        assert "qualify" in _perf(e)
    for e in ("redshift", "fabric"):
        assert "qualify" not in _perf(e)


def test_engine_specific_perf_signatures_present():
    assert "top-k pruning" in _perf("snowflake")
    assert "photon-eligible" in _perf("databricks") or "udf" in _perf("databricks")
    assert "does not materialize with" in _perf("bigquery")
    assert "case" in _perf("redshift") and "one-phase" in _perf("redshift")
    assert "force distributed plan" in _perf("fabric")
    assert "-if aggregate combinators" in _perf("clickhouse") or "sumif" in _perf("clickhouse")


def test_perf_rules_stay_result_neutral_no_approximations():
    # Performance rules must never reintroduce approximate aggregation.
    for e in KNOWN_ENGINES:
        blob = _perf(e)
        for banned in ("approx_count_distinct", "approximate count", "uniqhll", "hll++"):
            assert banned not in blob

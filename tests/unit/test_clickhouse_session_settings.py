"""ClickHouse connector session defaults — correctness alignment vs DuckDB."""

from __future__ import annotations

from benchmark.connectors.clickhouse import ClickHouseConnector


def test_correctness_defaults_present():
    # join_use_nulls=1 and union_default_mode='DISTINCT' align ClickHouse outer-join
    # NULL fill and bare-UNION dedup with the DuckDB reference, applied to every
    # query independent of the optional optimization pass.
    s = ClickHouseConnector({})._session_settings()
    assert s["join_use_nulls"] == 1
    assert s["union_default_mode"] == "DISTINCT"


def test_spill_capability_parity_defaults():
    """Every other engine's defaults spill to disk under memory pressure; these
    session defaults let ClickHouse degrade the same way instead of failing fast.
    Ratio forms so one value fits every replica size in a sweep; grace_hash appended
    after the server-default join algorithms so in-memory plans are unchanged."""
    s = ClickHouseConnector({})._session_settings()
    assert s["max_bytes_ratio_before_external_group_by"] == 0.7
    assert s["max_bytes_ratio_before_external_sort"] == 0.7
    assert s["join_algorithm"] == "direct,parallel_hash,hash,grace_hash"
    assert s["join_algorithm"].endswith("grace_hash"), "spill fallback must come last"


def test_cache_disabled_by_default_and_toggleable():
    assert ClickHouseConnector({})._session_settings()["use_query_cache"] == 0
    assert ClickHouseConnector({"disable_cache": False})._session_settings()["use_query_cache"] == 1


def test_session_settings_overridable_but_defaults_stay_unless_overridden():
    s = ClickHouseConnector(
        {"session_settings": {"max_threads": 8, "join_use_nulls": 0}}
    )._session_settings()
    # Explicit override wins...
    assert s["join_use_nulls"] == 0
    assert s["max_threads"] == 8
    # ...and unrelated correctness defaults remain.
    assert s["union_default_mode"] == "DISTINCT"

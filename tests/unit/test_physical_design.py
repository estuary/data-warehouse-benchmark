"""Fabric CREATE STATISTICS emitter + design entry + registration."""

from __future__ import annotations

from benchmark.physical_design import _PLATFORM_EMITTERS, DESIGN, _fabric_statistics_ddl


def test_fabric_statistics_ddl_is_idempotent_create():
    ddl = _fabric_statistics_ddl("tpcds_sf1", "store_sales", ["ss_sold_date_sk", "ss_item_sk"])
    assert len(ddl) == 2
    assert "CREATE STATISTICS" in ddl[0]
    assert "sys.stats" in ddl[0] and "IF NOT EXISTS" in ddl[0]
    assert "tpcds_sf1.store_sales (ss_sold_date_sk)" in ddl[0]


def test_fabric_design_has_statistics_for_seven_facts():
    assert len(DESIGN["fabric"]["statistics"]) == 7


def test_fabric_emitter_registered():
    assert "fabric" in _PLATFORM_EMITTERS

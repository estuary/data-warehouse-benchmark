# ── complex_join classification ──────────────────────────────────────────────
#
# TPC-DS and DSB express joins as comma-separated relation lists, not JOIN
# keywords. Counting JOIN keywords alone tags 10 of 201 decision-support queries
# as join-heavy when 160 of them join three or more tables, which would leave
# every pattern-sliced figure in the report computed over a ~6-query remnant.


def test_comma_joins_count_as_a_complex_join():
    from benchmark.queries.registry import _infer_patterns

    sql = """
        SELECT i_item_id, SUM(ss_ext_sales_price)
        FROM store_sales, date_dim, item
        WHERE ss_sold_date_sk = d_date_sk AND ss_item_sk = i_item_sk
        GROUP BY i_item_id
    """

    assert "complex_join" in _infer_patterns(sql), (
        "a three-table star-schema join written in the comma style must be tagged"
    )


def test_explicit_joins_still_count():
    from benchmark.queries.registry import _infer_patterns

    sql = """
        SELECT * FROM store_sales
        JOIN date_dim ON ss_sold_date_sk = d_date_sk
        JOIN item ON ss_item_sk = i_item_sk
    """

    assert "complex_join" in _infer_patterns(sql)


def test_two_table_join_is_not_complex():
    from benchmark.queries.registry import _infer_patterns

    sql = "SELECT * FROM store_sales, date_dim WHERE ss_sold_date_sk = d_date_sk"

    assert "complex_join" not in _infer_patterns(sql)


def test_single_table_scan_is_not_a_join():
    from benchmark.queries.registry import _infer_patterns

    # A ClickBench-shaped query: one wide table, commas only in the SELECT list.
    sql = "SELECT UserID, COUNT(*), SUM(a), AVG(b) FROM hits WHERE URL LIKE '%x%' GROUP BY UserID"

    assert "complex_join" not in _infer_patterns(sql)


def test_commas_inside_parentheses_do_not_inflate_the_count():
    from benchmark.queries.registry import _infer_patterns

    # COALESCE args and an IN-list are commas at depth > 0, not relations.
    sql = (
        "SELECT COALESCE(a, b, c) FROM store_sales "
        "WHERE ss_item_sk IN (1, 2, 3, 4, 5) AND ss_quantity > 0"
    )

    assert "complex_join" not in _infer_patterns(sql)

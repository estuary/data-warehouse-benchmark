WITH results AS (
  SELECT
    i_item_id,
    s_state,
    0 AS g_state,
    ss_quantity AS agg1,
    ss_list_price AS agg2,
    ss_coupon_amt AS agg3,
    ss_sales_price AS agg4
  FROM store_sales, customer_demographics, date_dim, store, item
  WHERE
    ss_sold_date_sk = d_date_sk
    AND ss_item_sk = i_item_sk
    AND ss_store_sk = s_store_sk
    AND ss_cdemo_sk = cd_demo_sk
    AND cd_gender = 'M'
    AND cd_marital_status = 'S'
    AND cd_education_status = 'College'
    AND d_year = 2002
    AND s_state = 'TN'
)
SELECT
  i_item_id,
  s_state,
  g_state,
  agg1,
  agg2,
  agg3,
  agg4
FROM (
  SELECT
    i_item_id,
    s_state,
    0 AS g_state,
    AVG(agg1) AS agg1,
    AVG(agg2) AS agg2,
    AVG(agg3) AS agg3,
    AVG(agg4) AS agg4
  FROM results
  GROUP BY
    i_item_id,
    s_state
  UNION ALL
  SELECT
    i_item_id,
    NULL AS s_state,
    1 AS g_state,
    AVG(agg1) AS agg1,
    AVG(agg2) AS agg2,
    AVG(agg3) AS agg3,
    AVG(agg4) AS agg4
  FROM results
  GROUP BY
    i_item_id
  UNION ALL
  SELECT
    NULL AS i_item_id,
    NULL AS s_state,
    1 AS g_state,
    AVG(agg1) AS agg1,
    AVG(agg2) AS agg2,
    AVG(agg3) AS agg3,
    AVG(agg4) AS agg4
  FROM results
) AS foo
ORDER BY
  i_item_id,
  s_state
LIMIT 100

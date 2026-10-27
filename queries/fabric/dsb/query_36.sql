WITH results AS (
  SELECT
    SUM(ss_net_profit) AS ss_net_profit,
    SUM(ss_ext_sales_price) AS ss_ext_sales_price,
    CAST((
      SUM(ss_net_profit) * 1.0000
    ) AS FLOAT) / NULLIF(SUM(ss_ext_sales_price), 0) AS gross_margin,
    i_category AS i_category,
    i_class AS i_class,
    0 AS g_category,
    0 AS g_class
  FROM store_sales, date_dim AS d1, item, store
  WHERE
    d1.d_year = 2001
    AND d1.d_date_sk = ss_sold_date_sk
    AND i_item_sk = ss_item_sk
    AND s_store_sk = ss_store_sk
    AND s_state = 'TN'
  GROUP BY
    i_category,
    i_class
), results_rollup AS (
  SELECT
    gross_margin,
    i_category,
    i_class,
    0 AS t_category,
    0 AS t_class,
    0 AS lochierarchy
  FROM results
  UNION
  SELECT
    CAST((
      SUM(ss_net_profit) * 1.0000
    ) AS FLOAT) / NULLIF(SUM(ss_ext_sales_price), 0) AS gross_margin,
    i_category,
    NULL AS i_class,
    0 AS t_category,
    1 AS t_class,
    1 AS lochierarchy
  FROM results
  GROUP BY
    i_category
  UNION
  SELECT
    CAST((
      SUM(ss_net_profit) * 1.0000
    ) AS FLOAT) / NULLIF(SUM(ss_ext_sales_price), 0) AS gross_margin,
    NULL AS i_category,
    NULL AS i_class,
    1 AS t_category,
    1 AS t_class,
    2 AS lochierarchy
  FROM results
)
SELECT
TOP 100
  gross_margin,
  i_category,
  i_class,
  lochierarchy,
  RANK() OVER (
    PARTITION BY lochierarchy, CASE WHEN t_class = 0 THEN i_category END
    ORDER BY CASE WHEN gross_margin IS NULL THEN 1 ELSE 0 END, gross_margin ASC
  ) AS rank_within_parent
FROM results_rollup
ORDER BY
  CASE WHEN lochierarchy IS NULL THEN 1 ELSE 0 END DESC,
  lochierarchy DESC,
  CASE WHEN lochierarchy = 0 THEN i_category END,
  rank_within_parent

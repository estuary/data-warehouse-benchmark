SELECT
TOP 100
  i_item_id,
  i_item_desc,
  i_category,
  i_class,
  i_current_price,
  SUM(ws_ext_sales_price) AS itemrevenue,
  CAST(SUM(ws_ext_sales_price) * 100.0000 AS FLOAT) / NULLIF(SUM(SUM(ws_ext_sales_price)) OVER (PARTITION BY i_class), 0) AS revenueratio
FROM web_sales, item, date_dim
WHERE
  ws_item_sk = i_item_sk
  AND i_category IN ('Sports', 'Books', 'Home')
  AND ws_sold_date_sk = d_date_sk
  AND d_date BETWEEN CAST('1999-02-22' AS DATE) AND CAST('1999-03-24' AS DATE)
GROUP BY
  i_item_id,
  i_item_desc,
  i_category,
  i_class,
  i_current_price
ORDER BY
  CASE WHEN i_category IS NULL THEN 1 ELSE 0 END,
  i_category,
  CASE WHEN i_class IS NULL THEN 1 ELSE 0 END,
  i_class,
  CASE WHEN i_item_id IS NULL THEN 1 ELSE 0 END,
  i_item_id,
  CASE WHEN i_item_desc IS NULL THEN 1 ELSE 0 END,
  i_item_desc,
  CASE
    WHEN CAST(SUM(ws_ext_sales_price) * 100.0000 AS FLOAT) / NULLIF(SUM(SUM(ws_ext_sales_price)) OVER (PARTITION BY i_class), 0) IS NULL
    THEN 1
    ELSE 0
  END,
  revenueratio

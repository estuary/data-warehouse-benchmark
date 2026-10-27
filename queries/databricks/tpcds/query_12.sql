SELECT
  i_item_id,
  i_item_desc,
  i_category,
  i_class,
  i_current_price,
  SUM(ws_ext_sales_price) AS itemrevenue,
  SUM(ws_ext_sales_price) * 100.0000 / NULLIF(SUM(SUM(ws_ext_sales_price)) OVER (PARTITION BY i_class), 0) AS revenueratio
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
  i_category NULLS LAST,
  i_class NULLS LAST,
  i_item_id NULLS LAST,
  i_item_desc NULLS LAST,
  revenueratio NULLS LAST
LIMIT 100

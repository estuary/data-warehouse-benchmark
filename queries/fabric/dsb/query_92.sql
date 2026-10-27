SELECT
TOP 100
  SUM(ws_ext_discount_amt) AS [Excess Discount Amount]
FROM web_sales, item, date_dim
WHERE
  i_manufact_id = 350
  AND i_item_sk = ws_item_sk
  AND d_date BETWEEN '2000-01-27' AND CAST('2000-04-26' AS DATE)
  AND d_date_sk = ws_sold_date_sk
  AND ws_ext_discount_amt > (
    SELECT
      1.3 * AVG(CAST(ws_ext_discount_amt AS FLOAT))
    FROM web_sales, date_dim
    WHERE
      ws_item_sk = i_item_sk
      AND d_date BETWEEN '2000-01-27' AND CAST('2000-04-26' AS DATE)
      AND d_date_sk = ws_sold_date_sk
  )
ORDER BY
  CASE WHEN SUM(ws_ext_discount_amt) IS NULL THEN 1 ELSE 0 END, SUM(ws_ext_discount_amt)

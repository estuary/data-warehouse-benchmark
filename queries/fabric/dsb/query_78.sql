WITH ws AS (
  SELECT
    d_year AS ws_sold_year,
    ws_item_sk AS ws_item_sk,
    ws_bill_customer_sk AS ws_customer_sk,
    SUM(ws_quantity) AS ws_qty,
    SUM(ws_wholesale_cost) AS ws_wc,
    SUM(ws_sales_price) AS ws_sp
  FROM web_sales
  LEFT JOIN web_returns
    ON wr_order_number = ws_order_number AND ws_item_sk = wr_item_sk
  JOIN date_dim
    ON ws_sold_date_sk = d_date_sk
  WHERE
    wr_order_number IS NULL
  GROUP BY
    d_year,
    ws_item_sk,
    ws_bill_customer_sk
), cs AS (
  SELECT
    d_year AS cs_sold_year,
    cs_item_sk AS cs_item_sk,
    cs_bill_customer_sk AS cs_customer_sk,
    SUM(cs_quantity) AS cs_qty,
    SUM(cs_wholesale_cost) AS cs_wc,
    SUM(cs_sales_price) AS cs_sp
  FROM catalog_sales
  LEFT JOIN catalog_returns
    ON cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
  JOIN date_dim
    ON cs_sold_date_sk = d_date_sk
  WHERE
    cr_order_number IS NULL
  GROUP BY
    d_year,
    cs_item_sk,
    cs_bill_customer_sk
), ss AS (
  SELECT
    d_year AS ss_sold_year,
    ss_item_sk AS ss_item_sk,
    ss_customer_sk AS ss_customer_sk,
    SUM(ss_quantity) AS ss_qty,
    SUM(ss_wholesale_cost) AS ss_wc,
    SUM(ss_sales_price) AS ss_sp
  FROM store_sales
  LEFT JOIN store_returns
    ON sr_ticket_number = ss_ticket_number AND ss_item_sk = sr_item_sk
  JOIN date_dim
    ON ss_sold_date_sk = d_date_sk
  WHERE
    sr_ticket_number IS NULL
  GROUP BY
    d_year,
    ss_item_sk,
    ss_customer_sk
)
SELECT
TOP 100
  ss_sold_year,
  ss_item_sk,
  ss_customer_sk,
  ROUND(
    CAST((
      ss_qty * 1.00
    ) AS FLOAT) / NULLIF((
      COALESCE(ws_qty, 0) + COALESCE(cs_qty, 0)
    ), 0),
    2
  ) AS ratio,
  ss_qty AS store_qty,
  ss_wc AS store_wholesale_cost,
  ss_sp AS store_sales_price,
  COALESCE(ws_qty, 0) + COALESCE(cs_qty, 0) AS other_chan_qty,
  COALESCE(ws_wc, 0) + COALESCE(cs_wc, 0) AS other_chan_wholesale_cost,
  COALESCE(ws_sp, 0) + COALESCE(cs_sp, 0) AS other_chan_sales_price
FROM ss
LEFT JOIN ws
  ON (
    ws_sold_year = ss_sold_year
    AND ws_item_sk = ss_item_sk
    AND ws_customer_sk = ss_customer_sk
  )
LEFT JOIN cs
  ON (
    cs_sold_year = ss_sold_year
    AND cs_item_sk = ss_item_sk
    AND cs_customer_sk = ss_customer_sk
  )
WHERE
  (
    COALESCE(ws_qty, 0) > 0 OR COALESCE(cs_qty, 0) > 0
  ) AND ss_sold_year = 2000
ORDER BY
  CASE WHEN ss_sold_year IS NULL THEN 1 ELSE 0 END,
  ss_sold_year,
  CASE WHEN ss_item_sk IS NULL THEN 1 ELSE 0 END,
  ss_item_sk,
  CASE WHEN ss_customer_sk IS NULL THEN 1 ELSE 0 END,
  ss_customer_sk,
  ss_qty DESC,
  ss_wc DESC,
  ss_sp DESC,
  CASE WHEN COALESCE(ws_qty, 0) + COALESCE(cs_qty, 0) IS NULL THEN 1 ELSE 0 END,
  other_chan_qty,
  CASE WHEN COALESCE(ws_wc, 0) + COALESCE(cs_wc, 0) IS NULL THEN 1 ELSE 0 END,
  other_chan_wholesale_cost,
  CASE WHEN COALESCE(ws_sp, 0) + COALESCE(cs_sp, 0) IS NULL THEN 1 ELSE 0 END,
  other_chan_sales_price,
  CASE
    WHEN ROUND(
      CAST((
        ss_qty * 1.00
      ) AS FLOAT) / NULLIF((
        COALESCE(ws_qty, 0) + COALESCE(cs_qty, 0)
      ), 0),
      2
    ) IS NULL
    THEN 1
    ELSE 0
  END,
  ratio

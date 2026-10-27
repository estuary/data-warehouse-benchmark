WITH ss AS (
  SELECT
    ca_county AS ca_county,
    d_qoy AS d_qoy,
    d_year AS d_year,
    SUM(ss_ext_sales_price) AS store_sales
  FROM store_sales, date_dim, customer_address
  WHERE
    ss_sold_date_sk = d_date_sk AND ss_addr_sk = ca_address_sk
  GROUP BY
    ca_county,
    d_qoy,
    d_year
), ws AS (
  SELECT
    ca_county AS ca_county,
    d_qoy AS d_qoy,
    d_year AS d_year,
    SUM(ws_ext_sales_price) AS web_sales
  FROM web_sales, date_dim, customer_address
  WHERE
    ws_sold_date_sk = d_date_sk AND ws_bill_addr_sk = ca_address_sk
  GROUP BY
    ca_county,
    d_qoy,
    d_year
)
SELECT
  ss1.ca_county,
  ss1.d_year,
  CAST((
    ws2.web_sales * 1.0000
  ) AS FLOAT) / NULLIF(ws1.web_sales, 0) AS web_q1_q2_increase,
  CAST((
    ss2.store_sales * 1.0000
  ) AS FLOAT) / NULLIF(ss1.store_sales, 0) AS store_q1_q2_increase,
  CAST((
    ws3.web_sales * 1.0000
  ) AS FLOAT) / NULLIF(ws2.web_sales, 0) AS web_q2_q3_increase,
  CAST((
    ss3.store_sales * 1.0000
  ) AS FLOAT) / NULLIF(ss2.store_sales, 0) AS store_q2_q3_increase
FROM ss AS ss1, ss AS ss2, ss AS ss3, ws AS ws1, ws AS ws2, ws AS ws3
WHERE
  ss1.d_qoy = 1
  AND ss1.d_year = 2000
  AND ss1.ca_county = ss2.ca_county
  AND ss2.d_qoy = 2
  AND ss2.d_year = 2000
  AND ss2.ca_county = ss3.ca_county
  AND ss3.d_qoy = 3
  AND ss3.d_year = 2000
  AND ss1.ca_county = ws1.ca_county
  AND ws1.d_qoy = 1
  AND ws1.d_year = 2000
  AND ws1.ca_county = ws2.ca_county
  AND ws2.d_qoy = 2
  AND ws2.d_year = 2000
  AND ws1.ca_county = ws3.ca_county
  AND ws3.d_qoy = 3
  AND ws3.d_year = 2000
  AND CASE
    WHEN ws1.web_sales > 0
    THEN CAST((
      ws2.web_sales * 1.0000
    ) AS FLOAT) / NULLIF(ws1.web_sales, 0)
    ELSE NULL
  END > CASE
    WHEN ss1.store_sales > 0
    THEN CAST((
      ss2.store_sales * 1.0000
    ) AS FLOAT) / NULLIF(ss1.store_sales, 0)
    ELSE NULL
  END
  AND CASE
    WHEN ws2.web_sales > 0
    THEN CAST((
      ws3.web_sales * 1.0000
    ) AS FLOAT) / NULLIF(ws2.web_sales, 0)
    ELSE NULL
  END > CASE
    WHEN ss2.store_sales > 0
    THEN CAST((
      ss3.store_sales * 1.0000
    ) AS FLOAT) / NULLIF(ss2.store_sales, 0)
    ELSE NULL
  END
ORDER BY
  CASE WHEN ss1.ca_county IS NULL THEN 1 ELSE 0 END,
  ss1.ca_county

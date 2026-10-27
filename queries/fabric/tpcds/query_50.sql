SELECT
TOP 100
  s_store_name,
  s_company_id,
  s_street_number,
  s_street_name,
  s_street_type,
  s_suite_number,
  s_city,
  s_county,
  s_state,
  s_zip,
  SUM(CASE WHEN (
    sr_returned_date_sk - ss_sold_date_sk <= 30
  ) THEN 1 ELSE 0 END) AS [30 days],
  SUM(
    CASE
      WHEN (
        sr_returned_date_sk - ss_sold_date_sk > 30
      )
      AND (
        sr_returned_date_sk - ss_sold_date_sk <= 60
      )
      THEN 1
      ELSE 0
    END
  ) AS [31-60 days],
  SUM(
    CASE
      WHEN (
        sr_returned_date_sk - ss_sold_date_sk > 60
      )
      AND (
        sr_returned_date_sk - ss_sold_date_sk <= 90
      )
      THEN 1
      ELSE 0
    END
  ) AS [61-90 days],
  SUM(
    CASE
      WHEN (
        sr_returned_date_sk - ss_sold_date_sk > 90
      )
      AND (
        sr_returned_date_sk - ss_sold_date_sk <= 120
      )
      THEN 1
      ELSE 0
    END
  ) AS [91-120 days],
  SUM(CASE WHEN (
    sr_returned_date_sk - ss_sold_date_sk > 120
  ) THEN 1 ELSE 0 END) AS [>120 days]
FROM store_sales, store_returns, store, date_dim AS d1, date_dim AS d2
WHERE
  d2.d_year = 2001
  AND d2.d_moy = 8
  AND ss_ticket_number = sr_ticket_number
  AND ss_item_sk = sr_item_sk
  AND ss_sold_date_sk = d1.d_date_sk
  AND sr_returned_date_sk = d2.d_date_sk
  AND ss_customer_sk = sr_customer_sk
  AND ss_store_sk = s_store_sk
GROUP BY
  s_store_name,
  s_company_id,
  s_street_number,
  s_street_name,
  s_street_type,
  s_suite_number,
  s_city,
  s_county,
  s_state,
  s_zip
ORDER BY
  CASE WHEN s_store_name IS NULL THEN 1 ELSE 0 END, s_store_name,
  CASE WHEN s_company_id IS NULL THEN 1 ELSE 0 END, s_company_id,
  CASE WHEN s_street_number IS NULL THEN 1 ELSE 0 END, s_street_number,
  CASE WHEN s_street_name IS NULL THEN 1 ELSE 0 END, s_street_name,
  CASE WHEN s_street_type IS NULL THEN 1 ELSE 0 END, s_street_type,
  CASE WHEN s_suite_number IS NULL THEN 1 ELSE 0 END, s_suite_number,
  CASE WHEN s_city IS NULL THEN 1 ELSE 0 END, s_city,
  CASE WHEN s_county IS NULL THEN 1 ELSE 0 END, s_county,
  CASE WHEN s_state IS NULL THEN 1 ELSE 0 END, s_state,
  CASE WHEN s_zip IS NULL THEN 1 ELSE 0 END, s_zip

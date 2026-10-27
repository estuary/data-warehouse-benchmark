WITH customer_total_return AS (
  SELECT
    cr_returning_customer_sk AS ctr_customer_sk,
    ca_state AS ctr_state,
    SUM(cr_return_amt_inc_tax) AS ctr_total_return
  FROM catalog_returns, date_dim, customer_address
  WHERE
    cr_returned_date_sk = d_date_sk
    AND d_year = 2000
    AND cr_returning_addr_sk = ca_address_sk
  GROUP BY
    cr_returning_customer_sk,
    ca_state
)
SELECT
TOP 100
  c_customer_id,
  c_salutation,
  c_first_name,
  c_last_name,
  ca_street_number,
  ca_street_name,
  ca_street_type,
  ca_suite_number,
  ca_city,
  ca_county,
  ca_state,
  ca_zip,
  ca_country,
  ca_gmt_offset,
  ca_location_type,
  ctr_total_return
FROM customer_total_return AS ctr1, customer_address, customer
WHERE
  ctr1.ctr_total_return > (
    SELECT
      AVG(CAST(ctr_total_return AS FLOAT)) * 1.2
    FROM customer_total_return AS ctr2
    WHERE
      ctr1.ctr_state = ctr2.ctr_state
  )
  AND ca_address_sk = c_current_addr_sk
  AND ca_state = 'GA'
  AND ctr1.ctr_customer_sk = c_customer_sk
ORDER BY
  CASE WHEN c_customer_id IS NULL THEN 1 ELSE 0 END, c_customer_id,
  CASE WHEN c_salutation IS NULL THEN 1 ELSE 0 END, c_salutation,
  CASE WHEN c_first_name IS NULL THEN 1 ELSE 0 END, c_first_name,
  CASE WHEN c_last_name IS NULL THEN 1 ELSE 0 END, c_last_name,
  CASE WHEN ca_street_number IS NULL THEN 1 ELSE 0 END, ca_street_number,
  CASE WHEN ca_street_name IS NULL THEN 1 ELSE 0 END, ca_street_name,
  CASE WHEN ca_street_type IS NULL THEN 1 ELSE 0 END, ca_street_type,
  CASE WHEN ca_suite_number IS NULL THEN 1 ELSE 0 END, ca_suite_number,
  CASE WHEN ca_city IS NULL THEN 1 ELSE 0 END, ca_city,
  CASE WHEN ca_county IS NULL THEN 1 ELSE 0 END, ca_county,
  CASE WHEN ca_state IS NULL THEN 1 ELSE 0 END, ca_state,
  CASE WHEN ca_zip IS NULL THEN 1 ELSE 0 END, ca_zip,
  CASE WHEN ca_country IS NULL THEN 1 ELSE 0 END, ca_country,
  CASE WHEN ca_gmt_offset IS NULL THEN 1 ELSE 0 END, ca_gmt_offset,
  CASE WHEN ca_location_type IS NULL THEN 1 ELSE 0 END, ca_location_type,
  CASE WHEN ctr_total_return IS NULL THEN 1 ELSE 0 END, ctr_total_return

SELECT
TOP 100
  cd_gender,
  cd_marital_status,
  cd_education_status,
  COUNT_BIG(*) AS cnt1,
  cd_purchase_estimate,
  COUNT_BIG(*) AS cnt2,
  cd_credit_rating,
  COUNT_BIG(*) AS cnt3,
  cd_dep_count,
  COUNT_BIG(*) AS cnt4,
  cd_dep_employed_count,
  COUNT_BIG(*) AS cnt5,
  cd_dep_college_count,
  COUNT_BIG(*) AS cnt6
FROM customer AS c, customer_address AS ca, customer_demographics
WHERE
  c.c_current_addr_sk = ca.ca_address_sk
  AND ca_county IN (
    'Rush County',
    'Toole County',
    'Jefferson County',
    'Dona Ana County',
    'La Porte County'
  )
  AND cd_demo_sk = c.c_current_cdemo_sk
  AND EXISTS(
    SELECT
      *
    FROM store_sales, date_dim
    WHERE
      c.c_customer_sk = ss_customer_sk
      AND ss_sold_date_sk = d_date_sk
      AND d_year = 2002
      AND d_moy BETWEEN 1 AND 1 + 3
  )
  AND (
    EXISTS(
      SELECT
        *
      FROM web_sales, date_dim
      WHERE
        c.c_customer_sk = ws_bill_customer_sk
        AND ws_sold_date_sk = d_date_sk
        AND d_year = 2002
        AND d_moy BETWEEN 1 AND 1 + 3
    )
    OR EXISTS(
      SELECT
        *
      FROM catalog_sales, date_dim
      WHERE
        c.c_customer_sk = cs_ship_customer_sk
        AND cs_sold_date_sk = d_date_sk
        AND d_year = 2002
        AND d_moy BETWEEN 1 AND 1 + 3
    )
  )
GROUP BY
  cd_gender,
  cd_marital_status,
  cd_education_status,
  cd_purchase_estimate,
  cd_credit_rating,
  cd_dep_count,
  cd_dep_employed_count,
  cd_dep_college_count
ORDER BY
  CASE WHEN cd_gender IS NULL THEN 1 ELSE 0 END, cd_gender,
  CASE WHEN cd_marital_status IS NULL THEN 1 ELSE 0 END, cd_marital_status,
  CASE WHEN cd_education_status IS NULL THEN 1 ELSE 0 END, cd_education_status,
  CASE WHEN cd_purchase_estimate IS NULL THEN 1 ELSE 0 END, cd_purchase_estimate,
  CASE WHEN cd_credit_rating IS NULL THEN 1 ELSE 0 END, cd_credit_rating,
  CASE WHEN cd_dep_count IS NULL THEN 1 ELSE 0 END, cd_dep_count,
  CASE WHEN cd_dep_employed_count IS NULL THEN 1 ELSE 0 END, cd_dep_employed_count,
  CASE WHEN cd_dep_college_count IS NULL THEN 1 ELSE 0 END, cd_dep_college_count

SELECT
TOP 100
  i_brand_id AS brand_id,
  i_brand AS brand,
  i_manufact_id,
  i_manufact,
  SUM(ss_ext_sales_price) AS ext_price
FROM date_dim, store_sales, item, customer, customer_address, store
WHERE
  d_date_sk = ss_sold_date_sk
  AND ss_item_sk = i_item_sk
  AND i_manager_id = 8
  AND d_moy = 11
  AND d_year = 1998
  AND ss_customer_sk = c_customer_sk
  AND c_current_addr_sk = ca_address_sk
  AND SUBSTRING(ca_zip, 1, 5) <> SUBSTRING(s_zip, 1, 5)
  AND ss_store_sk = s_store_sk
GROUP BY
  i_brand,
  i_brand_id,
  i_manufact_id,
  i_manufact
ORDER BY
  ext_price DESC,
  CASE WHEN i_brand IS NULL THEN 1 ELSE 0 END, i_brand,
  CASE WHEN i_brand_id IS NULL THEN 1 ELSE 0 END, i_brand_id,
  CASE WHEN i_manufact_id IS NULL THEN 1 ELSE 0 END, i_manufact_id,
  CASE WHEN i_manufact IS NULL THEN 1 ELSE 0 END, i_manufact

SELECT
  i_item_id,
  i_item_desc,
  s_state,
  COUNT(ss_quantity) AS store_sales_quantitycount,
  AVG(CAST(ss_quantity AS DOUBLE PRECISION)) AS store_sales_quantityave,
  STDDEV_SAMP(ss_quantity) AS store_sales_quantitystdev,
  CAST(STDDEV_SAMP(ss_quantity) AS DOUBLE PRECISION) / NULLIF(AVG(CAST(ss_quantity AS DOUBLE PRECISION)), 0) AS store_sales_quantitycov,
  COUNT(sr_return_quantity) AS store_returns_quantitycount,
  AVG(CAST(sr_return_quantity AS DOUBLE PRECISION)) AS store_returns_quantityave,
  STDDEV_SAMP(sr_return_quantity) AS store_returns_quantitystdev,
  CAST(STDDEV_SAMP(sr_return_quantity) AS DOUBLE PRECISION) / NULLIF(AVG(CAST(sr_return_quantity AS DOUBLE PRECISION)), 0) AS store_returns_quantitycov,
  COUNT(cs_quantity) AS catalog_sales_quantitycount,
  AVG(CAST(cs_quantity AS DOUBLE PRECISION)) AS catalog_sales_quantityave,
  STDDEV_SAMP(cs_quantity) AS catalog_sales_quantitystdev,
  CAST(STDDEV_SAMP(cs_quantity) AS DOUBLE PRECISION) / NULLIF(AVG(CAST(cs_quantity AS DOUBLE PRECISION)), 0) AS catalog_sales_quantitycov
FROM store_sales, store_returns, catalog_sales, date_dim AS d1, date_dim AS d2, date_dim AS d3, store, item
WHERE
  d1.d_quarter_name = '2001Q1'
  AND d1.d_date_sk = ss_sold_date_sk
  AND i_item_sk = ss_item_sk
  AND s_store_sk = ss_store_sk
  AND ss_customer_sk = sr_customer_sk
  AND ss_item_sk = sr_item_sk
  AND ss_ticket_number = sr_ticket_number
  AND sr_returned_date_sk = d2.d_date_sk
  AND d2.d_quarter_name IN ('2001Q1', '2001Q2', '2001Q3')
  AND sr_customer_sk = cs_bill_customer_sk
  AND sr_item_sk = cs_item_sk
  AND cs_sold_date_sk = d3.d_date_sk
  AND d3.d_quarter_name IN ('2001Q1', '2001Q2', '2001Q3')
GROUP BY
  i_item_id,
  i_item_desc,
  s_state
ORDER BY
  i_item_id NULLS FIRST,
  i_item_desc NULLS FIRST,
  s_state NULLS FIRST
LIMIT 100

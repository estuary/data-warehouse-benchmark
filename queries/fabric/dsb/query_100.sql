/* DSB Query 100 */ /* Two products sold together frequently. Self-joins on store_sales, */ /* PK/FK joins. DSB-specific addition to TPC-DS. */
SELECT
TOP 100
  item1.i_item_sk,
  item2.i_item_sk,
  COUNT_BIG(*) AS cnt
FROM item AS item1, item AS item2, store_sales AS s1, store_sales AS s2, date_dim, customer, customer_address, customer_demographics
WHERE
  item1.i_item_sk < item2.i_item_sk
  AND s1.ss_ticket_number = s2.ss_ticket_number
  AND s1.ss_item_sk = item1.i_item_sk
  AND s2.ss_item_sk = item2.i_item_sk
  AND s1.ss_customer_sk = c_customer_sk
  AND c_current_addr_sk = ca_address_sk
  AND c_current_cdemo_sk = cd_demo_sk
  AND d_year BETWEEN 1999 AND 2000
  AND d_date_sk = s1.ss_sold_date_sk
  AND item1.i_category IN ('Books', 'Music')
  AND item2.i_manager_id BETWEEN 1 AND 20
  AND cd_marital_status = 'M'
  AND cd_education_status = 'College'
  AND s1.ss_list_price BETWEEN 1 AND 5
  AND s2.ss_list_price BETWEEN 1 AND 5
GROUP BY
  item1.i_item_sk,
  item2.i_item_sk
ORDER BY
  CASE WHEN COUNT_BIG(*) IS NULL THEN 1 ELSE 0 END,
  cnt

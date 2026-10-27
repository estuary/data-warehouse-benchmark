/* DSB Query 101 */ /* Customers who first buy and return on web, then buy again from store. */ /* Non-PK/FK joins. DSB-specific addition to TPC-DS. */
SELECT
  c_customer_sk,
  c_first_name,
  c_last_name,
  COUNT(*) AS cnt
FROM store_sales, store_returns, web_sales, date_dim AS d1, date_dim AS d2, item, customer, customer_address, household_demographics
WHERE
  ss_ticket_number = sr_ticket_number
  AND ss_customer_sk = ws_bill_customer_sk
  AND ss_customer_sk = c_customer_sk
  AND c_current_addr_sk = ca_address_sk
  AND c_current_hdemo_sk = hd_demo_sk
  AND ss_item_sk = sr_item_sk
  AND sr_item_sk = ws_item_sk
  AND i_item_sk = ss_item_sk
  AND i_category IN ('Books', 'Music', 'Electronics')
  AND sr_returned_date_sk = d1.d_date_sk
  AND ws_sold_date_sk = d2.d_date_sk
  AND d2.d_date BETWEEN d1.d_date AND (
    d1.d_date + INTERVAL '90' DAY
  )
  AND ca_state IN ('TX', 'GA', 'VA', 'KY', 'KS')
  AND d1.d_year = 1999
  AND hd_income_band_sk BETWEEN 0 AND 19
  AND hd_buy_potential = '1001-5000'
  AND ss_sales_price / NULLIF(NULLIF(ss_list_price, 0), 0) BETWEEN 0.0 AND 1.0
GROUP BY
  c_customer_sk,
  c_first_name,
  c_last_name
ORDER BY
  cnt NULLS LAST
LIMIT 100

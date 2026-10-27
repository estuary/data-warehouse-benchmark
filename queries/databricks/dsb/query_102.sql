/* DSB Query 102 */ /* Customer demographics where customer buys from store and again from web */ /* (initial purchase could also be web). Non-PK/FK joins. DSB addition. */
SELECT
  cd_gender,
  cd_marital_status,
  cd_education_status,
  hd_vehicle_count,
  COUNT(*) AS cnt
FROM store_sales, web_sales, date_dim AS d1, date_dim AS d2, customer, inventory, store, warehouse, item, customer_demographics, household_demographics, customer_address
WHERE
  ss_item_sk = i_item_sk
  AND ws_item_sk = ss_item_sk
  AND ss_sold_date_sk = d1.d_date_sk
  AND ws_sold_date_sk = d2.d_date_sk
  AND d2.d_date BETWEEN d1.d_date AND (
    d1.d_date + INTERVAL '30' DAY
  )
  AND ss_customer_sk = c_customer_sk
  AND ws_bill_customer_sk = c_customer_sk
  AND ws_warehouse_sk = inv_warehouse_sk
  AND ws_warehouse_sk = w_warehouse_sk
  AND inv_item_sk = ss_item_sk
  AND inv_date_sk = ss_sold_date_sk
  AND inv_quantity_on_hand >= ss_quantity
  AND s_state = w_state
  AND i_category IN ('Books', 'Music', 'Electronics')
  AND i_manager_id IN (1, 11, 21, 31, 41, 51, 61, 71, 81, 91)
  AND c_current_cdemo_sk = cd_demo_sk
  AND c_current_hdemo_sk = hd_demo_sk
  AND c_current_addr_sk = ca_address_sk
  AND ca_state IN ('TX', 'GA', 'VA', 'KY', 'KS')
  AND d1.d_year = 1999
  AND ws_wholesale_cost BETWEEN 0 AND 19
GROUP BY
  cd_gender,
  cd_marital_status,
  cd_education_status,
  hd_vehicle_count
ORDER BY
  cnt NULLS LAST
LIMIT 100

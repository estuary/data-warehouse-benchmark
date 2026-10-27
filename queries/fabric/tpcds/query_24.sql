WITH ssales AS (
  SELECT
    c_last_name AS c_last_name,
    c_first_name AS c_first_name,
    s_store_name AS s_store_name,
    ca_state AS ca_state,
    s_state AS s_state,
    i_color AS i_color,
    i_current_price AS i_current_price,
    i_manager_id AS i_manager_id,
    i_units AS i_units,
    i_size AS i_size,
    SUM(ss_net_paid) AS netpaid
  FROM store_sales, store_returns, store, item, customer, customer_address
  WHERE
    ss_ticket_number = sr_ticket_number
    AND ss_item_sk = sr_item_sk
    AND ss_customer_sk = c_customer_sk
    AND ss_item_sk = i_item_sk
    AND ss_store_sk = s_store_sk
    AND c_current_addr_sk = ca_address_sk
    AND c_birth_country <> UPPER(ca_country)
    AND s_zip = ca_zip
    AND s_market_id = 8
  GROUP BY
    c_last_name,
    c_first_name,
    s_store_name,
    ca_state,
    s_state,
    i_color,
    i_current_price,
    i_manager_id,
    i_units,
    i_size
)
SELECT
  c_last_name,
  c_first_name,
  s_store_name,
  SUM(netpaid) AS paid
FROM ssales
WHERE
  i_color = 'peach'
GROUP BY
  c_last_name,
  c_first_name,
  s_store_name
HAVING
  SUM(netpaid) > (
    SELECT
      0.05 * AVG(CAST(netpaid AS FLOAT))
    FROM ssales
  )
ORDER BY
  CASE WHEN c_last_name IS NULL THEN 1 ELSE 0 END,
  c_last_name,
  CASE WHEN c_first_name IS NULL THEN 1 ELSE 0 END,
  c_first_name,
  CASE WHEN s_store_name IS NULL THEN 1 ELSE 0 END,
  s_store_name

SELECT
  w_warehouse_name,
  w_warehouse_sq_ft,
  w_city,
  w_county,
  w_state,
  w_country,
  ship_carriers,
  year_,
  SUM(x.jan_sales) AS jan_sales,
  SUM(x.feb_sales) AS feb_sales,
  SUM(x.mar_sales) AS mar_sales,
  SUM(x.apr_sales) AS apr_sales,
  SUM(x.may_sales) AS may_sales,
  SUM(x.jun_sales) AS jun_sales,
  SUM(x.jul_sales) AS jul_sales,
  SUM(x.aug_sales) AS aug_sales,
  SUM(x.sep_sales) AS sep_sales,
  SUM(x.oct_sales) AS oct_sales,
  SUM(x.nov_sales) AS nov_sales,
  SUM(x.dec_sales) AS dec_sales,
  SUM(x.jan_sales / x.w_warehouse_sq_ft) AS jan_sales_per_sq_foot,
  SUM(x.feb_sales / x.w_warehouse_sq_ft) AS feb_sales_per_sq_foot,
  SUM(x.mar_sales / x.w_warehouse_sq_ft) AS mar_sales_per_sq_foot,
  SUM(x.apr_sales / x.w_warehouse_sq_ft) AS apr_sales_per_sq_foot,
  SUM(x.may_sales / x.w_warehouse_sq_ft) AS may_sales_per_sq_foot,
  SUM(x.jun_sales / x.w_warehouse_sq_ft) AS jun_sales_per_sq_foot,
  SUM(x.jul_sales / x.w_warehouse_sq_ft) AS jul_sales_per_sq_foot,
  SUM(x.aug_sales / x.w_warehouse_sq_ft) AS aug_sales_per_sq_foot,
  SUM(x.sep_sales / x.w_warehouse_sq_ft) AS sep_sales_per_sq_foot,
  SUM(x.oct_sales / x.w_warehouse_sq_ft) AS oct_sales_per_sq_foot,
  SUM(x.nov_sales / x.w_warehouse_sq_ft) AS nov_sales_per_sq_foot,
  SUM(x.dec_sales / x.w_warehouse_sq_ft) AS dec_sales_per_sq_foot,
  SUM(x.jan_net) AS jan_net,
  SUM(x.feb_net) AS feb_net,
  SUM(x.mar_net) AS mar_net,
  SUM(x.apr_net) AS apr_net,
  SUM(x.may_net) AS may_net,
  SUM(x.jun_net) AS jun_net,
  SUM(x.jul_net) AS jul_net,
  SUM(x.aug_net) AS aug_net,
  SUM(x.sep_net) AS sep_net,
  SUM(x.oct_net) AS oct_net,
  SUM(x.nov_net) AS nov_net,
  SUM(x.dec_net) AS dec_net
FROM (
  SELECT
    w_warehouse_name,
    w_warehouse_sq_ft,
    w_city,
    w_county,
    w_state,
    w_country,
    'DHL,BARIAN' AS ship_carriers,
    d_year AS year_,
    SUM(CASE WHEN d_moy = 1 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS jan_sales,
    SUM(CASE WHEN d_moy = 2 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS feb_sales,
    SUM(CASE WHEN d_moy = 3 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS mar_sales,
    SUM(CASE WHEN d_moy = 4 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS apr_sales,
    SUM(CASE WHEN d_moy = 5 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS may_sales,
    SUM(CASE WHEN d_moy = 6 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS jun_sales,
    SUM(CASE WHEN d_moy = 7 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS jul_sales,
    SUM(CASE WHEN d_moy = 8 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS aug_sales,
    SUM(CASE WHEN d_moy = 9 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS sep_sales,
    SUM(CASE WHEN d_moy = 10 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS oct_sales,
    SUM(CASE WHEN d_moy = 11 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS nov_sales,
    SUM(CASE WHEN d_moy = 12 THEN ws_ext_sales_price * ws_quantity ELSE 0 END) AS dec_sales,
    SUM(CASE WHEN d_moy = 1 THEN ws_net_paid * ws_quantity ELSE 0 END) AS jan_net,
    SUM(CASE WHEN d_moy = 2 THEN ws_net_paid * ws_quantity ELSE 0 END) AS feb_net,
    SUM(CASE WHEN d_moy = 3 THEN ws_net_paid * ws_quantity ELSE 0 END) AS mar_net,
    SUM(CASE WHEN d_moy = 4 THEN ws_net_paid * ws_quantity ELSE 0 END) AS apr_net,
    SUM(CASE WHEN d_moy = 5 THEN ws_net_paid * ws_quantity ELSE 0 END) AS may_net,
    SUM(CASE WHEN d_moy = 6 THEN ws_net_paid * ws_quantity ELSE 0 END) AS jun_net,
    SUM(CASE WHEN d_moy = 7 THEN ws_net_paid * ws_quantity ELSE 0 END) AS jul_net,
    SUM(CASE WHEN d_moy = 8 THEN ws_net_paid * ws_quantity ELSE 0 END) AS aug_net,
    SUM(CASE WHEN d_moy = 9 THEN ws_net_paid * ws_quantity ELSE 0 END) AS sep_net,
    SUM(CASE WHEN d_moy = 10 THEN ws_net_paid * ws_quantity ELSE 0 END) AS oct_net,
    SUM(CASE WHEN d_moy = 11 THEN ws_net_paid * ws_quantity ELSE 0 END) AS nov_net,
    SUM(CASE WHEN d_moy = 12 THEN ws_net_paid * ws_quantity ELSE 0 END) AS dec_net
  FROM web_sales, warehouse, date_dim, time_dim, ship_mode
  WHERE
    ws_warehouse_sk = w_warehouse_sk
    AND ws_sold_date_sk = d_date_sk
    AND ws_sold_time_sk = t_time_sk
    AND ws_ship_mode_sk = sm_ship_mode_sk
    AND d_year = 2001
    AND t_time BETWEEN 30838 AND 30838 + 28800
    AND sm_carrier IN ('DHL', 'BARIAN')
  GROUP BY
    w_warehouse_name,
    w_warehouse_sq_ft,
    w_city,
    w_county,
    w_state,
    w_country,
    d_year
  UNION ALL
  SELECT
    w_warehouse_name,
    w_warehouse_sq_ft,
    w_city,
    w_county,
    w_state,
    w_country,
    'DHL,BARIAN' AS ship_carriers,
    d_year AS year_,
    SUM(CASE WHEN d_moy = 1 THEN cs_sales_price * cs_quantity ELSE 0 END) AS jan_sales,
    SUM(CASE WHEN d_moy = 2 THEN cs_sales_price * cs_quantity ELSE 0 END) AS feb_sales,
    SUM(CASE WHEN d_moy = 3 THEN cs_sales_price * cs_quantity ELSE 0 END) AS mar_sales,
    SUM(CASE WHEN d_moy = 4 THEN cs_sales_price * cs_quantity ELSE 0 END) AS apr_sales,
    SUM(CASE WHEN d_moy = 5 THEN cs_sales_price * cs_quantity ELSE 0 END) AS may_sales,
    SUM(CASE WHEN d_moy = 6 THEN cs_sales_price * cs_quantity ELSE 0 END) AS jun_sales,
    SUM(CASE WHEN d_moy = 7 THEN cs_sales_price * cs_quantity ELSE 0 END) AS jul_sales,
    SUM(CASE WHEN d_moy = 8 THEN cs_sales_price * cs_quantity ELSE 0 END) AS aug_sales,
    SUM(CASE WHEN d_moy = 9 THEN cs_sales_price * cs_quantity ELSE 0 END) AS sep_sales,
    SUM(CASE WHEN d_moy = 10 THEN cs_sales_price * cs_quantity ELSE 0 END) AS oct_sales,
    SUM(CASE WHEN d_moy = 11 THEN cs_sales_price * cs_quantity ELSE 0 END) AS nov_sales,
    SUM(CASE WHEN d_moy = 12 THEN cs_sales_price * cs_quantity ELSE 0 END) AS dec_sales,
    SUM(CASE WHEN d_moy = 1 THEN cs_net_paid_inc_tax * cs_quantity ELSE 0 END) AS jan_net,
    SUM(CASE WHEN d_moy = 2 THEN cs_net_paid_inc_tax * cs_quantity ELSE 0 END) AS feb_net,
    SUM(CASE WHEN d_moy = 3 THEN cs_net_paid_inc_tax * cs_quantity ELSE 0 END) AS mar_net,
    SUM(CASE WHEN d_moy = 4 THEN cs_net_paid_inc_tax * cs_quantity ELSE 0 END) AS apr_net,
    SUM(CASE WHEN d_moy = 5 THEN cs_net_paid_inc_tax * cs_quantity ELSE 0 END) AS may_net,
    SUM(CASE WHEN d_moy = 6 THEN cs_net_paid_inc_tax * cs_quantity ELSE 0 END) AS jun_net,
    SUM(CASE WHEN d_moy = 7 THEN cs_net_paid_inc_tax * cs_quantity ELSE 0 END) AS jul_net,
    SUM(CASE WHEN d_moy = 8 THEN cs_net_paid_inc_tax * cs_quantity ELSE 0 END) AS aug_net,
    SUM(CASE WHEN d_moy = 9 THEN cs_net_paid_inc_tax * cs_quantity ELSE 0 END) AS sep_net,
    SUM(CASE WHEN d_moy = 10 THEN cs_net_paid_inc_tax * cs_quantity ELSE 0 END) AS oct_net,
    SUM(CASE WHEN d_moy = 11 THEN cs_net_paid_inc_tax * cs_quantity ELSE 0 END) AS nov_net,
    SUM(CASE WHEN d_moy = 12 THEN cs_net_paid_inc_tax * cs_quantity ELSE 0 END) AS dec_net
  FROM catalog_sales, warehouse, date_dim, time_dim, ship_mode
  WHERE
    cs_warehouse_sk = w_warehouse_sk
    AND cs_sold_date_sk = d_date_sk
    AND cs_sold_time_sk = t_time_sk
    AND cs_ship_mode_sk = sm_ship_mode_sk
    AND d_year = 2001
    AND t_time BETWEEN 30838 AND 30838 + 28800
    AND sm_carrier IN ('DHL', 'BARIAN')
  GROUP BY
    w_warehouse_name,
    w_warehouse_sq_ft,
    w_city,
    w_county,
    w_state,
    w_country,
    d_year
) AS x
GROUP BY
  w_warehouse_name,
  w_warehouse_sq_ft,
  w_city,
  w_county,
  w_state,
  w_country,
  ship_carriers,
  year_
ORDER BY
  w_warehouse_name NULLS FIRST
LIMIT 100

SELECT
TOP 100
  i_item_id,
  AVG(CAST(ss_quantity AS FLOAT)) AS agg1,
  AVG(CAST(ss_list_price AS FLOAT)) AS agg2,
  AVG(CAST(ss_coupon_amt AS FLOAT)) AS agg3,
  AVG(CAST(ss_sales_price AS FLOAT)) AS agg4
FROM store_sales, customer_demographics, date_dim, item, promotion
WHERE
  ss_sold_date_sk = d_date_sk
  AND ss_item_sk = i_item_sk
  AND ss_cdemo_sk = cd_demo_sk
  AND ss_promo_sk = p_promo_sk
  AND cd_gender = 'M'
  AND cd_marital_status = 'S'
  AND cd_education_status = 'College'
  AND (
    p_channel_email = 'N' OR p_channel_event = 'N'
  )
  AND d_year = 2000
GROUP BY
  i_item_id
ORDER BY
  CASE WHEN i_item_id IS NULL THEN 1 ELSE 0 END, i_item_id

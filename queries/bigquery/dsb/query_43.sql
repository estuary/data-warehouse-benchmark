SELECT
  s_store_name,
  s_store_id,
  SUM(CASE WHEN (
    d_day_name = 'Sunday'
  ) THEN ss_sales_price ELSE NULL END) AS sun_sales,
  SUM(CASE WHEN (
    d_day_name = 'Monday'
  ) THEN ss_sales_price ELSE NULL END) AS mon_sales,
  SUM(CASE WHEN (
    d_day_name = 'Tuesday'
  ) THEN ss_sales_price ELSE NULL END) AS tue_sales,
  SUM(CASE WHEN (
    d_day_name = 'Wednesday'
  ) THEN ss_sales_price ELSE NULL END) AS wed_sales,
  SUM(CASE WHEN (
    d_day_name = 'Thursday'
  ) THEN ss_sales_price ELSE NULL END) AS thu_sales,
  SUM(CASE WHEN (
    d_day_name = 'Friday'
  ) THEN ss_sales_price ELSE NULL END) AS fri_sales,
  SUM(CASE WHEN (
    d_day_name = 'Saturday'
  ) THEN ss_sales_price ELSE NULL END) AS sat_sales
FROM date_dim, store_sales, store
WHERE
  d_date_sk = ss_sold_date_sk
  AND s_store_sk = ss_store_sk
  AND s_gmt_offset = -5
  AND d_year = 2000
GROUP BY
  s_store_name,
  s_store_id
ORDER BY
  s_store_name NULLS LAST,
  s_store_id NULLS LAST,
  sun_sales NULLS LAST,
  mon_sales NULLS LAST,
  tue_sales NULLS LAST,
  wed_sales NULLS LAST,
  thu_sales NULLS LAST,
  fri_sales NULLS LAST,
  sat_sales NULLS LAST
LIMIT 100

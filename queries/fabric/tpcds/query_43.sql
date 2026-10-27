SELECT
TOP 100
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
  CASE WHEN s_store_name IS NULL THEN 1 ELSE 0 END,
  s_store_name,
  CASE WHEN s_store_id IS NULL THEN 1 ELSE 0 END,
  s_store_id,
  CASE
    WHEN SUM(CASE WHEN (
      d_day_name = 'Sunday'
    ) THEN ss_sales_price ELSE NULL END) IS NULL
    THEN 1
    ELSE 0
  END,
  sun_sales,
  CASE
    WHEN SUM(CASE WHEN (
      d_day_name = 'Monday'
    ) THEN ss_sales_price ELSE NULL END) IS NULL
    THEN 1
    ELSE 0
  END,
  mon_sales,
  CASE
    WHEN SUM(CASE WHEN (
      d_day_name = 'Tuesday'
    ) THEN ss_sales_price ELSE NULL END) IS NULL
    THEN 1
    ELSE 0
  END,
  tue_sales,
  CASE
    WHEN SUM(CASE WHEN (
      d_day_name = 'Wednesday'
    ) THEN ss_sales_price ELSE NULL END) IS NULL
    THEN 1
    ELSE 0
  END,
  wed_sales,
  CASE
    WHEN SUM(CASE WHEN (
      d_day_name = 'Thursday'
    ) THEN ss_sales_price ELSE NULL END) IS NULL
    THEN 1
    ELSE 0
  END,
  thu_sales,
  CASE
    WHEN SUM(CASE WHEN (
      d_day_name = 'Friday'
    ) THEN ss_sales_price ELSE NULL END) IS NULL
    THEN 1
    ELSE 0
  END,
  fri_sales,
  CASE
    WHEN SUM(CASE WHEN (
      d_day_name = 'Saturday'
    ) THEN ss_sales_price ELSE NULL END) IS NULL
    THEN 1
    ELSE 0
  END,
  sat_sales

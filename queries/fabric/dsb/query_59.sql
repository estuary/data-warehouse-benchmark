WITH wss AS (
  SELECT
    d_week_seq AS d_week_seq,
    ss_store_sk AS ss_store_sk,
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
  FROM store_sales, date_dim
  WHERE
    d_date_sk = ss_sold_date_sk
  GROUP BY
    d_week_seq,
    ss_store_sk
)
SELECT
TOP 100
  s_store_name1,
  s_store_id1,
  d_week_seq1,
  CAST(sun_sales1 AS FLOAT) / NULLIF(sun_sales2, 0) AS sun_sales_ratio,
  CAST(mon_sales1 AS FLOAT) / NULLIF(mon_sales2, 0) AS mon_sales_ratio,
  CAST(tue_sales1 AS FLOAT) / NULLIF(tue_sales2, 0) AS tue_sales_ratio,
  CAST(wed_sales1 AS FLOAT) / NULLIF(wed_sales2, 0) AS wed_sales_ratio,
  CAST(thu_sales1 AS FLOAT) / NULLIF(thu_sales2, 0) AS thu_sales_ratio,
  CAST(fri_sales1 AS FLOAT) / NULLIF(fri_sales2, 0) AS fri_sales_ratio,
  CAST(sat_sales1 AS FLOAT) / NULLIF(sat_sales2, 0) AS sat_sales_ratio
FROM (
  SELECT
    s_store_name AS s_store_name1,
    wss.d_week_seq AS d_week_seq1,
    s_store_id AS s_store_id1,
    sun_sales AS sun_sales1,
    mon_sales AS mon_sales1,
    tue_sales AS tue_sales1,
    wed_sales AS wed_sales1,
    thu_sales AS thu_sales1,
    fri_sales AS fri_sales1,
    sat_sales AS sat_sales1
  FROM wss, store, date_dim AS d
  WHERE
    d.d_week_seq = wss.d_week_seq
    AND ss_store_sk = s_store_sk
    AND d_month_seq BETWEEN 1212 AND 1212 + 11
) AS y, (
  SELECT
    s_store_name AS s_store_name2,
    wss.d_week_seq AS d_week_seq2,
    s_store_id AS s_store_id2,
    sun_sales AS sun_sales2,
    mon_sales AS mon_sales2,
    tue_sales AS tue_sales2,
    wed_sales AS wed_sales2,
    thu_sales AS thu_sales2,
    fri_sales AS fri_sales2,
    sat_sales AS sat_sales2
  FROM wss, store, date_dim AS d
  WHERE
    d.d_week_seq = wss.d_week_seq
    AND ss_store_sk = s_store_sk
    AND d_month_seq BETWEEN 1212 + 12 AND 1212 + 23
) AS x
WHERE
  s_store_id1 = s_store_id2 AND d_week_seq1 = d_week_seq2 - 52
ORDER BY
  s_store_name1,
  s_store_id1,
  d_week_seq1

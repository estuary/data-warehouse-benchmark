SELECT
TOP 100
  *
FROM (
  SELECT
    i_category AS i_category,
    i_class AS i_class,
    i_brand AS i_brand,
    s_store_name AS s_store_name,
    s_company_name AS s_company_name,
    d_moy AS d_moy,
    SUM(ss_sales_price) AS sum_sales,
    AVG(CAST(SUM(ss_sales_price) AS FLOAT)) OVER (PARTITION BY i_category, i_brand, s_store_name, s_company_name) AS avg_monthly_sales
  FROM item, store_sales, date_dim, store
  WHERE
    ss_item_sk = i_item_sk
    AND ss_sold_date_sk = d_date_sk
    AND ss_store_sk = s_store_sk
    AND d_year = 1999
    AND (
      (
        i_category IN ('Books', 'Electronics', 'Sports')
        AND i_class IN ('computers', 'stereo', 'football')
      )
      OR (
        i_category IN ('Men', 'Jewelry', 'Women')
        AND i_class IN ('shirts', 'birdal', 'dresses')
      )
    )
  GROUP BY
    i_category,
    i_class,
    i_brand,
    s_store_name,
    s_company_name,
    d_moy
) AS tmp1
WHERE
  CASE
    WHEN (
      avg_monthly_sales <> 0
    )
    THEN (
      CAST(ABS(sum_sales - avg_monthly_sales) AS FLOAT) / NULLIF(avg_monthly_sales, 0)
    )
    ELSE NULL
  END > 0.1
ORDER BY
  CASE WHEN sum_sales - avg_monthly_sales IS NULL THEN 1 ELSE 0 END,
  sum_sales - avg_monthly_sales,
  CASE WHEN s_store_name IS NULL THEN 1 ELSE 0 END,
  s_store_name,
  1,
  2,
  3,
  5,
  6,
  7,
  8

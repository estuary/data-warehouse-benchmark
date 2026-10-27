SELECT
  *
FROM (
  SELECT
    w_warehouse_name,
    i_item_id,
    SUM(
      CASE
        WHEN (
          CAST(d_date AS DATE) < CAST('2000-03-11' AS DATE)
        )
        THEN inv_quantity_on_hand
        ELSE 0
      END
    ) AS inv_before,
    SUM(
      CASE
        WHEN (
          CAST(d_date AS DATE) >= CAST('2000-03-11' AS DATE)
        )
        THEN inv_quantity_on_hand
        ELSE 0
      END
    ) AS inv_after
  FROM inventory, warehouse, item, date_dim
  WHERE
    i_current_price BETWEEN 0.99 AND 1.49
    AND i_item_sk = inv_item_sk
    AND inv_warehouse_sk = w_warehouse_sk
    AND inv_date_sk = d_date_sk
    AND d_date BETWEEN CAST('2000-02-10' AS DATE) AND CAST('2000-04-10' AS DATE)
  GROUP BY
    w_warehouse_name,
    i_item_id
) AS x
WHERE
  (
    CASE
      WHEN inv_before > 0
      THEN (
        inv_after * 1.000
      ) / NULLIF(inv_before, 0)
      ELSE NULL
    END
  ) BETWEEN 2.000 / NULLIF(3.000, 0) AND 3.000 / NULLIF(2.000, 0)
ORDER BY
  w_warehouse_name,
  i_item_id
LIMIT 100

WITH inv AS (
  SELECT
    w_warehouse_name,
    w_warehouse_sk,
    i_item_sk,
    d_moy,
    stdev,
    mean,
    CASE mean WHEN 0 THEN NULL ELSE stdev / NULLIF(mean, 0) END AS cov
  FROM (
    SELECT
      w_warehouse_name,
      w_warehouse_sk,
      i_item_sk,
      d_moy,
      STDDEV_SAMP(inv_quantity_on_hand) * 1.000 AS stdev,
      AVG(inv_quantity_on_hand) AS mean
    FROM inventory, item, warehouse, date_dim
    WHERE
      inv_item_sk = i_item_sk
      AND inv_warehouse_sk = w_warehouse_sk
      AND inv_date_sk = d_date_sk
      AND d_year = 2001
    GROUP BY
      w_warehouse_name,
      w_warehouse_sk,
      i_item_sk,
      d_moy
  ) AS foo
  WHERE
    CASE mean WHEN 0 THEN 0 ELSE stdev / NULLIF(mean, 0) END > 1
)
SELECT
  inv1.w_warehouse_sk AS wsk1,
  inv1.i_item_sk AS isk1,
  inv1.d_moy AS dmoy1,
  inv1.mean AS mean1,
  inv1.cov AS cov1,
  inv2.w_warehouse_sk,
  inv2.i_item_sk,
  inv2.d_moy,
  inv2.mean,
  inv2.cov
FROM inv AS inv1, inv AS inv2
WHERE
  inv1.i_item_sk = inv2.i_item_sk
  AND inv1.w_warehouse_sk = inv2.w_warehouse_sk
  AND inv1.d_moy = 1
  AND inv2.d_moy = 1 + 1
ORDER BY
  inv1.w_warehouse_sk,
  inv1.i_item_sk,
  inv1.d_moy,
  inv1.mean,
  inv1.cov,
  inv2.d_moy,
  inv2.mean,
  inv2.cov

SELECT
  AdvEngineID,
  COUNT_BIG(*)
FROM hits
WHERE
  AdvEngineID <> 0
GROUP BY
  AdvEngineID
ORDER BY
  COUNT_BIG(*) DESC

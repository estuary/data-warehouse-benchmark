SELECT
TOP 10
  RegionID,
  SUM(AdvEngineID),
  COUNT_BIG(*) AS c,
  AVG(CAST(ResolutionWidth AS FLOAT)),
  COUNT_BIG(DISTINCT UserID)
FROM hits
GROUP BY
  RegionID
ORDER BY
  c DESC

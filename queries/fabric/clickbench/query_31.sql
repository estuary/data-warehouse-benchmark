SELECT
TOP 10
  SearchEngineID,
  ClientIP,
  COUNT_BIG(*) AS c,
  SUM(IsRefresh),
  AVG(CAST(ResolutionWidth AS FLOAT))
FROM hits
WHERE
  SearchPhrase <> ''
GROUP BY
  SearchEngineID,
  ClientIP
ORDER BY
  c DESC

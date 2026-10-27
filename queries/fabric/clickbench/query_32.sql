SELECT
TOP 10
  WatchID,
  ClientIP,
  COUNT_BIG(*) AS c,
  SUM(IsRefresh),
  AVG(CAST(ResolutionWidth AS FLOAT))
FROM hits
WHERE
  SearchPhrase <> ''
GROUP BY
  WatchID,
  ClientIP
ORDER BY
  c DESC

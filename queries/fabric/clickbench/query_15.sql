SELECT
TOP 10
  SearchEngineID,
  SearchPhrase,
  COUNT_BIG(*) AS c
FROM hits
WHERE
  SearchPhrase <> ''
GROUP BY
  SearchEngineID,
  SearchPhrase
ORDER BY
  c DESC

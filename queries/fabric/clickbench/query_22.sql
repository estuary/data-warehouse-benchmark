SELECT
TOP 10
  SearchPhrase,
  MIN(URL),
  COUNT_BIG(*) AS c
FROM hits
WHERE
  URL LIKE '%google%' AND SearchPhrase <> ''
GROUP BY
  SearchPhrase
ORDER BY
  c DESC

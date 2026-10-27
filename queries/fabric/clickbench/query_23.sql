SELECT
TOP 10
  SearchPhrase,
  MIN(URL),
  MIN(Title),
  COUNT_BIG(*) AS c,
  COUNT_BIG(DISTINCT UserID)
FROM hits
WHERE
  Title LIKE '%Google%' AND URL NOT LIKE '%.google.%' AND SearchPhrase <> ''
GROUP BY
  SearchPhrase
ORDER BY
  c DESC

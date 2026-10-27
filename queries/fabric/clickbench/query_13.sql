SELECT
TOP 10
  SearchPhrase,
  COUNT_BIG(*) AS c
FROM hits
WHERE
  SearchPhrase <> ''
GROUP BY
  SearchPhrase
ORDER BY
  c DESC

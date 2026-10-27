SELECT
TOP 10
  SearchPhrase,
  COUNT_BIG(DISTINCT UserID) AS u
FROM hits
WHERE
  SearchPhrase <> ''
GROUP BY
  SearchPhrase
ORDER BY
  u DESC

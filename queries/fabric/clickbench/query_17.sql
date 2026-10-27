SELECT
TOP 10
  UserID,
  SearchPhrase,
  COUNT_BIG(*)
FROM hits
GROUP BY
  UserID,
  SearchPhrase
ORDER BY
  COUNT_BIG(*) DESC

SELECT
TOP 10
  UserID,
  DATEPART(MINUTE, EventTime) AS m,
  SearchPhrase,
  COUNT_BIG(*)
FROM hits
GROUP BY
  UserID,
  DATEPART(MINUTE, EventTime),
  SearchPhrase
ORDER BY
  COUNT_BIG(*) DESC

SELECT
TOP 10
  SearchPhrase
FROM hits
WHERE
  SearchPhrase <> ''
ORDER BY
  CASE WHEN EventTime IS NULL THEN 1 ELSE 0 END, EventTime

SELECT
  URL,
  COUNT_BIG(*) AS PageViews
FROM hits
WHERE
  CounterID = 62
  AND EventDate >= '2013-07-01'
  AND EventDate <= '2013-07-31'
  AND IsRefresh = 0
  AND IsLink <> 0
  AND IsDownload = 0
GROUP BY
  URL
ORDER BY
  PageViews DESC
OFFSET 1000 ROWS
FETCH FIRST 10 ROWS ONLY

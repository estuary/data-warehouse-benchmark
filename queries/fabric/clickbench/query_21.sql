SELECT
  COUNT_BIG(*)
FROM hits
WHERE
  URL LIKE '%google%'

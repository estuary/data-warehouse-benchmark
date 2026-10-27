SELECT
TOP 10
  MobilePhoneModel,
  COUNT_BIG(DISTINCT UserID) AS u
FROM hits
WHERE
  MobilePhoneModel <> ''
GROUP BY
  MobilePhoneModel
ORDER BY
  u DESC

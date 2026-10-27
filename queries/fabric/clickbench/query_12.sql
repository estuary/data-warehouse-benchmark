SELECT
TOP 10
  MobilePhone,
  MobilePhoneModel,
  COUNT_BIG(DISTINCT UserID) AS u
FROM hits
WHERE
  MobilePhoneModel <> ''
GROUP BY
  MobilePhone,
  MobilePhoneModel
ORDER BY
  u DESC

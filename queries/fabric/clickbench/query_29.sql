-- HAND-MAINTAINED T-SQL rewrite (see the fabric NOTE in translator.py):
-- Fabric DW has no REGEXP_REPLACE, so the canonical
--   REGEXP_REPLACE(Referer, '^https?://(?:www\.)?([^/]+)/.*$', '\1')
-- is emulated with string functions. Semantics preserved: when the pattern
-- matches (protocol, optional www., >=1 non-slash char, then a slash) the
-- domain is extracted; otherwise the raw Referer passes through unchanged,
-- exactly like REGEXP_REPLACE's no-match behavior. The DW's binary collation
-- keeps LIKE case-sensitive, matching the regex. RE2's `.` does not match a
-- newline, so a referer with an LF after the domain's slash is a regex
-- NO-match (raw referer passes through) — the CHARINDEX(CHAR(10), ...) guard
-- reproduces that (verified vs ground truth: without it 3 domain groups
-- over-count by 14-61 rows at SF100). Re-running `translate` will clobber
-- this file back to REGEXP_REPLACE; validate-sf1 catches that as an engine
-- error on this query.
SELECT
TOP 25
  k,
  AVG(CAST(DATALENGTH(Referer) AS FLOAT)) AS l,
  COUNT_BIG(*) AS c,
  MIN(Referer)
FROM (
  SELECT
    Referer,
    CASE
      WHEN stripped IS NOT NULL
        AND CHARINDEX('/', stripped) > 1
        AND CHARINDEX(CHAR(10), stripped, CHARINDEX('/', stripped)) = 0
      THEN LEFT(stripped, CHARINDEX('/', stripped) - 1)
      ELSE Referer
    END AS k
  FROM (
    SELECT
      Referer,
      CASE
        WHEN Referer LIKE 'https://www.%' THEN SUBSTRING(Referer, 13, LEN(Referer))
        WHEN Referer LIKE 'http://www.%' THEN SUBSTRING(Referer, 12, LEN(Referer))
        WHEN Referer LIKE 'https://%' THEN SUBSTRING(Referer, 9, LEN(Referer))
        WHEN Referer LIKE 'http://%' THEN SUBSTRING(Referer, 8, LEN(Referer))
        ELSE NULL
      END AS stripped
    FROM hits
    WHERE
      Referer <> ''
  ) AS proto
) AS domains
GROUP BY
  k
HAVING
  COUNT_BIG(*) > 100000
ORDER BY
  l DESC

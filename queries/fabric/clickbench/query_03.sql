SELECT
  SUM(AdvEngineID),
  COUNT_BIG(*),
  AVG(CAST(ResolutionWidth AS FLOAT))
FROM hits

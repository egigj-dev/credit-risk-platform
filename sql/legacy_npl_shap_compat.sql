-- Make monthly NPL scoring replay-safe for the same score date.
DELETE FROM ml.NPL_SHAP_Values older
USING ml.NPL_SHAP_Values newer
WHERE older.NPLSHAPRecordID < newer.NPLSHAPRecordID
  AND older.ApplicationID = newer.ApplicationID
  AND older.ScoreDate = newer.ScoreDate
  AND older.FeatureName = newer.FeatureName
  AND older.Horizon = newer.Horizon;

CREATE UNIQUE INDEX IF NOT EXISTS UQ_NPL_SHAP_AppDateFeature
ON ml.NPL_SHAP_Values (ApplicationID, ScoreDate, FeatureName, Horizon);


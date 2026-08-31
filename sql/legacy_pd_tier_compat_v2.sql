-- The merged DDL names this constraint CK_RiskTier, not CK_PD_RiskTier.
ALTER TABLE ml.PD_Scores DROP CONSTRAINT IF EXISTS CK_RiskTier;
ALTER TABLE ml.PD_Scores DROP CONSTRAINT IF EXISTS CK_PD_RiskTier;
ALTER TABLE ml.PD_Scores ADD CONSTRAINT CK_RiskTier
  CHECK (RiskTier IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL'));

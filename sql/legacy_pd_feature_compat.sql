-- Derived by legacy credit_risk_etl_pg.py but omitted from the merged FeatureStore DDL.
ALTER TABLE ml.FeatureStore ADD COLUMN IF NOT EXISTS bureau_active_ratio NUMERIC(12,6);

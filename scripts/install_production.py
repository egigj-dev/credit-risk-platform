"""Self-contained, idempotent installer for the full production platform."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
FK = """,\n    CONSTRAINT FK_Training_ModelReg  FOREIGN KEY (GovernanceCycleID) REFERENCES ml.ModelRegistry(CycleID)
        DEFERRABLE INITIALLY DEFERRED"""
COMPATIBILITY_SQL = (
    "legacy_loader_compat.sql",
    "legacy_pd_feature_compat.sql",
    "legacy_pd_tier_compat_v2.sql",
    "legacy_npl_shap_compat.sql",
    "legacy_nbo_cooldown_compat.sql",
)


def main() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is required")
    ddl = (ROOT / "legacy/sql/credit_risk_ddl_pg.sql").read_text(encoding="utf-8")
    if FK not in ddl:
        raise RuntimeError("Expected TrainingDatasetRegistry FK was not found in the vendored DDL")
    procedures = (ROOT / "legacy/sql/credit_risk_proc_pg.sql").read_text(encoding="utf-8")
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        cur.execute("CREATE SCHEMA IF NOT EXISTS stg")
        cur.execute("CREATE SCHEMA IF NOT EXISTS ml")
        cur.execute("CREATE SCHEMA IF NOT EXISTS rpt")
        cur.execute(ddl.replace(FK, ""))
        cur.execute(
            """
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_training_modelreg') THEN
                ALTER TABLE ml.TrainingDatasetRegistry
                  ADD CONSTRAINT FK_Training_ModelReg
                  FOREIGN KEY (GovernanceCycleID) REFERENCES ml.ModelRegistry(CycleID)
                  DEFERRABLE INITIALLY DEFERRED;
              END IF;
            END $$
            """
        )
        cur.execute(procedures)
        for filename in COMPATIBILITY_SQL:
            cur.execute((ROOT / "sql" / filename).read_text(encoding="utf-8"))
    print("Production schema, procedures, and compatibility fixes applied")


if __name__ == "__main__":
    main()

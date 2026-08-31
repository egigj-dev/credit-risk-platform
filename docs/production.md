# Production runbook

## Install and load

Create the `credit_risk_db_v2` database, set `DATABASE_URL`, then run the single
idempotent installer:

```bash
.venv/bin/python scripts/install_production.py
```

Validate the seven required Home Credit inputs, import the three artifact groups, and
load source records as shown in the root README. For a referentially valid smoke sample:

```bash
.venv/bin/python scripts/seed_homecredit_compat.py \
  --data-dir /path/to/data --sample 1000 \
  --dbname credit_risk_db_v2 --user credit_risk_app --password change-me
```

POS history is optional for the initial MVP load. The vendored
`legacy/pipelines/seed_pos_cash_pg.py` supports the full POS/backfill workflow.

## Models and daily operation

Imported NBO nearest-neighbour artifacts are population-specific. If the database client
count differs from `nbo_artifacts/nbo_config.json`, retrain after refreshing ownership:

```bash
.venv/bin/credit-risk-model nbo --mode ownership
.venv/bin/credit-risk-model nbo --mode train
```

Run the prior complete business date. Ownership refresh is normally monthly rather than
daily:

```bash
APP_MODE=database ARTIFACT_DIR=./artifacts \
  .venv/bin/credit-risk-full --run-date YYYY-MM-DD --refresh-ownership
```

The sequence is staging, validation, raw features, PD/PSI/SHAP, NPL/SHAP, NBO, and
reporting. A completed preprocessing date is checkpointed for safe replay. Current-day
NBO rows do not suppress themselves; accepted or prior-day offers respect cooldown.

## Serve and verify

```bash
APP_MODE=database ARTIFACT_DIR=./artifacts \
  .venv/bin/uvicorn credit_risk.api.production_main:app --host 0.0.0.0 --port 8002

.venv/bin/python scripts/verify_production.py --database-url "$DATABASE_URL"
```

The verifier is read-only. It checks non-empty features and all model/reporting outputs,
latest required ETL statuses, dashboard/readiness, CRM PD/NPL/NBO/Client 360 endpoints,
pipeline status, and overall health.

Use an external scheduler for daily execution and add authentication/authorization before
deployment. Keep raw data, artifacts, credentials, and MLflow stores outside Git.


# Credit Risk Platform

A credit-risk MVP that scores clients for probability of default (PD) and non-performing-loan early warning (NPL), recommends next-best offers (NBO), and exposes results through an API and an operator dashboard.

- PostgreSQL 16 source staging, watermarking, validation, and a 131-feature store.
- CatBoost PD scoring with PSI monitoring and SHAP explanations.
- XGBoost NPL early-warning scoring, four risk tiers, watchlists, and SHAP output.
- Next-best-offer recommendations with ownership, cold-start handling, PD suppression, cooldowns, and CRM acceptance feedback.
- Client 360, portfolio and reporting views, Power BI assets, pipeline audit and health APIs, Swagger documentation, and a responsive dashboard.
- A deterministic demo with generated clients, which needs no database, data or trained models.

## Quick UI demo

```bash
python3.10 -m venv .venv
.venv/bin/pip install -e '.[dev]'
make demo
```

Open <http://localhost:8002>. Search/filter clients, inspect PD and NPL risk, review
owned products, accept offers, and exercise the visible pipeline workflow.

## Full platform

Python 3.10 is required because the imported model environment was trained on Python
3.10.11.

```bash
.venv/bin/pip install -e '.[dev,ml]'
docker compose up -d postgres
export DATABASE_URL='postgresql://credit_risk_app:change-me@localhost:5432/credit_risk_db_v2'

# Idempotent schema, procedure, reporting, and compatibility install
.venv/bin/python scripts/install_production.py

# Validate and import the existing data/model assets
.venv/bin/credit-risk check-data --data-dir /path/to/credit_risk_etl_v2/data
.venv/bin/credit-risk import-legacy-artifacts \
  --source /path/to/credit_risk_etl_v2 --destination ./artifacts
.venv/bin/python scripts/seed_homecredit_compat.py \
  --data-dir /path/to/credit_risk_etl_v2/data \
  --dbname credit_risk_db_v2 --user credit_risk_app --password change-me

# Monthly ownership refresh plus the replay-safe daily pipeline
APP_MODE=database ARTIFACT_DIR=./artifacts \
  .venv/bin/credit-risk-full --run-date YYYY-MM-DD --refresh-ownership

# Full CRM API and dashboard
APP_MODE=database ARTIFACT_DIR=./artifacts \
  .venv/bin/uvicorn credit_risk.api.production_main:app --host 0.0.0.0 --port 8002
```

Open `/` for the dashboard and `/docs` for the complete API. The canonical pipeline
runs staging, validation, raw features, PD, NPL, optional ownership refresh, NBO, and
reporting. Replaying a completed date reuses its validated preprocessing checkpoint.
NBO refuses to score when its nearest-neighbour artifacts were trained against a
different client population.

Use `credit-risk-model` for individual PD/NPL/NBO train, evaluate, or score operations.
See [the production runbook](docs/production.md) for operations and verification.

## Testing

```bash
.venv/bin/pip install -e '.[dev]'
make test
```

## What this repository contains

The demo mode and its tests run from a fresh clone. Demo clients and scores are generated, and are not model output. The full platform needs a PostgreSQL database, the source dataset, and trained model artifacts, none of which are in Git, and model performance figures are not published here.

## Documentation

- [How it works and local installation](docs/pipeline-guide.md) — an approachable guide
  with PD, NPL, NBO, and combined Mermaid diagrams.
- [Architecture](docs/architecture.md) — system modes and orchestration boundaries.
- [Production runbook](docs/production.md) — loading, scoring, and verification.
- [Operations and safety](docs/operations.md) — release and governance reminders.

## Safety boundary

This is an operational MVP, not an approved automated credit-decision system. Thresholds,
adverse-action reasons, access controls, model governance, and deployment controls need
institution-specific review before production use. Raw financial data, trained pickles,
credentials, and MLflow stores are intentionally excluded from Git.

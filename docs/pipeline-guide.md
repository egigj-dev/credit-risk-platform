# How the Credit Risk Platform Works

This guide is the shortest path from a fresh clone to a working local dashboard. It
also explains the three decision pipelines without requiring a machine-learning
background.

## The 30-second version

The platform answers three questions in order:

1. **PD:** How likely is this customer to default?
2. **NPL:** Is an existing loan showing signs of distress?
3. **NBO:** If risk is acceptable, which useful product should be offered next?

PD and NPL provide SHAP explanations. NBO is a hybrid recommender, so its explanation
is based on recommendation method, product affinity, and business-rule provenance—not
SHAP.

```mermaid
flowchart LR
    A["Customer data"] --> B["PD<br/>Default risk"]
    B --> C["NPL<br/>Early warning"]
    B --> D{"Safe to recommend?"}
    C --> D
    D -->|Yes| E["NBO<br/>Best products"]
    D -->|No| F["Support first<br/>No marketing"]
    B --> G["One customer view"]
    C --> G
    E --> G
    F --> G
    G --> H["Dashboard + CRM"]

    classDef input fill:#e8f1ff,stroke:#3973ac;
    classDef risk fill:#eee8ff,stroke:#7559b5;
    classDef decision fill:#fff4d6,stroke:#c58b18;
    classDef positive fill:#e7f7ea,stroke:#3a8b4f;
    classDef blocked fill:#fde8e8,stroke:#c84b4b;
    classDef output fill:#e8f7f5,stroke:#278579;

    class A input;
    class B,C risk;
    class D decision;
    class E positive;
    class F blocked;
    class G,H output;
```

## Choose a local setup

| Goal | Mode | PostgreSQL | Model files | Raw data |
|---|---|---:|---:|---:|
| Explore the UI and API | Demo | No | No | No |
| Exercise the real pipeline | Full | Yes | Yes | Yes |

Start with **demo mode** unless you specifically need database-backed scoring.

## Requirements

- Git
- Python 3.10 (the imported model environment requires Python `>=3.10,<3.11`)
- Docker with Docker Compose, only for full mode

Commands below use a macOS/Linux shell. On Windows, use WSL for the most direct setup.

## Install and run the demo

```bash
git clone YOUR_REPOSITORY_URL credit-risk-platform
cd credit-risk-platform

python3.10 -m venv .venv
.venv/bin/pip install -e '.[dev]'
make demo
```

If `make` is unavailable, start the same server directly:

```bash
APP_MODE=demo .venv/bin/uvicorn credit_risk.api.main:app \
  --host 0.0.0.0 --port 8002 --reload
```

Open:

- Dashboard: <http://localhost:8002>
- Interactive API documentation: <http://localhost:8002/docs>
- Readiness check: <http://localhost:8002/health/ready>

Stop the server with `Ctrl+C`.

## Install and run the full platform

Full mode needs files intentionally excluded from Git: Home Credit CSV inputs and the
trained PD, NPL, and NBO artifacts. Keeping customer data and serialized models out of
the repository is a security boundary, not a missing installation step.

### 1. Install Python and start PostgreSQL

```bash
python3.10 -m venv .venv
.venv/bin/pip install -e '.[dev,ml]'

docker compose up -d postgres
export DATABASE_URL='postgresql://credit_risk_app:change-me@localhost:5432/credit_risk_db_v2'
```

### 2. Install the database objects

The installer is idempotent, so it is safe to run again after a restart.

```bash
.venv/bin/python scripts/install_production.py
```

### 3. Validate data and import model artifacts

Replace `/path/to/credit_risk_etl_v2` with the directory containing the legacy data and
artifact folders.

```bash
.venv/bin/credit-risk check-data \
  --data-dir /path/to/credit_risk_etl_v2/data

.venv/bin/credit-risk import-legacy-artifacts \
  --source /path/to/credit_risk_etl_v2 \
  --destination ./artifacts
```

The required data files are:

- `application_train.csv`
- `application_test.csv`
- `bureau.csv`
- `previous_application.csv`
- `POS_CASH_balance.csv`
- `credit_card_balance.csv`
- `installments_payments.csv`

The imported artifact groups are `model_artifacts`, `npl_artifacts`, and
`nbo_artifacts`.

### 4. Load the source data

For a quick, referentially valid smoke test, load a 1,000-customer sample:

```bash
.venv/bin/python scripts/seed_homecredit_compat.py \
  --data-dir /path/to/credit_risk_etl_v2/data \
  --sample 1000 \
  --dbname credit_risk_db_v2 \
  --user credit_risk_app \
  --password change-me
```

NBO artifacts are tied to the client population used during training. After loading a
different-sized sample, refresh ownership and retrain NBO:

```bash
APP_MODE=database .venv/bin/credit-risk-model nbo --mode ownership
APP_MODE=database .venv/bin/credit-risk-model nbo --mode train
```

### 5. Run the pipeline

Use the prior complete business date for a normal daily run:

```bash
APP_MODE=database ARTIFACT_DIR=./artifacts \
  .venv/bin/credit-risk-full \
  --run-date YYYY-MM-DD \
  --refresh-ownership
```

Ownership is normally refreshed monthly. Daily runs can omit `--refresh-ownership`.

### 6. Start and verify the full application

```bash
APP_MODE=database ARTIFACT_DIR=./artifacts \
  .venv/bin/uvicorn credit_risk.api.production_main:app \
  --host 0.0.0.0 --port 8002
```

In a second terminal:

```bash
export DATABASE_URL='postgresql://credit_risk_app:change-me@localhost:5432/credit_risk_db_v2'
.venv/bin/python scripts/verify_production.py --database-url "$DATABASE_URL"
```

## PD: probability of default

```mermaid
flowchart LR
    A["Customer + credit history"] --> B["PD model"]
    B --> C["Default probability"]
    C --> D["Risk level"]
    B --> E["Top reasons<br/>SHAP"]

    classDef input fill:#e8f1ff,stroke:#3973ac;
    classDef model fill:#eee8ff,stroke:#7559b5;
    classDef result fill:#e7f7ea,stroke:#3a8b4f;
    classDef explain fill:#fff4d6,stroke:#c58b18;

    class A input;
    class B model;
    class C,D result;
    class E explain;
```

The PD pipeline validates and transforms source records, builds the feature store, and
uses the saved champion classifier to produce a probability, risk tier, and portfolio
decile. It also monitors drift with PSI and stores the five strongest SHAP drivers for
each scored application.

Main outputs: `ml.PD_Scores`, `ml.PSI_Monitor`, and `ml.SHAP_Values`.

## NPL: early warning for active loans

```mermaid
flowchart LR
    A["Loan + payment behaviour"] --> B["NPL model"]
    C["Latest PD score"] --> B
    B --> D["Early-warning score"]
    D --> E["Normal · Watch<br/>Concern · Critical"]
    B --> F["Top reasons<br/>SHAP"]

    classDef input fill:#e8f1ff,stroke:#3973ac;
    classDef model fill:#eee8ff,stroke:#7559b5;
    classDef result fill:#e7f7ea,stroke:#3a8b4f;
    classDef explain fill:#fff4d6,stroke:#c58b18;

    class A,C input;
    class B model;
    class D,E result;
    class F explain;
```

The NPL pipeline combines active-loan behaviour with the latest PD context. Its XGBoost
model produces an early-warning probability and assigns percentile-based tiers. The five
largest SHAP contributions explain what moved each score.

Main outputs: `ml.NPL_Scores` and `ml.NPL_SHAP_Values`.

## NBO: next best offer

```mermaid
flowchart LR
    A["Customer profile<br/>and current products"] --> B{"Risk acceptable?"}
    C["PD + NPL results"] --> B
    B -->|No| D["Pause marketing<br/>Offer support"]
    B -->|Yes| E["Recommendation engine"]
    E --> F["Rank suitable products"]
    F --> G["Apply ownership<br/>and cooldown rules"]
    G --> H["Best eligible offers"]
    E --> I["Why recommended?<br/>Method + business rules"]

    classDef input fill:#e8f1ff,stroke:#3973ac;
    classDef decision fill:#fff4d6,stroke:#c58b18;
    classDef model fill:#eee8ff,stroke:#7559b5;
    classDef result fill:#e7f7ea,stroke:#3a8b4f;
    classDef blocked fill:#fde8e8,stroke:#c84b4b;
    classDef explain fill:#fff8e8,stroke:#b38a30;

    class A,C input;
    class B decision;
    class E model;
    class F,G,H result;
    class D blocked;
    class I explain;
```

The NBO pipeline first protects high-risk customers by suppressing marketing. Eligible
customers follow either collaborative filtering or a cold-start cluster route. Business
rules adjust the scores; ownership and cooldown checks remove unsuitable offers.

NBO does not currently produce SHAP values. The engine calculates rule triggers, but the
daily writer currently persists the method and final score rather than the complete rule
provenance. Main output: `ml.NBO_Scores`.

## Repository map

```text
credit_risk_platform/
├── src/credit_risk/       Application, API, orchestration and dashboard
├── legacy/pipelines/      Vendored PD, NPL and NBO model pipelines
├── sql/                   Core migrations and compatibility SQL
├── scripts/               Installation, migration, import and verification tools
├── tests/                 Demo and production contract tests
├── docs/                  Architecture, operations and this guide
├── schemas/               Rendered pipeline diagrams
├── artifacts/             Local model artifacts; ignored by Git
└── data/                  Local raw data; ignored by Git
```

## Useful commands

```bash
make demo                # Start the zero-service demo
make db-up               # Start PostgreSQL
make install-production  # Install all database objects
make pipeline            # Run the full daily pipeline
make production          # Start the database-backed application
make test                # Run tests
make lint                # Run static checks
make db-down             # Stop PostgreSQL
```

## Common problems

### `python3.10: command not found`

Install Python 3.10. Python 3.11 or newer is intentionally rejected by the package
because the imported model environment was trained with Python 3.10.

### Port 8002 is already in use

Stop the other process or replace `--port 8002` with another port. Update
`CORS_ORIGINS` when serving the UI from a different origin.

### `DATABASE_URL is required when APP_MODE=database`

Export the connection string in every terminal that runs a database-mode command:

```bash
export DATABASE_URL='postgresql://credit_risk_app:change-me@localhost:5432/credit_risk_db_v2'
```

### NBO artifact population mismatch

The nearest-neighbour index must match the current database population. Refresh product
ownership and retrain NBO after changing the loaded client sample.

## Safety boundary

This repository is an operational MVP, not an approved automated credit-decision system.
Authentication, authorization, institution-approved thresholds, adverse-action logic,
model governance, monitoring, and deployment controls must be added before production
use. Never commit raw financial data, credentials, trained pickle files, or MLflow stores.

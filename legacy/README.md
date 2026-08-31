# Vendored production source snapshot

This directory contains the selected executable source from the two original projects, so the final project does not lose any capability while the package-level adapters are progressively hardened.

| Capability | Origin | Active entrypoint |
|---|---|---|
| PostgreSQL DDL, staging, 138-feature raw store, NPL views, reporting | `credit_risk_final` | `legacy/sql/*` |
| Home Credit source load | `credit_risk_etl_v2` | `legacy/pipelines/seed_homecredit_pg.py` |
| POS load and feature backfill | `credit_risk_final` | `legacy/pipelines/seed_pos_cash_pg.py` |
| PD training and model artifacts | shared | `legacy/pipelines/credit_scoring.py` |
| PD daily ETL, PSI, SHAP | `credit_risk_etl_v2` | `credit-risk-model pd-etl` |
| NPL train, score, evaluation | `credit_risk_final` | `credit-risk-model npl` |
| NBO ownership, training, daily score | `credit_risk_final` | `credit-risk-model nbo` |
| Drift simulation, Power BI/DAX and workflow reference | `credit_risk_etl_v2` | `legacy/pipelines` and `legacy/reporting` |

Large data and artifacts are intentionally excluded. Use `credit-risk import-legacy-artifacts --source /home/egigj/credit_risk_etl_v2` to bring the artifact bundles into `artifacts/` with checksums.

The original files are retained as an auditable compatibility snapshot. Package runners
inject database configuration, while `credit-risk-full` owns the canonical end-to-end run.

# Operations and safety

- Never commit raw CSVs, trained pickles, passwords or MLflow databases.
- Use `.env` for local settings and a secret manager in deployed environments.
- Apply migrations twice in a disposable PostgreSQL instance before release.
- Validate score counts, tiers and recommendation exclusions before a cutover.
- The dashboard is an operational MVP, not a credit-decision system. Any decision thresholds, adverse-action logic and model-governance controls need institution-specific approval.

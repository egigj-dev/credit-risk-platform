"""Command-line entrypoint for the complete production pipeline."""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timedelta, timezone

from credit_risk.config import Settings
from credit_risk.production import run_daily


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the complete PD/NPL/NBO pipeline")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    default_run_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    parser.add_argument("--run-date", default=str(default_run_date))
    parser.add_argument("--refresh-ownership", action="store_true")
    args = parser.parse_args()
    base = Settings.from_env()
    settings = Settings(
        "database", args.database_url, base.artifact_dir, base.data_dir, base.cors_origins
    )
    result = run_daily(
        date.fromisoformat(args.run_date),
        settings,
        refresh_ownership=args.refresh_ownership,
    )
    print(result.as_dict())


if __name__ == "__main__":
    main()

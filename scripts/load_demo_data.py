"""Load a deterministic portfolio into the PostgreSQL MVP schema."""

from __future__ import annotations

import os

import psycopg2
import psycopg2.extras

from credit_risk.core.demo import DemoPortfolio


def main() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is required")
    portfolio = DemoPortfolio()
    rows = [
        (
            c["client_id"],
            c["name"],
            c["segment"],
            c["annual_income"],
            c["utilisation"],
            c["days_past_due"],
        )
        for c in portfolio.clients.values()
    ]
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO public.clients(client_id,full_name,segment,annual_income,utilisation,days_past_due)
          VALUES %s ON CONFLICT (client_id) DO UPDATE SET full_name=EXCLUDED.full_name,segment=EXCLUDED.segment,annual_income=EXCLUDED.annual_income,utilisation=EXCLUDED.utilisation,days_past_due=EXCLUDED.days_past_due""",
            rows,
        )
    print(f"Loaded {len(rows)} demo clients")


if __name__ == "__main__":
    main()

"""Apply ordered SQL migrations to DATABASE_URL. Safe to rerun."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is required")
    migrations = sorted((ROOT / "sql" / "migrations").glob("*.sql"))
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        for migration in migrations:
            print(f"Applying {migration.name}")
            cur.execute(migration.read_text(encoding="utf-8"))
    print(f"Applied {len(migrations)} migrations")


if __name__ == "__main__":
    main()

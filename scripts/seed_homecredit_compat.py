"""Run the v2 Home Credit loader with referentially safe sampling."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_module():
    path = ROOT / "legacy/pipelines/seed_homecredit_pg.py"
    spec = importlib.util.spec_from_file_location("legacy_home_credit_seed", path)
    if not spec or not spec.loader:
        raise RuntimeError("Unable to load vendored Home Credit seed script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser(description="Referentially safe Home Credit source loader")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--dbname", default="credit_risk_db_v2")
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default="")
    args = parser.parse_args()
    source = source_module()
    app, bureau, credit_card, previous, installments = source.load_csvs(args.data_dir, args.sample)
    # The upstream sample loader filters prior applications but not their payment rows.
    # Restricting by SK_ID_PREV preserves the InstallmentsPayments FK contract.
    installments = installments[installments["SK_ID_PREV"].isin(set(previous["SK_ID_PREV"]))].copy()
    conn = source.get_connection(args)
    try:
        source.insert_clients(conn, source.build_clients(app))
        source.insert_applications(conn, source.build_applications(app))
        source.insert_bureau_snapshots(conn, source.build_bureau_snapshots(app, bureau))
        source.insert_transactions(conn, source.build_transactions(credit_card, app))
        source.insert_bureau_loans(conn, source.build_bureau_loans(bureau))
        source.insert_previous_applications(conn, source.build_previous_applications(previous))
        source.insert_installments(conn, source.build_installments(installments))
    finally:
        conn.close()
    print(
        f"Loaded {len(app):,} clients and {len(installments):,} referentially valid installment rows"
    )


if __name__ == "__main__":
    main()

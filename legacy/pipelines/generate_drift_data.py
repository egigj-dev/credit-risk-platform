"""
generate_drift_data.py
======================
Injects synthetic "drifted" population into the public source tables so the
full ETL pipeline (staging → feature store → scoring → PSI) can be run and
the PSI monitor can be observed to fire.

All synthetic records live in a RESERVED ID range — rollback is a clean
DELETE in reverse FK order.

RESERVED RANGES
---------------
  ClientID       : 9_900_001 – 9_900_000 + N_CLIENTS
  ApplicationID  : 99_000_001 – 99_000_000 + N_CLIENTS
  SnapshotID     : 999_900_001 – 999_900_000 + N_CLIENTS
  TxnID          : 9_990_000_001 + (client_offset * TXN_PER_CLIENT + txn_idx)

Usage
-----
  # inject (default 3 000 clients)
  python generate_drift_data.py --mode inject

  # inject a specific volume
  python generate_drift_data.py --mode inject --n-clients 5000

  # verify what's sitting in the DB
  python generate_drift_data.py --mode verify

  # full cleanup — removes ALL synthetic rows from every table
  python generate_drift_data.py --mode rollback

After injecting, run the normal pipeline:
  CALL stg.usp_StageSourceData(NULL);       -- in psql
  CALL stg.usp_ValidateStagedData(NULL);
  CALL ml.usp_BuildFeatureStoreRaw(NULL);
  python credit_risk_etl_pg.py --run-date 2026-04-24

DRIFT DESIGN  (compound = feature distribution + score shift)
-------------------------------------------------------------
Feature (input) drift:
  ExtScore1/2/3   : shifted from ~N(0.50, 0.15) → N(0.15, 0.07)  [much worse bureau]
  IncomeTotal     : reduced ~55 % vs training population
  ActiveLoans     : 3–9  (training: 0–3)
  EnquiriesYear   : 4–12 (training: 0–3)
  Days_Employed   : mostly positive (unemployed) or very short tenure
  OwnCar/Realty   : mostly False
  RegionRating    : skewed to 3 (worst region)
  OverdraftDays   : 20–60 (training: 0–5) — via transaction behaviour

Behavioural (transaction) drift:
  Avg/min balance : near-zero or negative
  OVERDRAFT txns  : majority of transaction types

Score (output) shift — emerges naturally from the above:
  PD scores shift toward HIGH / VERY_HIGH tiers → PSI flags RETRAIN.
"""

import argparse
import logging
import os
import sys
from datetime import date, timedelta

import numpy as np
import psycopg2
import psycopg2.extras
import psycopg2.extensions

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("drift_gen")

# ── Connection ────────────────────────────────────────────────────────────────
PG_CONN_PARAMS = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   "credit_risk_db_v2",
    "user":     "postgres",
    "password": os.getenv("PG_PASS", ""),
}

# ── Reserved ID ranges ────────────────────────────────────────────────────────
CLIENT_ID_BASE      = 9_900_001
APP_ID_BASE         = 99_000_001
SNAPSHOT_ID_BASE    = 999_900_001
TXN_ID_BASE         = 9_990_000_001
TXN_PER_CLIENT      = 30          # 30 transactions per synthetic client (90-day window)

# ── Controlled seed for reproducibility ──────────────────────────────────────
RNG_SEED = 42

# ── Drift parameters ──────────────────────────────────────────────────────────
#    Each tuple is (mean, std, clip_lo, clip_hi).
#    Compare to training population to understand the magnitude of shift.
DRIFT = {
    # Bureau external scores — deliberately terrible
    "ext_score1": dict(mu=0.15, sd=0.07, lo=0.01, hi=0.40),
    "ext_score2": dict(mu=0.20, sd=0.09, lo=0.01, hi=0.45),
    "ext_score3": dict(mu=0.16, sd=0.07, lo=0.01, hi=0.38),
    # Income — reduced
    "income_lo":  45_000,
    "income_hi":  120_000,
    # Loan — high relative to income to push debt-to-income ratio up
    "loan_lo":    150_000,
    "loan_hi":    600_000,
    "tenor_lo":   60,
    "tenor_hi":   120,
    # Bureau activity — many open loans and recent enquiries
    "active_loans_lo":  3,
    "active_loans_hi":  9,
    "closed_loans_lo":  1,
    "closed_loans_hi":  6,
    "enquiries_lo":     4,
    "enquiries_hi":     12,
    # Employment — short tenure or unemployed
    # Days_Employed convention: negative = employed (Home Credit)
    # positive = unemployed / no employer on record
    "unemployed_prob":  0.45,      # 45 % are unemployed
    "tenure_days_lo":  -270,       # max ~9 months employed
    "tenure_days_hi":  -15,        # min 2 weeks employed
    # Transaction behaviour — overdraft-heavy
    "overdraft_days_lo": 20,
    "overdraft_days_hi": 60,
    "avg_balance_lo":   -5_000,
    "avg_balance_hi":    2_000,
}

EDUCATION_TYPES = [
    "Secondary / secondary special",
    "Secondary / secondary special",  # duplicated to weight it higher
    "Lower secondary",
    "Lower secondary",
    "Incomplete higher",
    "Higher education",
]
FAMILY_STATUSES = ["Single / not married", "Civil marriage", "Married", "Separated"]
INCOME_TYPES    = [
    "Working",
    "Working",
    "Commercial associate",
    "Unemployed",
    "State servant",
    "Pensioner",
]
TXN_TYPES = ["OVERDRAFT", "OVERDRAFT", "DEBIT", "ATM", "FEE", "CREDIT"]
CHANNELS  = ["ATM", "ONLINE", "BRANCH", "POS"]


# ═══════════════════════════════════════════════════════════════════════════════
# GENERATORS
# ═══════════════════════════════════════════════════════════════════════════════

def generate_clients(n: int, rng: np.random.Generator) -> list[dict]:
    """
    Synthetic clients with deliberately bad risk profiles.
    Days_Employed uses Home Credit sign convention:
      negative → currently employed (tenure = abs(days))
      positive → unemployed / N/A
    """
    today = date.today()
    rows = []
    for i in range(n):
        cid = CLIENT_ID_BASE + i
        age_years = int(rng.integers(22, 65))
        birth_date = today - timedelta(days=age_years * 365 + int(rng.integers(0, 365)))

        unemployed = rng.random() < DRIFT["unemployed_prob"]
        if unemployed:
            days_employed = int(rng.integers(1, 365))   # positive = unemployed
        else:
            days_employed = int(rng.integers(DRIFT["tenure_days_lo"], DRIFT["tenure_days_hi"]))

        income = float(rng.uniform(DRIFT["income_lo"], DRIFT["income_hi"]))
        n_children = int(rng.integers(0, 6))

        rows.append({
            "ClientID":       cid,
            "BirthDate":      birth_date,
            "Gender":         rng.choice(["M", "F"]),
            "EducationType":  rng.choice(EDUCATION_TYPES),
            "FamilyStatus":   rng.choice(FAMILY_STATUSES),
            "IncomeTotal":    round(income, 2),
            "IncomeType":     rng.choice(INCOME_TYPES),
            "OwnCar":         bool(rng.random() < 0.15),    # mostly no car
            "OwnRealty":      bool(rng.random() < 0.10),    # mostly no realty
            "CNTChildren":    n_children,
            "CNTFamilyMembers": max(1, n_children + int(rng.integers(1, 3))),
            "RegionRating":   int(rng.choice([2, 3, 3, 3])),  # skewed to worst
            "Days_Employed":  days_employed,
        })
    log.info("Generated %d clients (IDs %d – %d)", n, CLIENT_ID_BASE, CLIENT_ID_BASE + n - 1)
    return rows


def generate_applications(clients: list[dict], rng: np.random.Generator,
                           app_date: date) -> list[dict]:
    """One loan application per client, with high LoanAmount / Income ratio."""
    rows = []
    for i, c in enumerate(clients):
        aid = APP_ID_BASE + i
        loan  = float(rng.uniform(DRIFT["loan_lo"], DRIFT["loan_hi"]))
        tenor = int(rng.integers(DRIFT["tenor_lo"], DRIFT["tenor_hi"]))
        # Annuity: roughly loan / tenor * 1.15 (high interest factor)
        annuity = round(loan / tenor * 1.15, 2)
        goods   = round(loan * rng.uniform(0.90, 1.05), 2)
        rows.append({
            "ApplicationID":   aid,
            "ClientID":        c["ClientID"],
            "ApplicationDate": app_date,
            "LoanAmount":      round(loan, 2),
            "Annuity":         annuity,
            "LoanTenorMonths": tenor,
            "GoodsPriceAmount": goods,
            "ContractType":    rng.choice(["Cash loans", "Cash loans", "Revolving loans"]),
            "TARGET":          None,   # no label for new applications
        })
    log.info("Generated %d applications", len(rows))
    return rows


def generate_bureau_snapshots(clients: list[dict], rng: np.random.Generator,
                               snap_date: date) -> list[dict]:
    """Poor bureau profiles: low ext scores, many active loans, high enquiries."""
    rows = []
    for i, c in enumerate(clients):
        sid = SNAPSHOT_ID_BASE + i

        def _clipped_normal(p: dict) -> float:
            v = rng.normal(p["mu"], p["sd"])
            return float(np.clip(v, p["lo"], p["hi"]))

        rows.append({
            "SnapshotID":    sid,
            "ClientID":      c["ClientID"],
            "SnapshotDate":  snap_date,
            "ExtScore1":     round(_clipped_normal(DRIFT["ext_score1"]), 4),
            "ExtScore2":     round(_clipped_normal(DRIFT["ext_score2"]), 4),
            "ExtScore3":     round(_clipped_normal(DRIFT["ext_score3"]), 4),
            "ActiveLoans":   int(rng.integers(DRIFT["active_loans_lo"], DRIFT["active_loans_hi"] + 1)),
            "ClosedLoans":   int(rng.integers(DRIFT["closed_loans_lo"], DRIFT["closed_loans_hi"] + 1)),
            "EnquiriesYear": int(rng.integers(DRIFT["enquiries_lo"], DRIFT["enquiries_hi"] + 1)),
        })
    log.info("Generated %d bureau snapshots", len(rows))
    return rows


def generate_transactions(clients: list[dict], rng: np.random.Generator,
                           end_date: date) -> list[dict]:
    """
    30 transactions per client over the 90-day window before end_date.
    Overdraft-heavy with near-zero or negative running balances —
    this pushes OverdraftDays_90d and AvgBalance_90d into the drift zone.
    """
    start_date = end_date - timedelta(days=89)
    date_range  = (end_date - start_date).days + 1
    rows = []
    txn_counter = 0

    for i, c in enumerate(clients):
        balance = float(rng.uniform(DRIFT["avg_balance_lo"], DRIFT["avg_balance_hi"]))
        for j in range(TXN_PER_CLIENT):
            txn_id  = TXN_ID_BASE + i * TXN_PER_CLIENT + j
            txn_day = start_date + timedelta(days=int(rng.integers(0, date_range)))
            txn_type = rng.choice(TXN_TYPES)

            if txn_type in ("OVERDRAFT", "DEBIT", "ATM", "FEE"):
                amount  = float(-rng.uniform(500, 8_000))
            else:
                amount  = float(rng.uniform(500, 5_000))
            balance += amount

            rows.append({
                "TxnID":    txn_id,
                "ClientID": c["ClientID"],
                "TxnDate":  txn_day,
                "TxnType":  txn_type,
                "Amount":   round(amount, 2),
                "Balance":  round(balance, 2),
                "Channel":  rng.choice(CHANNELS),
            })
            txn_counter += 1

    log.info("Generated %d transactions (%d per client)", txn_counter, TXN_PER_CLIENT)
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# DB OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_conn() -> psycopg2.extensions.connection:
    conn = psycopg2.connect(**PG_CONN_PARAMS)
    conn.autocommit = False
    return conn


def _bulk_insert(cur, table: str, cols: list[str], rows: list[dict],
                 batch: int = 5_000) -> int:
    """execute_values in batches; returns total rows inserted."""
    template = "(%s)" % ",".join(["%s"] * len(cols))
    total = 0
    for start in range(0, len(rows), batch):
        chunk = rows[start: start + batch]
        values = [tuple(r[c] for c in cols) for r in chunk]
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s",
            values,
            template=template,
            page_size=batch,
        )
        total += len(chunk)
    return total


def inject(n_clients: int) -> None:
    rng      = np.random.default_rng(RNG_SEED)
    run_date = date.today()

    clients  = generate_clients(n_clients, rng)
    apps     = generate_applications(clients, rng, run_date)
    bureau   = generate_bureau_snapshots(clients, rng, run_date - timedelta(days=1))
    txns     = generate_transactions(clients, rng, run_date)

    conn = get_conn()
    try:
        cur = conn.cursor()

        # ── Clients ──────────────────────────────────────────────────────────
        client_cols = [
            "ClientID", "BirthDate", "Gender", "EducationType", "FamilyStatus",
            "IncomeTotal", "IncomeType", "OwnCar", "OwnRealty",
            "CNTChildren", "CNTFamilyMembers", "RegionRating", "Days_Employed",
        ]
        n = _bulk_insert(cur, "public.Clients", client_cols, clients)
        log.info("Inserted %d rows → public.Clients", n)

        # ── Loan Applications ─────────────────────────────────────────────────
        app_cols = [
            "ApplicationID", "ClientID", "ApplicationDate",
            "LoanAmount", "Annuity", "LoanTenorMonths",
            "GoodsPriceAmount", "ContractType", "TARGET",
        ]
        n = _bulk_insert(cur, "public.LoanApplications", app_cols, apps)
        log.info("Inserted %d rows → public.LoanApplications", n)

        # ── Bureau Snapshots ─────────────────────────────────────────────────
        bur_cols = [
            "SnapshotID", "ClientID", "SnapshotDate",
            "ExtScore1", "ExtScore2", "ExtScore3",
            "ActiveLoans", "ClosedLoans", "EnquiriesYear",
        ]
        n = _bulk_insert(cur, "public.BureauSnapshots", bur_cols, bureau)
        log.info("Inserted %d rows → public.BureauSnapshots", n)

        # ── Transactions ─────────────────────────────────────────────────────
        txn_cols = [
            "TxnID", "ClientID", "TxnDate", "TxnType", "Amount", "Balance", "Channel",
        ]
        n = _bulk_insert(cur, "public.Transactions", txn_cols, txns)
        log.info("Inserted %d rows → public.Transactions", n)

        conn.commit()
        log.info("✓ All synthetic data committed.")
        log.info(
            "DRIFT SUMMARY\n"
            "  Clients        : %d  (IDs %d – %d)\n"
            "  Applications   : %d  (IDs %d – %d)\n"
            "  Bureau rows    : %d\n"
            "  Transaction rows: %d\n"
            "  ExtScore1 mean : ~%.2f  (training baseline ~0.50)\n"
            "  ExtScore2 mean : ~%.2f  (training baseline ~0.50)\n"
            "  Unemployed %%   : ~%.0f%%\n"
            "  Overdraft txns : ~%.0f%% of all transactions",
            n_clients, CLIENT_ID_BASE, CLIENT_ID_BASE + n_clients - 1,
            n_clients, APP_ID_BASE,    APP_ID_BASE    + n_clients - 1,
            n_clients,
            n_clients * TXN_PER_CLIENT,
            DRIFT["ext_score1"]["mu"],
            DRIFT["ext_score2"]["mu"],
            DRIFT["unemployed_prob"] * 100,
            (TXN_TYPES.count("OVERDRAFT") / len(TXN_TYPES)) * 100,
        )
        log.info(
            "\nNEXT STEPS:\n"
            "  1. psql: CALL stg.usp_StageSourceData(NULL);\n"
            "  2. psql: CALL stg.usp_ValidateStagedData(NULL);\n"
            "  3. psql: CALL ml.usp_BuildFeatureStoreRaw(NULL);\n"
            "  4. python credit_risk_etl_pg.py --run-date %s\n"
            "  5. Query rpt.vw_PSITrend — PSI_Status should be MONITOR or RETRAIN",
            run_date,
        )

    except Exception:
        conn.rollback()
        log.exception("Injection failed — rolled back.")
        raise
    finally:
        conn.close()


def rollback() -> None:
    """
    Delete all synthetic rows in reverse FK order.
    SHAP_Values cascades from PD_Scores, so no explicit delete needed there.
    """
    id_lo = CLIENT_ID_BASE
    id_hi = CLIENT_ID_BASE + 9_999_999   # covers any N_CLIENTS value

    conn = get_conn()
    try:
        cur = conn.cursor()
        steps = [
            # ml layer (FK children of public)
            ("ml.PD_Scores",
             "DELETE FROM ml.PD_Scores "
             "WHERE ClientID BETWEEN %s AND %s", (id_lo, id_hi)),
            ("ml.FeatureStore",
             "DELETE FROM ml.FeatureStore "
             "WHERE ClientID BETWEEN %s AND %s", (id_lo, id_hi)),
            ("stg.FeatureStore_Raw",
             "DELETE FROM stg.FeatureStore_Raw "
             "WHERE ClientID BETWEEN %s AND %s", (id_lo, id_hi)),
            # Delta tables (keyed by ETL_RunID, no FK from Clients — safe to delete by ClientID)
            ("stg.Applications_Delta",
             "DELETE FROM stg.Applications_Delta "
             "WHERE ClientID BETWEEN %s AND %s", (id_lo, id_hi)),
            ("stg.Clients_Delta",
             "DELETE FROM stg.Clients_Delta "
             "WHERE ClientID BETWEEN %s AND %s", (id_lo, id_hi)),
            ("stg.Transactions_Delta",
             "DELETE FROM stg.Transactions_Delta "
             "WHERE ClientID BETWEEN %s AND %s", (id_lo, id_hi)),
            ("stg.Bureau_Delta",
             "DELETE FROM stg.Bureau_Delta "
             "WHERE ClientID BETWEEN %s AND %s", (id_lo, id_hi)),
            # Public source tables (in FK dependency order)
            ("public.BureauSnapshots",
             "DELETE FROM public.BureauSnapshots "
             "WHERE ClientID BETWEEN %s AND %s", (id_lo, id_hi)),
            ("public.Transactions",
             "DELETE FROM public.Transactions "
             "WHERE ClientID BETWEEN %s AND %s", (id_lo, id_hi)),
            ("public.LoanApplications",
             "DELETE FROM public.LoanApplications "
             "WHERE ClientID BETWEEN %s AND %s", (id_lo, id_hi)),
            ("public.Clients",
             "DELETE FROM public.Clients "
             "WHERE ClientID BETWEEN %s AND %s", (id_lo, id_hi)),
        ]
        total_deleted = 0
        for label, sql, params in steps:
            cur.execute(sql, params)
            n = cur.rowcount
            total_deleted += n
            if n:
                log.info("  Deleted %5d rows from %s", n, label)

        conn.commit()
        log.info("✓ Rollback complete — %d total rows removed.", total_deleted)

    except Exception:
        conn.rollback()
        log.exception("Rollback failed.")
        raise
    finally:
        conn.close()


def verify() -> None:
    """Print row counts for synthetic records across all affected tables."""
    id_lo = CLIENT_ID_BASE
    id_hi = CLIENT_ID_BASE + 9_999_999

    checks = [
        ("public.Clients",           "SELECT COUNT(*) FROM public.Clients           WHERE ClientID BETWEEN %s AND %s"),
        ("public.LoanApplications",  "SELECT COUNT(*) FROM public.LoanApplications  WHERE ClientID BETWEEN %s AND %s"),
        ("public.BureauSnapshots",   "SELECT COUNT(*) FROM public.BureauSnapshots   WHERE ClientID BETWEEN %s AND %s"),
        ("public.Transactions",      "SELECT COUNT(*) FROM public.Transactions      WHERE ClientID BETWEEN %s AND %s"),
        ("stg.FeatureStore_Raw",     "SELECT COUNT(*) FROM stg.FeatureStore_Raw     WHERE ClientID BETWEEN %s AND %s"),
        ("ml.FeatureStore",          "SELECT COUNT(*) FROM ml.FeatureStore          WHERE ClientID BETWEEN %s AND %s"),
        ("ml.PD_Scores",             "SELECT COUNT(*) FROM ml.PD_Scores             WHERE ClientID BETWEEN %s AND %s"),
    ]

    conn = get_conn()
    try:
        cur = conn.cursor()
        log.info("Synthetic row counts (ClientID %d – %d):", id_lo, id_lo + 9_999)
        log.info("  %-30s  %s", "Table", "Rows")
        log.info("  %s", "-" * 45)
        for label, sql in checks:
            cur.execute(sql, (id_lo, id_hi))
            count = cur.fetchone()[0]
            log.info("  %-30s  %d", label, count)

        # PSI check
        cur.execute("""
            SELECT MonitorDate, PSI_Score, PSI_Status
            FROM   rpt.vw_PSITrend
            ORDER  BY MonitorDate DESC
            LIMIT  5
        """)
        rows = cur.fetchall()
        if rows:
            log.info("\n  Latest PSI readings:")
            log.info("  %-12s  %-8s  %s", "MonitorDate", "PSI", "Status")
            log.info("  %s", "-" * 35)
            for r in rows:
                log.info("  %-12s  %-8.4f  %s", r[0], r[1], r[2])
        else:
            log.info("\n  No PSI readings yet — run the ETL first.")
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Drift data generator / rollback tool")
    parser.add_argument(
        "--mode", choices=["inject", "rollback", "verify"],
        default="inject",
        help="inject: insert synthetic data | rollback: clean up | verify: check counts",
    )
    parser.add_argument(
        "--n-clients", type=int, default=3_000,
        help="Number of synthetic clients to inject (default: 3000)",
    )
    args = parser.parse_args()

    if args.mode == "inject":
        log.info("Mode: INJECT  |  N=%d clients", args.n_clients)
        inject(args.n_clients)
    elif args.mode == "rollback":
        log.info("Mode: ROLLBACK")
        rollback()
    else:
        log.info("Mode: VERIFY")
        verify()


if __name__ == "__main__":
    main()

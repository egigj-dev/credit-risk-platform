"""
seed_pos_cash_pg.py
===================
Seeds public.POS_CashBalance from POS_CASH_balance.csv.

Usage
-----
  python seed_pos_cash_pg.py --data-dir ./data --dbname credit_risk_db_v2

Required CSV (same Kaggle download):
  POS_CASH_balance.csv
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
import psycopg2.extensions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seed_pos")


def get_connection(args):
    return psycopg2.connect(
        host=args.host, port=args.port,
        dbname=args.dbname, user=args.user, password=args.password,
    )


def _int(v, default=None):
    if v is None: return default
    try:
        if np.isnan(v): return default
    except (TypeError, ValueError): pass
    return int(v)


def _float(v, default=None):
    if v is None: return default
    try:
        if np.isnan(v): return default
    except (TypeError, ValueError): pass
    return float(v)


def seed_pos_cash(conn, data_dir: Path, sample: int):
    csv_path = data_dir / "POS_CASH_balance.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Not found: {csv_path}")

    log.info(f"Loading {csv_path} ...")
    df = pd.read_csv(csv_path)
    log.info(f"  Loaded {len(df):,} rows × {df.shape[1]} cols")

    if sample and sample < len(df):
        # Sample by client ID to keep all months for selected clients
        client_ids = df["SK_ID_CURR"].unique()
        n_clients  = int(sample / (len(df) / len(client_ids)))
        sampled_ids = np.random.choice(client_ids, size=min(n_clients, len(client_ids)), replace=False)
        df = df[df["SK_ID_CURR"].isin(sampled_ids)].copy()
        log.info(f"  Sampled to {len(df):,} rows for {len(sampled_ids):,} clients")

    # Check which ClientIDs and PrevAppIDs exist in DB (FK constraint)
    with conn.cursor() as cur:
        cur.execute("SELECT ClientID FROM public.Clients")
        valid_clients = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT PrevAppID FROM public.PreviousApplications")
        valid_prev    = {r[0] for r in cur.fetchall()}

    df = df[
        df["SK_ID_CURR"].isin(valid_clients) &
        df["SK_ID_PREV"].isin(valid_prev)
    ].copy()
    log.info(f"  After FK filter: {len(df):,} rows")

    BATCH = 5000
    total = 0

    sql = """
        INSERT INTO public.POS_CashBalance
            (ClientID, PrevAppID, MonthsBalance, CNT_Instalment,
             CNT_Instalment_Future, NAME_Contract_Status, SK_DPD, SK_DPD_DEF)
        VALUES %s
        ON CONFLICT DO NOTHING
    """

    with conn.cursor() as cur:
        batch = []
        for _, row in df.iterrows():
            batch.append((
                int(row["SK_ID_CURR"]),
                int(row["SK_ID_PREV"]),
                _int(row.get("MONTHS_BALANCE"), 0),
                _float(row.get("CNT_INSTALMENT")),
                _float(row.get("CNT_INSTALMENT_FUTURE")),
                str(row["NAME_CONTRACT_STATUS"])[:20] if pd.notna(row.get("NAME_CONTRACT_STATUS")) else None,
                _int(row.get("SK_DPD"), 0),
                _int(row.get("SK_DPD_DEF"), 0),
            ))
            if len(batch) >= BATCH:
                psycopg2.extras.execute_values(cur, sql, batch, page_size=BATCH)
                total += len(batch)
                batch = []
                log.info(f"  Inserted {total:,} rows ...")

        if batch:
            psycopg2.extras.execute_values(cur, sql, batch, page_size=BATCH)
            total += len(batch)

    conn.commit()
    log.info(f"POS_CashBalance seeded: {total:,} rows")


def seed_new_client_cols(conn, data_dir: Path):
    """
    Back-fill new Clients columns from application_train.csv.
    Matches on SK_ID_CURR = ClientID.
    """
    csv_path = data_dir / "application_train.csv"
    if not csv_path.exists():
        log.warning(f"application_train.csv not found — skipping Clients back-fill")
        return

    log.info("Back-filling new Clients columns from application_train.csv ...")
    cols = [
        "SK_ID_CURR", "NAME_HOUSING_TYPE", "REGION_POPULATION_RELATIVE",
        "DAYS_REGISTRATION", "DAYS_ID_PUBLISH",
        "FLAG_MOBIL", "FLAG_WORK_PHONE", "FLAG_CONT_MOBILE", "FLAG_PHONE", "FLAG_EMAIL",
        "OCCUPATION_TYPE", "ORGANIZATION_TYPE",
        "REG_REGION_NOT_LIVE_REGION", "REG_REGION_NOT_WORK_REGION",
        "LIVE_REGION_NOT_WORK_REGION", "REG_CITY_NOT_LIVE_CITY",
        "REG_CITY_NOT_WORK_CITY", "LIVE_CITY_NOT_WORK_CITY",
        "YEARS_BEGINEXPLUATATION_AVG", "FLOORSMAX_AVG", "TOTALAREA_MODE",
        "EMERGENCYSTATE_MODE",
        "OBS_30_CNT_SOCIAL_CIRCLE", "DEF_30_CNT_SOCIAL_CIRCLE", "DEF_60_CNT_SOCIAL_CIRCLE",
    ]
    df = pd.read_csv(csv_path, usecols=[c for c in cols if c in pd.read_csv(csv_path, nrows=0).columns])
    df = df.drop_duplicates("SK_ID_CURR")
    log.info(f"  {len(df):,} client rows to update")

    BATCH = 2000
    total = 0
    with conn.cursor() as cur:
        batch_ids = []
        batch_vals = []
        for _, row in df.iterrows():
            batch_ids.append(int(row["SK_ID_CURR"]))
            batch_vals.append(row)
            if len(batch_ids) >= BATCH:
                _do_client_update(cur, batch_vals, batch_ids)
                total += len(batch_ids)
                batch_ids, batch_vals = [], []
        if batch_ids:
            _do_client_update(cur, batch_vals, batch_ids)
            total += len(batch_ids)

    conn.commit()
    log.info(f"Clients back-filled: {total:,} rows updated")


def _do_client_update(cur, rows, ids):
    for row in rows:
        cur.execute("""
            UPDATE public.Clients SET
                NAME_HOUSING_TYPE            = %s,
                REGION_POPULATION_RELATIVE   = %s,
                DAYS_REGISTRATION            = %s,
                DAYS_ID_PUBLISH              = %s,
                FLAG_MOBIL                   = %s,
                FLAG_WORK_PHONE              = %s,
                FLAG_CONT_MOBILE             = %s,
                FLAG_PHONE                   = %s,
                FLAG_EMAIL                   = %s,
                OCCUPATION_TYPE              = %s,
                ORGANIZATION_TYPE            = %s,
                REG_REGION_NOT_LIVE_REGION   = %s,
                REG_REGION_NOT_WORK_REGION   = %s,
                LIVE_REGION_NOT_WORK_REGION  = %s,
                REG_CITY_NOT_LIVE_CITY       = %s,
                REG_CITY_NOT_WORK_CITY       = %s,
                LIVE_CITY_NOT_WORK_CITY      = %s,
                YEARS_BEGINEXPLUATATION_AVG  = %s,
                FLOORSMAX_AVG                = %s,
                TOTALAREA_MODE               = %s,
                EMERGENCYSTATE_MODE          = %s,
                OBS_30_CNT_SOCIAL_CIRCLE     = %s,
                DEF_30_CNT_SOCIAL_CIRCLE     = %s,
                DEF_60_CNT_SOCIAL_CIRCLE     = %s
            WHERE ClientID = %s
        """, (
            str(row.get("NAME_HOUSING_TYPE",""))[:50] or None,
            _float(row.get("REGION_POPULATION_RELATIVE")),
            _int(row.get("DAYS_REGISTRATION")),
            _int(row.get("DAYS_ID_PUBLISH")),
            _int(row.get("FLAG_MOBIL"), 0),
            _int(row.get("FLAG_WORK_PHONE"), 0),
            _int(row.get("FLAG_CONT_MOBILE"), 0),
            _int(row.get("FLAG_PHONE"), 0),
            _int(row.get("FLAG_EMAIL"), 0),
            str(row.get("OCCUPATION_TYPE",""))[:50] or None,
            str(row.get("ORGANIZATION_TYPE",""))[:60] or None,
            _int(row.get("REG_REGION_NOT_LIVE_REGION"), 0),
            _int(row.get("REG_REGION_NOT_WORK_REGION"), 0),
            _int(row.get("LIVE_REGION_NOT_WORK_REGION"), 0),
            _int(row.get("REG_CITY_NOT_LIVE_CITY"), 0),
            _int(row.get("REG_CITY_NOT_WORK_CITY"), 0),
            _int(row.get("LIVE_CITY_NOT_WORK_CITY"), 0),
            _float(row.get("YEARS_BEGINEXPLUATATION_AVG")),
            _float(row.get("FLOORSMAX_AVG")),
            _float(row.get("TOTALAREA_MODE")),
            str(row.get("EMERGENCYSTATE_MODE",""))[:3] or None,
            _int(row.get("OBS_30_CNT_SOCIAL_CIRCLE")),
            _int(row.get("DEF_30_CNT_SOCIAL_CIRCLE")),
            _int(row.get("DEF_60_CNT_SOCIAL_CIRCLE")),
            int(row["SK_ID_CURR"]),
        ))


def seed_new_loanapp_cols(conn, data_dir: Path):
    """Back-fill new LoanApplications columns."""
    csv_path = data_dir / "application_train.csv"
    if not csv_path.exists():
        return

    log.info("Back-filling new LoanApplications columns ...")
    doc_flags = [f"FLAG_DOCUMENT_{i}" for i in range(2, 22)]
    bureau_enq = [f"AMT_REQ_CREDIT_BUREAU_{w}" for w in
                  ["HOUR","DAY","WEEK","MON","QRT","YEAR"]]
    base_cols  = ["SK_ID_CURR","NAME_TYPE_SUITE","WEEKDAY_APPR_PROCESS_START",
                  "HOUR_APPR_PROCESS_START","DAYS_LAST_PHONE_CHANGE"]
    all_cols   = base_cols + doc_flags + bureau_enq

    header = pd.read_csv(csv_path, nrows=0).columns.tolist()
    read_cols = [c for c in all_cols if c in header]
    df = pd.read_csv(csv_path, usecols=read_cols)
    df = df.drop_duplicates("SK_ID_CURR")
    log.info(f"  {len(df):,} application rows to update")

    doc_set_str  = ", ".join(f'"{c}" = %s' for c in doc_flags if c in read_cols)
    enq_set_str  = ", ".join(f'"{c}" = %s' for c in bureau_enq if c in read_cols)
    base_set_str = 'NAME_TYPE_SUITE=%s, WEEKDAY_APPR_PROCESS_START=%s, HOUR_APPR_PROCESS_START=%s, DAYS_LAST_PHONE_CHANGE=%s'
    set_str = ", ".join(filter(None, [base_set_str, doc_set_str, enq_set_str]))

    BATCH = 2000
    total = 0
    with conn.cursor() as cur:
        for _, row in df.iterrows():
            vals = (
                str(row.get("NAME_TYPE_SUITE",""))[:40] or None,
                str(row.get("WEEKDAY_APPR_PROCESS_START",""))[:12] or None,
                _int(row.get("HOUR_APPR_PROCESS_START")),
                _int(row.get("DAYS_LAST_PHONE_CHANGE")),
            )
            vals += tuple(_int(row.get(c), 0) for c in doc_flags if c in read_cols)
            vals += tuple(_int(row.get(c)) for c in bureau_enq if c in read_cols)
            vals += (int(row["SK_ID_CURR"]),)

            # Build lowercase SET clause
            doc_set   = ", ".join(f"{c.lower()} = %s" for c in doc_flags if c in read_cols)
            enq_set   = ", ".join(f"{c.lower()} = %s" for c in bureau_enq if c in read_cols)
            base_set  = "name_type_suite=%s, weekday_appr_process_start=%s, hour_appr_process_start=%s, days_last_phone_change=%s"
            set_str   = ", ".join(filter(None, [base_set, doc_set, enq_set]))

            cur.execute(
                f"UPDATE public.LoanApplications SET {set_str} WHERE ApplicationID = %s",
                vals
            )

    conn.commit()
    log.info(f"LoanApplications back-filled: {total:,} rows")


def _float(v, default=None):
    if v is None: return default
    try:
        if np.isnan(v): return default
    except (TypeError, ValueError): pass
    return float(v)


def _int(v, default=None):
    if v is None: return default
    try:
        if np.isnan(float(v)): return default
    except (TypeError, ValueError): pass
    return int(v)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed POS_CashBalance + back-fill new columns")
    parser.add_argument("--data-dir",  default="./data")
    parser.add_argument("--sample",    type=int, default=0, help="0=all rows")
    parser.add_argument("--host",      default="localhost")
    parser.add_argument("--port",      type=int, default=5432)
    parser.add_argument("--dbname",    default="credit_risk_db_v2")
    parser.add_argument("--user",      default="postgres")
    parser.add_argument("--password",  default=os.getenv("PG_PASS", ""))
    parser.add_argument("--skip-pos",  action="store_true")
    parser.add_argument("--skip-backfill", action="store_true")
    args = parser.parse_args()

    conn = get_connection(args)
    try:
        if not args.skip_backfill:
            seed_new_client_cols(conn, Path(args.data_dir))
            seed_new_loanapp_cols(conn, Path(args.data_dir))
            if not args.skip_pos:
                seed_pos_cash(conn, Path(args.data_dir), args.sample)
    except Exception as e:
        log.critical(f"FAILED: {e}")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()
    log.info("Done.")

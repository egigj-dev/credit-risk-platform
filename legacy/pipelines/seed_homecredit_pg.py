"""
seed_homecredit_v2_pg.py
========================
Extends seed_homecredit_pg.py to populate the three new v2 source tables:
  public.BureauLoans          ← bureau.csv (full tradeline history)
  public.PreviousApplications ← previous_application.csv
  public.InstallmentsPayments ← installments_payments.csv

Usage
-----
  python seed_homecredit_v2_pg.py --data-dir ./data --sample 50000 --dbname credit_risk_db_v2

Required CSVs (same Kaggle download):
  application_train.csv
  bureau.csv
  credit_card_balance.csv
  previous_application.csv      ← new
  installments_payments.csv     ← new
"""

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
import psycopg2.extensions
from dateutil.relativedelta import relativedelta

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seed_v2")

APPLICATION_DATE = date(2026, 4, 1)
TXN_MONTHS_WINDOW = (-3, 0)

# ── numpy adapters ────────────────────────────────────────────────────────────
def _np_int(v):   return psycopg2.extensions.AsIs(int(v))
def _np_float(v): return psycopg2.extensions.AsIs(float(v))
def _np_bool(v):  return psycopg2.extensions.AsIs(bool(v))
for _t in (np.int8, np.int16, np.int32, np.int64,
           np.uint8, np.uint16, np.uint32, np.uint64):
    psycopg2.extensions.register_adapter(_t, _np_int)
for _t in (np.float32, np.float64):
    psycopg2.extensions.register_adapter(_t, _np_float)
psycopg2.extensions.register_adapter(np.bool_, _np_bool)


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

def _smallint(v, default=0):
    r = _int(v, default)
    return default if r is None else max(-32768, min(32767, r))


# ═════════════════════════════════════════════════════════════════════════════
# CONNECTION
# ═════════════════════════════════════════════════════════════════════════════

def get_connection(args):
    return psycopg2.connect(
        host=args.host, port=args.port, dbname=args.dbname,
        user=args.user, password=args.password,
        options="-c application_name=seed_homecredit_v2",
    )


# ═════════════════════════════════════════════════════════════════════════════
# LOAD CSVs
# ═════════════════════════════════════════════════════════════════════════════

def load_csvs(data_dir: Path, sample: int):
    log.info("Loading application_train.csv ...")
    app = pd.read_csv(data_dir / "application_train.csv")
    log.info(f"  {len(app):,} rows")
    if sample and sample < len(app):
        app = app.sample(n=sample, random_state=42).reset_index(drop=True)
        log.info(f"  Sampled to {len(app):,} rows")
    client_ids = set(app["SK_ID_CURR"].tolist())

    log.info("Loading bureau.csv ...")
    bureau = pd.read_csv(data_dir / "bureau.csv")
    bureau = bureau[bureau["SK_ID_CURR"].isin(client_ids)]
    log.info(f"  {len(bureau):,} rows")

    log.info("Loading credit_card_balance.csv ...")
    cc = pd.read_csv(data_dir / "credit_card_balance.csv")
    cc = cc[cc["SK_ID_CURR"].isin(client_ids)]
    cc = cc[cc["MONTHS_BALANCE"].between(TXN_MONTHS_WINDOW[0], TXN_MONTHS_WINDOW[1])]
    log.info(f"  {len(cc):,} rows (last 3 months)")

    log.info("Loading previous_application.csv ...")
    prev = pd.read_csv(data_dir / "previous_application.csv")
    prev = prev[prev["SK_ID_CURR"].isin(client_ids)]
    log.info(f"  {len(prev):,} rows")

    log.info("Loading installments_payments.csv ...")
    inst = pd.read_csv(data_dir / "installments_payments.csv")
    inst = inst[inst["SK_ID_CURR"].isin(client_ids)]
    log.info(f"  {len(inst):,} rows")

    return app, bureau, cc, prev, inst


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — public.Clients
# ═════════════════════════════════════════════════════════════════════════════

def build_clients(app):
    df = pd.DataFrame()
    df["ClientID"]         = app["SK_ID_CURR"]
    df["BirthDate"]        = app["DAYS_BIRTH"].apply(
        lambda d: APPLICATION_DATE + timedelta(days=int(d)))
    df["Gender"]           = app["CODE_GENDER"].map({"M":"M","F":"F","XNA":"X"}).fillna("X")
    df["EducationType"]    = app["NAME_EDUCATION_TYPE"].fillna("Unknown")
    df["FamilyStatus"]     = app["NAME_FAMILY_STATUS"].fillna("Unknown")
    df["IncomeTotal"]      = app["AMT_INCOME_TOTAL"].fillna(0).clip(lower=0)
    df["IncomeType"]       = app["NAME_INCOME_TYPE"].fillna("Unknown")
    df["OwnCar"]           = app["FLAG_OWN_CAR"].map({"Y":True,"N":False}).fillna(False)
    df["OwnRealty"]        = app["FLAG_OWN_REALTY"].map({"Y":True,"N":False}).fillna(False)
    df["CNTChildren"]      = app["CNT_CHILDREN"].fillna(0).clip(0, 32767).astype(int)
    df["CNTFamilyMembers"] = app["CNT_FAM_MEMBERS"].fillna(1).clip(1, 32767).astype(int)
    df["RegionRating"]     = app["REGION_RATING_CLIENT"].fillna(2).clip(1, 3).astype(int)
    df["Days_Employed"]    = app["DAYS_EMPLOYED"].apply(
        lambda v: None if pd.isna(v) or int(v) >= 0 else int(v))
    return df

def insert_clients(conn, df):
    sql = """
        INSERT INTO public.Clients
            (ClientID, BirthDate, Gender, EducationType, FamilyStatus,
             IncomeTotal, IncomeType, OwnCar, OwnRealty, CNTChildren,
             CNTFamilyMembers, RegionRating, Days_Employed)
        VALUES %s ON CONFLICT (ClientID) DO NOTHING
    """
    records = [(
        int(r.ClientID), r.BirthDate, str(r.Gender), str(r.EducationType),
        str(r.FamilyStatus), float(r.IncomeTotal), str(r.IncomeType),
        bool(r.OwnCar), bool(r.OwnRealty), _smallint(r.CNTChildren),
        _smallint(r.CNTFamilyMembers, 1), _smallint(r.RegionRating, 2),
        _int(r.Days_Employed),
    ) for r in df.itertuples(index=False)]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, records, page_size=2000)
    conn.commit()
    log.info(f"  public.Clients: {len(records):,} rows")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — public.LoanApplications
# ═════════════════════════════════════════════════════════════════════════════

def build_applications(app):
    df = pd.DataFrame()
    df["ApplicationID"]   = app["SK_ID_CURR"]
    df["ClientID"]        = app["SK_ID_CURR"]
    df["ApplicationDate"] = APPLICATION_DATE
    df["LoanAmount"]      = app["AMT_CREDIT"].fillna(0).clip(lower=0)
    df["Annuity"]         = app["AMT_ANNUITY"].fillna(0).clip(lower=0)
    annuity_safe          = app["AMT_ANNUITY"].replace(0, np.nan).fillna(1)
    df["LoanTenorMonths"] = (app["AMT_CREDIT"] / annuity_safe).clip(1, 360).fillna(12).astype(int)
    df["GoodsPriceAmount"]= app["AMT_GOODS_PRICE"]
    #df["PurposeCode"]     = app["NAME_CASH_LOAN_PURPOSE"].fillna("Other")
    df["ContractType"]    = app["NAME_CONTRACT_TYPE"].fillna("Cash loans")
    df["ContractType"]    = df["ContractType"].where(
        df["ContractType"].isin(["Cash loans","Revolving loans"]), "Cash loans")
    df["TARGET"]          = app["TARGET"].map({0:False,1:True})
    return df

def insert_applications(conn, df):
    sql = """
        INSERT INTO public.LoanApplications
            (ApplicationID, ClientID, ApplicationDate, LoanAmount, Annuity,
             LoanTenorMonths, GoodsPriceAmount, ContractType, TARGET)
        VALUES %s ON CONFLICT (ApplicationID) DO NOTHING
    """
    records = []
    for r in df.itertuples(index=False):
        records.append((
            int(r.ApplicationID), int(r.ClientID), r.ApplicationDate,
            float(r.LoanAmount), float(r.Annuity), int(r.LoanTenorMonths),
            None if pd.isna(r.GoodsPriceAmount) else float(r.GoodsPriceAmount),
            str(r.ContractType), None if pd.isna(r.TARGET) else bool(r.TARGET),
        ))
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, records, page_size=2000)
    conn.commit()
    log.info(f"  public.LoanApplications: {len(records):,} rows")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — public.BureauSnapshots (ext scores)
# ═════════════════════════════════════════════════════════════════════════════

def build_bureau_snapshots(app, bureau):
    bureau_agg = (bureau.groupby("SK_ID_CURR")["CREDIT_ACTIVE"]
        .value_counts().unstack(fill_value=0).reset_index())
    bureau_agg.columns.name = None
    bureau_agg["ActiveLoans"] = bureau_agg.get("Active", 0)
    bureau_agg["ClosedLoans"] = bureau_agg.get("Closed", 0)
    bureau_agg = bureau_agg[["SK_ID_CURR","ActiveLoans","ClosedLoans"]]

    df = app[["SK_ID_CURR","EXT_SOURCE_1","EXT_SOURCE_2","EXT_SOURCE_3",
              "AMT_REQ_CREDIT_BUREAU_YEAR"]].copy()
    df = df.merge(bureau_agg, on="SK_ID_CURR", how="left")
    df["SnapshotID"]    = df["SK_ID_CURR"]
    df["ClientID"]      = df["SK_ID_CURR"]
    df["SnapshotDate"]  = APPLICATION_DATE - timedelta(days=1)
    df["ExtScore1"]     = df["EXT_SOURCE_1"].clip(0, 1)
    df["ExtScore2"]     = df["EXT_SOURCE_2"].clip(0, 1)
    df["ExtScore3"]     = df["EXT_SOURCE_3"].clip(0, 1)
    df["ActiveLoans"]   = df["ActiveLoans"].fillna(0).clip(0,32767).astype(int)
    df["ClosedLoans"]   = df["ClosedLoans"].fillna(0).clip(0,32767).astype(int)
    df["EnquiriesYear"] = df["AMT_REQ_CREDIT_BUREAU_YEAR"].fillna(0).clip(0,32767).astype(int)
    return df[["SnapshotID","ClientID","SnapshotDate",
               "ExtScore1","ExtScore2","ExtScore3",
               "ActiveLoans","ClosedLoans","EnquiriesYear"]]

def insert_bureau_snapshots(conn, df):
    sql = """
        INSERT INTO public.BureauSnapshots
            (SnapshotID, ClientID, SnapshotDate,
             ExtScore1, ExtScore2, ExtScore3,
             ActiveLoans, ClosedLoans, EnquiriesYear)
        VALUES %s ON CONFLICT (ClientID, SnapshotDate) DO NOTHING
    """
    records = []
    for r in df.itertuples(index=False):
        records.append((
            int(r.SnapshotID), int(r.ClientID), r.SnapshotDate,
            None if pd.isna(r.ExtScore1) else float(r.ExtScore1),
            None if pd.isna(r.ExtScore2) else float(r.ExtScore2),
            None if pd.isna(r.ExtScore3) else float(r.ExtScore3),
            int(r.ActiveLoans), int(r.ClosedLoans), int(r.EnquiriesYear),
        ))
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, records, page_size=2000)
    conn.commit()
    log.info(f"  public.BureauSnapshots: {len(records):,} rows")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 — public.Transactions
# ═════════════════════════════════════════════════════════════════════════════

def build_transactions(cc, app):
    rng = np.random.default_rng(seed=42)
    employed_set = set(app.loc[app["DAYS_EMPLOYED"] < 0, "SK_ID_CURR"].tolist())
    rows = []
    txn_id = 1

    for r in cc.itertuples(index=False):
        month_offset = int(r.MONTHS_BALANCE)
        month_base   = APPLICATION_DATE + relativedelta(months=month_offset)
        days_in_month = ((month_base.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
        balance  = float(r.AMT_BALANCE) if not pd.isna(r.AMT_BALANCE) else 0.0
        client   = int(r.SK_ID_CURR)

        def rand_day(lo=1, hi=None):
            hi = min(hi or days_in_month, days_in_month)
            return month_base.replace(day=int(rng.integers(lo, hi + 1)))

        if client in employed_set:
            rows.append((txn_id, client, month_base.replace(day=min(25,days_in_month)),
                         "SALARY", 1.0, balance, "ONLINE"))
            txn_id += 1

        atm = float(r.AMT_DRAWINGS_ATM_CURRENT) if not pd.isna(r.AMT_DRAWINGS_ATM_CURRENT) else 0.0
        if atm > 0:
            for amt in rng.dirichlet(np.ones(int(rng.integers(1,4)))) * atm:
                rows.append((txn_id, client, rand_day(), "ATM", float(amt), balance, "ATM"))
                txn_id += 1

        pos = float(r.AMT_DRAWINGS_POS_CURRENT) if not pd.isna(r.AMT_DRAWINGS_POS_CURRENT) else 0.0
        if pos > 0:
            for amt in rng.dirichlet(np.ones(int(rng.integers(1,6)))) * pos:
                rows.append((txn_id, client, rand_day(), "DEBIT", float(amt), balance, "POS"))
                txn_id += 1

        pay = float(r.AMT_PAYMENT_CURRENT) if not pd.isna(r.AMT_PAYMENT_CURRENT) else 0.0
        if pay > 0:
            rows.append((txn_id, client, month_base.replace(day=min(10,days_in_month)),
                         "CREDIT", float(pay), balance, "ONLINE"))
            txn_id += 1

        if balance < 0:
            od_date = rand_day()
            rows.append((txn_id, client, od_date, "OVERDRAFT", abs(balance)*0.01, balance, None))
            txn_id += 1

    df = pd.DataFrame(rows, columns=["TxnID","ClientID","TxnDate","TxnType","Amount","Balance","Channel"])
    return df[df["TxnDate"] <= APPLICATION_DATE]

def insert_transactions(conn, df):
    sql = """
        INSERT INTO public.Transactions
            (TxnID, ClientID, TxnDate, TxnType, Amount, Balance, Channel)
        VALUES %s ON CONFLICT (TxnDate, TxnID) DO NOTHING
    """
    records = [(int(r.TxnID), int(r.ClientID), r.TxnDate,
                r.TxnType, float(r.Amount), float(r.Balance), r.Channel)
               for r in df.itertuples(index=False)]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, records, page_size=5000)
    conn.commit()
    log.info(f"  public.Transactions: {len(records):,} rows")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — public.BureauLoans  (NEW)
# ═════════════════════════════════════════════════════════════════════════════

def build_bureau_loans(bureau):
    """Map bureau.csv → public.BureauLoans (full tradeline history)."""
    df = pd.DataFrame()
    df["BureauLoanID"]         = bureau["SK_ID_BUREAU"]
    df["ClientID"]             = bureau["SK_ID_CURR"]
    df["CreditActive"]         = bureau["CREDIT_ACTIVE"].fillna("Unknown")
    df["DaysCredit"]           = bureau["DAYS_CREDIT"].fillna(0).astype(int)
    df["DaysCreditEndDate"]    = bureau["DAYS_CREDIT_ENDDATE"].apply(_int)
    df["AMT_CreditSum"]        = bureau["AMT_CREDIT_SUM"].apply(_float)
    df["AMT_CreditSumDebt"]    = bureau["AMT_CREDIT_SUM_DEBT"].apply(_float)
    df["AMT_CreditSumOverdue"] = bureau["AMT_CREDIT_SUM_OVERDUE"].apply(_float)
    df["AMT_CreditMaxOverdue"] = bureau["AMT_CREDIT_MAX_OVERDUE"].apply(_float)
    df["CNT_CreditProlong"]    = bureau["CNT_CREDIT_PROLONG"].apply(
        lambda v: _smallint(v, 0))
    df["AMT_Annuity"]          = bureau["AMT_ANNUITY"].apply(_float)
    df["DaysCreditUpdate"]     = bureau["DAYS_CREDIT_UPDATE"].apply(_int)
    return df

def insert_bureau_loans(conn, df):
    sql = """
        INSERT INTO public.BureauLoans
            (BureauLoanID, ClientID, CreditActive, DaysCredit, DaysCreditEndDate,
             AMT_CreditSum, AMT_CreditSumDebt, AMT_CreditSumOverdue, AMT_CreditMaxOverdue,
             CNT_CreditProlong, AMT_Annuity, DaysCreditUpdate)
        VALUES %s ON CONFLICT (BureauLoanID) DO NOTHING
    """
    records = [(
        int(r.BureauLoanID), int(r.ClientID), str(r.CreditActive),
        int(r.DaysCredit), _int(r.DaysCreditEndDate),
        _float(r.AMT_CreditSum), _float(r.AMT_CreditSumDebt),
        _float(r.AMT_CreditSumOverdue), _float(r.AMT_CreditMaxOverdue),
        _smallint(r.CNT_CreditProlong, 0), _float(r.AMT_Annuity),
        _int(r.DaysCreditUpdate),
    ) for r in df.itertuples(index=False)]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, records, page_size=3000)
    conn.commit()
    log.info(f"  public.BureauLoans: {len(records):,} rows")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 — public.PreviousApplications  (NEW)
# ═════════════════════════════════════════════════════════════════════════════

def build_previous_applications(prev):
    """Map previous_application.csv → public.PreviousApplications."""
    df = pd.DataFrame()
    df["PrevAppID"]       = prev["SK_ID_PREV"]
    df["ClientID"]        = prev["SK_ID_CURR"]
    df["ContractType"]    = prev["NAME_CONTRACT_TYPE"].fillna("Unknown")
    df["ContractStatus"]  = prev["NAME_CONTRACT_STATUS"].fillna("Unknown")
    df["AMT_Annuity"]     = prev["AMT_ANNUITY"].apply(_float)
    df["AMT_Application"] = prev["AMT_APPLICATION"].apply(_float)
    df["AMT_Credit"]      = prev["AMT_CREDIT"].apply(_float)
    df["AMT_DownPayment"] = prev["AMT_DOWN_PAYMENT"].apply(_float)
    df["AMT_GoodsPrice"]  = prev["AMT_GOODS_PRICE"].apply(_float)
    df["DaysDecision"]    = prev["DAYS_DECISION"].apply(_int)
    return df

def insert_previous_applications(conn, df):
    sql = """
        INSERT INTO public.PreviousApplications
            (PrevAppID, ClientID, ContractType, ContractStatus,
             AMT_Annuity, AMT_Application, AMT_Credit,
             AMT_DownPayment, AMT_GoodsPrice, DaysDecision)
        VALUES %s ON CONFLICT (PrevAppID) DO NOTHING
    """
    records = [(
        int(r.PrevAppID), int(r.ClientID), str(r.ContractType), str(r.ContractStatus),
        r.AMT_Annuity, r.AMT_Application, r.AMT_Credit,
        r.AMT_DownPayment, r.AMT_GoodsPrice, r.DaysDecision,
    ) for r in df.itertuples(index=False)]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, records, page_size=3000)
    conn.commit()
    log.info(f"  public.PreviousApplications: {len(records):,} rows")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 — public.InstallmentsPayments  (NEW)
# ═════════════════════════════════════════════════════════════════════════════

def build_installments(inst):
    """Map installments_payments.csv → public.InstallmentsPayments."""
    df = pd.DataFrame()
    df["PrevAppID"]        = inst["SK_ID_PREV"]
    df["ClientID"]         = inst["SK_ID_CURR"]
    df["DaysInstalment"]   = inst["DAYS_INSTALMENT"].apply(_int)
    df["DaysEntryPayment"] = inst["DAYS_ENTRY_PAYMENT"].apply(_int)
    df["AMT_Instalment"]   = inst["AMT_INSTALMENT"].apply(_float)
    df["AMT_Payment"]      = inst["AMT_PAYMENT"].apply(_float)
    return df

def insert_installments(conn, df):
    sql = """
        INSERT INTO public.InstallmentsPayments
            (PrevAppID, ClientID, DaysInstalment, DaysEntryPayment,
             AMT_Instalment, AMT_Payment)
        VALUES %s
    """
    records = [(
        int(r.PrevAppID), int(r.ClientID), _int(r.DaysInstalment), _int(r.DaysEntryPayment),
        _float(r.AMT_Instalment), _float(r.AMT_Payment),
    ) for r in df.itertuples(index=False)]
    # Batch in chunks — installments can be large
    chunk = 10000
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(records), chunk):
            psycopg2.extras.execute_values(cur, sql, records[i:i+chunk], page_size=chunk)
            total += len(records[i:i+chunk])
    conn.commit()
    log.info(f"  public.InstallmentsPayments: {total:,} rows")


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Seed credit_risk_db_v2 from Home Credit CSVs")
    parser.add_argument("--data-dir",  required=True)
    parser.add_argument("--sample",    type=int, default=None)
    parser.add_argument("--host",      default="localhost")
    parser.add_argument("--port",      type=int, default=5432)
    parser.add_argument("--dbname",    default="credit_risk_db_v2")
    parser.add_argument("--user",      default="postgres")
    parser.add_argument("--password",  default=os.getenv("PG_PASS", ""))
    parser.add_argument("--truncate-txn", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    required = ["application_train.csv", "bureau.csv", "credit_card_balance.csv",
                "previous_application.csv", "installments_payments.csv"]
    for f in required:
        if not (data_dir / f).exists():
            log.error(f"Missing: {data_dir / f}")
            sys.exit(1)

    app, bureau, cc, prev, inst = load_csvs(data_dir, args.sample)

    log.info("Connecting ...")
    conn = get_connection(args)
    log.info("  Connected")

    if args.truncate_txn:
        log.warning("Truncating public.Transactions ...")
        with conn.cursor() as cur: cur.execute("TRUNCATE public.Transactions")
        conn.commit()

    log.info("--- Clients ---")
    insert_clients(conn, build_clients(app))

    log.info("--- LoanApplications ---")
    insert_applications(conn, build_applications(app))

    log.info("--- BureauSnapshots ---")
    insert_bureau_snapshots(conn, build_bureau_snapshots(app, bureau))

    log.info("--- Transactions ---")
    txn_df = build_transactions(cc, app)
    log.info(f"  {len(txn_df):,} transaction rows generated")
    insert_transactions(conn, txn_df)

    log.info("--- BureauLoans (new) ---")
    insert_bureau_loans(conn, build_bureau_loans(bureau))

    log.info("--- PreviousApplications (new) ---")
    insert_previous_applications(conn, build_previous_applications(prev))

    log.info("--- InstallmentsPayments (new) ---")
    inst_df = build_installments(inst)
    log.info(f"  {len(inst_df):,} installment rows to insert")
    insert_installments(conn, inst_df)

    conn.close()
    log.info("=" * 55)
    log.info("SEED V2 COMPLETE")
    log.info(f"  Clients              : {len(app):,}")
    log.info(f"  Applications         : {len(app):,}")
    log.info(f"  BureauLoans          : {len(bureau):,}")
    log.info(f"  PreviousApplications : {len(prev):,}")
    log.info(f"  InstallmentsPayments : {len(inst_df):,}")
    log.info(f"  Transactions         : {len(txn_df):,}")
    log.info("=" * 55)
    log.info("Next: run SQL procs then credit_risk_etl_v2_pg.py")


if __name__ == "__main__":
    main()

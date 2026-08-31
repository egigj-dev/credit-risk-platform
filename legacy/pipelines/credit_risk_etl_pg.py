"""
credit_risk_etl_pg.py
=====================

Pipeline steps
--------------
  A. Build feature store   — reads stg.FeatureStore_Raw, adds derived
                             features, writes to ml.FeatureStore.
  B. Load champion model   — loads champion model from model_artifacts/.
  C. Score applications    — CatBoost predict_proba, risk tiers, deciles.
  D. PSI monitoring        — drift detection, writes to ml.PSI_Monitor.
  E. SHAP explanations     — top-5 features per application (vectorised).
  F. Write outputs         — bulk-upsert to ml.PD_Scores + ml.SHAP_Values.

Usage
-----
  python credit_risk_etl_pg.py --run-date 2026-04-01

Author : Data Science Division
Version: 3.1  |  April 2026

Changes from v3.0
-----------------
- [FIX-1] build_feature_store(): apply _COL_CASE to cursor column names so
          psycopg2's lowercase output is restored to mixed-case before the
          rename_map runs.  Without this, rename silently did nothing and
          df["AMT_INCOME_TOTAL"] raised a KeyError.
- [FIX-2] write_feature_store(): column quoting corrected.  The 138 new
          feature columns were added to ml.FeatureStore without quoted
          identifiers, so PostgreSQL stored them lowercase.  The INSERT
          now quotes only the original mixed-case key columns (ClientID,
          AsOfDate, ModelVersion, ETL_RunID) and lowercases everything
          else.  Previously every column was double-quoted with its
          uppercase name, causing a "column does not exist" error.
- [FIX-3] write_feature_store(): ModelVersion is now taken from a new
          mv parameter instead of the stale module-level MODEL_VERSION
          constant ("v1-local").  Call site in run_pipeline updated to
          pass the module-level model_version string (loaded dynamically
          from model_card.json at import time).
- [FIX-4] build_feature_store(): bureau_active_mean now computed from
          bureau_active_count (the bureau aggregation column) instead of
          bureau_active_count_snap, which had already been dropped from
          the DataFrame by the time the computation ran, causing all
          307,511 rows to receive bureau_active_mean = 0.
- [CLEAN-1] Removed stale CAT_MAPS dict (superseded by ETL_CAT_MAPS).
- [CLEAN-2] Removed stale MODEL_VERSION = "v1-local" constant (superseded
            by dynamic load from model_card.json).
- [CLEAN-3] Log file path changed from /logs/etl.log to etl.log (local
            working directory) to avoid PermissionError on Windows.

Changes from v2.0 (carried forward from v3.0)
----------------------------------------------
- [DEP-1]  pyodbc removed; psycopg2-binary added (matches requirements.txt).
- [FEAT-1] EmployedYears derivation now unconditionally reads Days_Employed
           from stg.FeatureStore_Raw.
- [PSI-1]  PSI baseline uses PSI_BASELINE_LOOKBACK_DAYS rolling window.
- [SHAP-1] build_shap_rows() uses application-first iteration.
- [SHAP-2] write_shap_values() uses ON CONFLICT DO NOTHING.
- [SHAP-3] compute_shap() model unwrap uses try/except chain.
- [SCORE-1] write_scores() sources ScoreDate from df["AsOfDate"] explicitly.
- [LOG-1]  Stale 'running' rows marked 'failed' at startup.
"""

# ── Standard library ──────────────────────────────────────────────────────────
import argparse
import json
import logging
import os
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Third-party ───────────────────────────────────────────────────────────────
import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
import shap
import mlflow
import mlflow.pyfunc

# ── Configuration ─────────────────────────────────────────────────────────────

PG_CONN_PARAMS = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   "credit_risk_db_v2",
    "user":     "postgres",
    "password": os.getenv("PG_PASS", ""),
    "options":  "-c application_name=credit_risk_etl",
}

MLFLOW_TRACKING_URI = "sqlite:///models/CreditRiskPD/Production/mlflow.db"
MLFLOW_MODEL_NAME   = "CreditRiskPD"
MLFLOW_MODEL_URI    = f"models:/{MLFLOW_MODEL_NAME}/Production"

PSI_MONITOR_THRESHOLD      = 0.10
PSI_RETRAIN_THRESHOLD      = 0.25
PSI_BASELINE_LOOKBACK_DAYS = 30

TIER_LOW_PCT      = 10
TIER_MEDIUM_PCT   = 40
TIER_HIGH_PCT     = 75

SHAP_TOP_N  = 5
BATCH_SIZE  = 10_000

# ── Feature columns & model version — loaded dynamically from artifacts ───────
with open(Path("model_artifacts") / "feature_cols.json") as _f:
    FEATURE_COLS: list[str] = json.load(_f)

with open(Path("model_artifacts") / "model_card.json") as _f:
    _card = json.load(_f)
model_version: str = f"v2-{_card['champion_model'].lower()}"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("etl.log", encoding="utf-8"),   # [CLEAN-3]
    ],
)
log = logging.getLogger(__name__)

log.info(f"FEATURE_COLS loaded: {len(FEATURE_COLS)} features from model_artifacts/feature_cols.json")
log.info(f"Model version      : {model_version}")


# ═════════════════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def get_connection() -> psycopg2.extensions.connection:
    conn = psycopg2.connect(**PG_CONN_PARAMS)
    conn.autocommit = False
    return conn


# Column case restoration — psycopg2 returns all names in lowercase.
# This dict maps lowercase → original mixed-case for the columns that exist
# in stg.FeatureStore_Raw.  All new 138-feature columns use their notebook
# names (which are already lowercase or have no mixed-case ambiguity) and
# pass through unchanged via the dict's default branch.
_COL_CASE: dict[str, str] = {
    'clientid':            'ClientID',
    'asofdate':            'AsOfDate',
    'birthdate':           'BirthDate',
    'gender':              'Gender',
    'educationtype':       'EducationType',
    'familystatus':        'FamilyStatus',
    'incometotal':         'IncomeTotal',
    'incometype':          'IncomeType',
    'owncar':              'OwnCar',
    'ownrealty':           'OwnRealty',
    'cntchildren':         'CNTChildren',
    'cntfamilymembers':    'CNTFamilyMembers',
    'regionrating':        'RegionRating',
    'days_employed':       'Days_Employed',
    'applicationid':       'ApplicationID',
    'applicationdate':     'ApplicationDate',
    'loanamount':          'LoanAmount',
    'annuity':             'Annuity',
    'loantenormonths':     'LoanTenorMonths',
    'goodspriceamount':    'GoodsPriceAmount',
    'contracttype':        'ContractType',
    'extscore1':           'ExtScore1',
    'extscore2':           'ExtScore2',
    'extscore3':           'ExtScore3',
    'activeloans':         'ActiveLoans',
    'closedloans':         'ClosedLoans',
    'enquiriesyear':       'EnquiriesYear',
    'avgbalance_90d':      'AvgBalance_90d',
    'minbalance_90d':      'MinBalance_90d',
    'maxbalance_90d':      'MaxBalance_90d',
    'salarycredits_90d':   'SalaryCredits_90d',
    'overdraftdays_90d':   'OverdraftDays_90d',
    'txncount_90d':        'TxnCount_90d',
    'avgtxnamount_90d':    'AvgTxnAmount_90d',
    'cashwithdraws_90d':   'CashWithdraws_90d',
    'etl_runid':           'ETL_RunID',
    'createdat':           'CreatedAt',
    'pd_score':            'PD_Score',
    'scoredate':           'ScoreDate',
}

# Columns in ml.FeatureStore that use quoted mixed-case identifiers (original DDL).
# All other columns (the 138 new feature cols) are stored lowercase.
_FS_MIXED_CASE: frozenset[str] = frozenset(
    {"ClientID", "AsOfDate", "ModelVersion", "ETL_RunID", "CreatedAt"}
)


def read_sql(sql: str, conn, params=None) -> pd.DataFrame:
    """psycopg2-native pd.read_sql replacement with column case restoration."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [_COL_CASE.get(d[0], d[0]) for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def cleanup_stale_running_rows(conn, run_date: date) -> None:
    """[LOG-1] Mark prior abandoned 'running' rows as 'failed' at startup."""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE stg.ETL_RunLog
            SET    Status       = 'failed',
                   ErrorMessage = 'Marked failed at next pipeline start (prior run abandoned)',
                   FinishedAt   = NOW()
            WHERE  RunDate = %s AND Status = 'running'
            """,
            (run_date,),
        )
        count = cur.rowcount
    conn.commit()
    if count:
        log.warning(f"[startup] Marked {count} stale 'running' rows as failed for {run_date}")


def log_step_start(conn, run_date: date, step_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stg.ETL_RunLog (RunDate, StepName, Status, StartedAt)
            VALUES (%s, %s, 'running', NOW())
            RETURNING RunID
            """,
            (run_date, step_name),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    log.info(f"[{step_name}] Started  (RunID={run_id})")
    return run_id


def log_step_done(conn, run_id: int, step_name: str,
                  rows: int = 0, duration_sec: int = 0) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE stg.ETL_RunLog
            SET    Status        = 'success',
                   RowsProcessed = %s,
                   DurationSec   = %s,
                   FinishedAt    = NOW()
            WHERE  RunID = %s
            """,
            (rows, duration_sec, run_id),
        )
    conn.commit()
    log.info(f"[{step_name}] Done  rows={rows:,}  {duration_sec}s")


def log_step_failed(conn, run_id: int, step_name: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE stg.ETL_RunLog
            SET    Status       = 'failed',
                   ErrorMessage = %s,
                   FinishedAt   = NOW()
            WHERE  RunID = %s
            """,
            (error[:4000], run_id),
        )
    conn.commit()
    log.error(f"[{step_name}] FAILED — {error[:200]}")


# ═════════════════════════════════════════════════════════════════════════════
# STEP A — BUILD FEATURE STORE
# ═════════════════════════════════════════════════════════════════════════════

# ETL-aligned categorical encoding maps — must exactly match ETL_CAT_MAPS
# in the notebook.  5 columns use fixed maps; all others use LabelEncoders
# loaded from model_artifacts/label_encoders.pkl.
ETL_CAT_MAPS: dict[str, dict] = {
    "NAME_CONTRACT_TYPE": {"Cash loans": 0, "Revolving loans": 1},
    "CODE_GENDER":        {"M": 0, "F": 1, "X": 2, "XNA": 2},
    "NAME_EDUCATION_TYPE": {
        "Secondary / secondary special": 0,
        "Higher education":              1,
        "Incomplete higher":             2,
        "Lower secondary":               3,
        "Academic degree":               4,
    },
    "NAME_FAMILY_STATUS": {
        "Married": 0, "Single / not married": 1,
        "Separated": 2, "Widow": 3, "Civil marriage": 4,
    },
    "NAME_INCOME_TYPE": {
        "Working": 0, "Commercial associate": 1, "Pensioner": 2,
        "State servant": 3, "Unemployed": 4, "Student": 5,
        "Businessman": 6, "Maternity leave": 7,
    },
}

_LABEL_ENCODERS = None

def _load_label_encoders() -> dict:
    import pickle
    global _LABEL_ENCODERS
    if _LABEL_ENCODERS is None:
        with open(Path("model_artifacts") / "label_encoders.pkl", "rb") as f:
            _LABEL_ENCODERS = pickle.load(f)
    return _LABEL_ENCODERS


def _encode_col(series: pd.Series, col: str) -> pd.Series:
    """Encode one categorical column. Unseen values → -1."""
    if col in ETL_CAT_MAPS:
        return series.fillna("MISSING").map(ETL_CAT_MAPS[col]).fillna(-1).astype(int)
    le_meta = _load_label_encoders().get(col)
    if le_meta is None:
        return series.fillna(-1).astype(int)
    if le_meta["type"] == "map":
        return series.fillna("MISSING").map(le_meta["mapping"]).fillna(-1).astype(int)
    le    = le_meta["encoder"]
    known = set(le.classes_)
    return series.fillna("MISSING").map(
        lambda v: int(le.transform([v])[0]) if v in known else -1
    ).astype(int)


FEATURE_STORE_RAW_SQL = """
SELECT
    r.*,
    (%(as_of_date)s::DATE - r.BirthDate) * -1 AS days_birth_raw
FROM stg.FeatureStore_Raw r
WHERE r.AsOfDate = %(as_of_date)s
"""


def build_feature_store(conn, run_date: date, run_id: int) -> pd.DataFrame:
    """
    Reads stg.FeatureStore_Raw and produces a DataFrame with EXACTLY the
    138 column names in feature_cols.json, matching the notebook training
    column names so the model receives the correct feature vector.

    [FIX-1] _COL_CASE is now applied when building the column list from
    the cursor, so psycopg2's lowercase output is restored to mixed-case
    before the rename_map runs.  Without this the rename silently did
    nothing and df["AMT_INCOME_TOTAL"] raised a KeyError.
    """
    log.info("Reading stg.FeatureStore_Raw ...")

    with conn.cursor() as cur:
        cur.execute(FEATURE_STORE_RAW_SQL, {"as_of_date": run_date})
        # [FIX-1]: restore mixed-case names via _COL_CASE
        cols = [_COL_CASE.get(d[0], d[0]) for d in cur.description]
        df   = pd.DataFrame(cur.fetchall(), columns=cols)

    if df.empty:
        raise ValueError(
            f"stg.FeatureStore_Raw has 0 rows for {run_date}. "
            "Did the SQL staging step complete?"
        )
    log.info(f"  {len(df):,} rows loaded")

    # psycopg2 returns DECIMAL/NUMERIC columns as decimal.Decimal objects.
    # Coerce every column that can be numeric to float/int so downstream
    # pandas arithmetic never raises "Decimal * float" type errors.
    # String columns (ContractType, Gender, etc.) are left unchanged.
    for _col in df.columns:
        try:
            df[_col] = pd.to_numeric(df[_col])
        except (ValueError, TypeError):
            pass

    # Restore uppercase names for any FEATURE_COLS column that came back
    # lowercase from psycopg2 (columns added via ALTER TABLE without quoting).
    # Build a lowercase → FEATURE_COLS casing map and rename in one pass.
    fc_lower_map = {c.lower(): c for c in FEATURE_COLS}
    case_restore = {
        col: fc_lower_map[col.lower()]
        for col in df.columns
        if col.lower() in fc_lower_map and col != fc_lower_map[col.lower()]
    }
    if case_restore:
        df = df.rename(columns=case_restore)
        log.info(f"  Restored case for {len(case_restore)} feature columns")

    # ── Rename ETL column names → notebook training column names ──────────────
    rename_map = {
        # Core application
        "LoanAmount":       "AMT_CREDIT",
        "Annuity":          "AMT_ANNUITY",
        "IncomeTotal":      "AMT_INCOME_TOTAL",
        "LoanTenorMonths":  "LOAN_TENOR_MONTHS_raw",
        "GoodsPriceAmount": "AMT_GOODS_PRICE",
        "ContractType":     "NAME_CONTRACT_TYPE_raw",
        # Client demographics
        "Gender":           "CODE_GENDER_raw",
        "EducationType":    "NAME_EDUCATION_TYPE_raw",
        "FamilyStatus":     "NAME_FAMILY_STATUS_raw",
        "IncomeType":       "NAME_INCOME_TYPE_raw",
        "OwnCar":           "FLAG_OWN_CAR",
        "OwnRealty":        "FLAG_OWN_REALTY",
        "CNTChildren":      "CNT_CHILDREN",
        "CNTFamilyMembers": "CNT_FAM_MEMBERS",
        "RegionRating":     "REGION_RATING_CLIENT",
        "Days_Employed":    "DAYS_EMPLOYED",
        # Bureau snapshot
        "ExtScore1":        "EXT_SOURCE_1",
        "ExtScore2":        "EXT_SOURCE_2",
        "ExtScore3":        "EXT_SOURCE_3",
        "ActiveLoans":      "bureau_active_count_snap",
        "ClosedLoans":      "bureau_closed_count_snap",
        "EnquiriesYear":    "AMT_REQ_CREDIT_BUREAU_YEAR_snap",
        # Days from birth (computed in SQL)
        "days_birth_raw":   "DAYS_BIRTH",
    }
    df = df.rename(columns=rename_map)

    # ── Encode categoricals ───────────────────────────────────────────────────
    cat_encode_map = {
        "NAME_CONTRACT_TYPE_raw":     "NAME_CONTRACT_TYPE",
        "CODE_GENDER_raw":            "CODE_GENDER",
        "NAME_EDUCATION_TYPE_raw":    "NAME_EDUCATION_TYPE",
        "NAME_FAMILY_STATUS_raw":     "NAME_FAMILY_STATUS",
        "NAME_INCOME_TYPE_raw":       "NAME_INCOME_TYPE",
        "NAME_TYPE_SUITE":            "NAME_TYPE_SUITE",
        "WEEKDAY_APPR_PROCESS_START": "WEEKDAY_APPR_PROCESS_START",
        "NAME_HOUSING_TYPE":          "NAME_HOUSING_TYPE",
        "OCCUPATION_TYPE":            "OCCUPATION_TYPE",
        "ORGANIZATION_TYPE":          "ORGANIZATION_TYPE",
        "EMERGENCYSTATE_MODE":        "EMERGENCYSTATE_MODE",
    }
    for src, dst in cat_encode_map.items():
        if src in df.columns:
            df[dst] = _encode_col(df[src].astype(str), dst)
            if src != dst:
                df = df.drop(columns=[src])
        elif dst not in df.columns:
            df[dst] = -1

    # ── Derived ratio features ────────────────────────────────────────────────
    income = df["AMT_INCOME_TOTAL"].clip(lower=1)
    goods  = (
        df["AMT_GOODS_PRICE"].clip(lower=1)
        if "AMT_GOODS_PRICE" in df.columns
        else pd.Series(1, index=df.index)
    )

    df["CREDIT_INCOME_RATIO"]  = df["AMT_CREDIT"] / income
    df["ANNUITY_INCOME_RATIO"] = df["AMT_ANNUITY"] / income
    df["CREDIT_GOODS_RATIO"]   = df["AMT_CREDIT"]  / goods
    df["ANNUITY_CREDIT_RATIO"] = df["AMT_ANNUITY"] / (df["AMT_CREDIT"] + 1)
    df["LOAN_TENOR_MONTHS"]    = df["AMT_CREDIT"]  / (df["AMT_ANNUITY"] + 1)

    # Employment years (negative DAYS_EMPLOYED = currently employed)
    emp = df["DAYS_EMPLOYED"].where(df["DAYS_EMPLOYED"] < 0, other=np.nan)
    df["EMPLOYMENT_YEARS"] = (-emp / 365.25).clip(lower=0)

    # External score composites
    ext = df[["EXT_SOURCE_2", "EXT_SOURCE_3"]].copy()
    if "EXT_SOURCE_1" in df.columns:
        ext["EXT_SOURCE_1"] = df["EXT_SOURCE_1"]
    df["EXT_SOURCE_MEAN"]    = ext.mean(axis=1)
    df["EXT_SOURCE_MIN"]     = ext.min(axis=1)
    df["EXT_SOURCE_PRODUCT"] = ext.fillna(0.5).prod(axis=1)

    # Document flags total
    doc_cols    = [f"FLAG_DOCUMENT_{i}" for i in range(2, 22)]
    present_doc = [c for c in doc_cols if c in df.columns]
    df["TOTAL_DOC_FLAGS"] = df[present_doc].fillna(0).sum(axis=1).astype(int)

    # Bureau enquiry total
    enq_cols    = [f"AMT_REQ_CREDIT_BUREAU_{w}" for w in ["HOUR","DAY","WEEK","MON","QRT","YEAR"]]
    present_enq = [c for c in enq_cols if c in df.columns]
    df["TOTAL_ENQUIRIES"] = df[present_enq].fillna(0).sum(axis=1).astype(int)

    # Bureau derived ratios
    if "bureau_total_debt" in df.columns and "bureau_total_limit" in df.columns:
        df["bureau_debt_limit_ratio"] = (
            df["bureau_total_debt"].fillna(0) /
            (df["bureau_total_limit"].fillna(0) + 1)
        )
    if "bureau_active_count" in df.columns and "bureau_loan_count" in df.columns:
        # Name matches feature_cols.json — notebook merge produced _x suffix
        # but the feature selection list uses the base name.
        df["bureau_active_ratio"] = (
            df["bureau_active_count"].fillna(0) /
            (df["bureau_loan_count"].fillna(0) + 1)
        )
    if "bureau_total_overdue" in df.columns and "bureau_total_debt" in df.columns:
        df["bureau_overdue_ratio"] = (
            df["bureau_total_overdue"].fillna(0) /
            (df["bureau_total_debt"].fillna(0) + 1)
        )
    if "bureau_days_credit_std" in df.columns:
        df["bureau_stability"] = 1 / (df["bureau_days_credit_std"].fillna(0) + 1)
    if "bureau_last_credit" in df.columns:
        # [FIX-4]: use bureau aggregation column, not the snapshot col that was
        # already renamed and dropped by this point in the function.
        df["bureau_active_mean"] = (
            df["bureau_active_count"].fillna(0) /
            (df["bureau_loan_count"].fillna(0) + 1)
        )

    # Previous application derived
    if "prev_app_count" in df.columns:
        df["prev_approval_rate"] = (
            df["prev_approved_count"].fillna(0) /
            (df["prev_app_count"].fillna(0) + 1)
        )
        df["prev_refusal_rate"] = (
            df["prev_refused_count"].fillna(0) /
            (df["prev_app_count"].fillna(0) + 1)
        )
    if "prev_app_span" in df.columns and "prev_days_std" in df.columns:
        df["prev_app_frequency"] = (
            df["prev_app_span"].fillna(0) /
            (df["prev_days_std"].fillna(0) + 1)
        )

    # Installment derived
    if "inst_avg_payment" in df.columns and "inst_avg_days_delay" in df.columns:
        df["inst_payment_ratio"] = (
            df["inst_avg_payment"].fillna(0) /
            (df["inst_avg_payment"].fillna(0) + 1)
        )
        df["inst_payment_gap"] = 0.0   # inst_avg_instalment not stored separately
    if "inst_max_days_delay" in df.columns and "inst_avg_days_delay" in df.columns:
        df["inst_delay_risk"] = (
            df["inst_max_days_delay"].fillna(0) -
            df["inst_avg_days_delay"].fillna(0)
        )

    # POS cash derived
    if "pos_count" in df.columns and "pos_completed" in df.columns:
        df["pos_active_ratio"] = (
            (df["pos_count"].fillna(0) - df["pos_completed"].fillna(0)) /
            (df["pos_count"].fillna(0) + 1)
        ).clip(0, 1)

    # Safety clips — guard against extreme values overflowing DB column types.
    # These arise when income/annuity is near-zero for edge-case applications.
    ratio_clips = {
        "CREDIT_INCOME_RATIO":   (-999999, 999999),
        "ANNUITY_INCOME_RATIO":  (-999999, 999999),
        "CREDIT_GOODS_RATIO":    (-999999, 999999),
        "ANNUITY_CREDIT_RATIO":  (-9999,   9999),
        "LOAN_TENOR_MONTHS":     (0,       99999),
        "EMPLOYMENT_YEARS":      (0,       9999),
        "bureau_stability":      (0,       99999),
        "bureau_debt_limit_ratio": (-99999, 99999),
        "bureau_overdue_ratio":  (-99999, 99999),
        "bureau_active_ratio_x": (0,      1),
        "prev_approval_rate":    (0,      1),
        "prev_refusal_rate":     (0,      1),
        "prev_app_frequency":    (-99999, 99999),
        "inst_payment_ratio":    (-99999, 99999),
        "inst_payment_gap":      (-999999, 999999),
        "inst_delay_risk":       (-9999,  9999),
        "pos_active_ratio":      (0,      1),
    }
    for col, (lo, hi) in ratio_clips.items():
        if col in df.columns:
            df[col] = df[col].clip(lo, hi)

    # Drop helper columns not in FEATURE_COLS
    drop_cols = [
        "bureau_active_count_snap", "bureau_closed_count_snap",
        "AMT_REQ_CREDIT_BUREAU_YEAR_snap", "EXT_SOURCE_1",
        "AMT_GOODS_PRICE", "LOAN_TENOR_MONTHS_raw",
    ]
    df = df.drop(columns=[c for c in drop_cols if c in df.columns], errors="ignore")

    # Coerce all feature columns to numeric
    for col in FEATURE_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    log.info("  Derived features computed")
    return df


def write_feature_store(conn, df: pd.DataFrame, run_id: int, mv: str) -> None:
    """
    Bulk-upsert the 138-feature store into ml.FeatureStore.

    [FIX-2] All columns in ml.FeatureStore are stored lowercase by PostgreSQL
    (none were created with quoted identifiers in the DDL).  Every column name
    in the INSERT is lowercased — no double-quoting at all.

    [FIX-3] ModelVersion is taken from the mv parameter.
    """
    key_cols  = ["ClientID", "AsOfDate", "ModelVersion"]
    data_cols = [c for c in FEATURE_COLS if c in df.columns]
    all_cols  = key_cols + data_cols + ["ETL_RunID"]

    df_out = df[["ClientID", "AsOfDate"] + data_cols].copy()
    df_out.insert(2, "ModelVersion", mv)
    df_out["ETL_RunID"] = run_id
    df_out = df_out.reindex(columns=all_cols)

    def _coerce(v):
        """bool → int (SMALLINT columns reject Python bool); NaN → NULL."""
        if isinstance(v, (bool, np.bool_)):
            return int(v)
        if isinstance(v, float) and np.isnan(v):
            return None
        return v

    records = [
        tuple(_coerce(v) for v in row)
        for row in df_out.itertuples(index=False, name=None)
    ]

    # All columns are lowercase in the DB — no quoting needed or wanted.
    col_list   = ", ".join(c.lower() for c in all_cols)
    set_clause = ", ".join(
        f"{c.lower()} = EXCLUDED.{c.lower()}"
        for c in data_cols + ["ETL_RunID"]
    )
    conflict_cols = "clientid, asofdate, modelversion"

    sql = f"""
        INSERT INTO ml.FeatureStore ({col_list})
        VALUES %s
        ON CONFLICT ({conflict_cols}) DO UPDATE SET {set_clause}
    """
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, records, page_size=2000)
        conn.commit()
        log.info(
            f"  ml.FeatureStore: {len(records):,} rows upserted "
            f"({len(data_cols)} features, ModelVersion={mv})"
        )
    except Exception as e:
        conn.rollback()
        log.error(f"  write_feature_store FAILED: {e}")
        raise


# ═════════════════════════════════════════════════════════════════════════════
# STEP B — LOAD CHAMPION MODEL
# ═════════════════════════════════════════════════════════════════════════════

def load_champion_model():
    """
    Load the champion model from model_artifacts/champion_model.pkl.
    Returns (_Wrapper, mlflow_run_id, local_model_version).

    [FIX-1 carried from v3.0] All variables are local; no shadowing of
    module-level FEATURE_COLS or model_version.
    """
    import pickle

    ARTIFACTS_DIR = Path("model_artifacts")

    with open(ARTIFACTS_DIR / "champion_model.pkl", "rb") as f:
        native_model = pickle.load(f)

    with open(ARTIFACTS_DIR / "feature_cols.json") as _f:
        feature_cols: list[str] = json.load(_f)

    with open(ARTIFACTS_DIR / "model_card.json") as _f:
        _card = json.load(_f)

    local_model_version = f"v2-{_card['champion_model'].lower()}"
    mlflow_run_id       = _card.get("mlflow_run_id", "local")

    class _Wrapper:
        def __init__(self, m, cols):
            self._m   = m
            self.cols = cols

        def predict(self, X):
            if isinstance(X, pd.DataFrame):
                return self._m.predict_proba(
                    X.reindex(columns=self.cols, fill_value=-999)
                )[:, 1]
            return self._m.predict_proba(X)[:, 1]

        def unwrap_python_model(self):
            return self._m

    log.info(f"Loaded champion: {local_model_version}  ({len(feature_cols)} features)")
    return _Wrapper(native_model, feature_cols), mlflow_run_id, local_model_version


# ═════════════════════════════════════════════════════════════════════════════
# STEP C — SCORE APPLICATIONS
# ═════════════════════════════════════════════════════════════════════════════

def score_applications(df: pd.DataFrame, model) -> np.ndarray:
    """Score in batches to keep memory bounded."""
    # reindex fills any missing FEATURE_COLS with -999 sentinel rather than
    # raising KeyError — safe for clients with no bureau/POS history.
    X          = df.reindex(columns=FEATURE_COLS, fill_value=-999)
    all_scores = []

    for start in range(0, len(X), BATCH_SIZE):
        batch = X.iloc[start : start + BATCH_SIZE]
        probs = model.predict(batch)
        if isinstance(probs, pd.DataFrame):
            probs = probs.iloc[:, 1].values
        elif isinstance(probs, np.ndarray) and probs.ndim == 2:
            probs = probs[:, 1]
        else:
            probs = np.asarray(probs).ravel()
        all_scores.append(probs)

    pd_scores = np.concatenate(all_scores)
    log.info(
        f"  Scored {len(pd_scores):,} applications  "
        f"mean PD={pd_scores.mean():.4f}  "
        f"rate>0.5={(pd_scores > 0.5).mean():.2%}"
    )
    return pd_scores


def assign_risk_tiers(pd_scores: np.ndarray) -> np.ndarray:
    p_low    = np.percentile(pd_scores, TIER_LOW_PCT)
    p_medium = np.percentile(pd_scores, TIER_MEDIUM_PCT)
    p_high   = np.percentile(pd_scores, TIER_HIGH_PCT)
    return np.where(
        pd_scores <= p_low,    "LOW",
        np.where(pd_scores <= p_medium, "MEDIUM",
        np.where(pd_scores <= p_high,   "HIGH", "CRITICAL"))
    )


def assign_deciles(pd_scores: np.ndarray) -> np.ndarray:
    ranks   = pd.Series(pd_scores).rank(pct=True)
    deciles = np.ceil(ranks * 10).clip(1, 10).astype(int).values
    return deciles


# ═════════════════════════════════════════════════════════════════════════════
# STEP D — PSI MONITORING
# ═════════════════════════════════════════════════════════════════════════════

def compute_psi(expected: np.ndarray, actual: np.ndarray,
                n_bins: int = 10) -> float:
    breakpoints = np.unique(np.percentile(expected, np.linspace(0, 100, n_bins + 1)))

    def safe_pct(arr, bp):
        counts, _ = np.histogram(arr, bins=bp)
        pct = counts / len(arr)
        return np.where(pct == 0, 1e-4, pct)

    exp_pct = safe_pct(expected, breakpoints)
    act_pct = safe_pct(actual,   breakpoints)
    return round(float(np.sum((act_pct - exp_pct) * np.log(act_pct / exp_pct))), 4)


def check_psi(conn, run_date: date, current_scores: np.ndarray,
              run_id: int, mv: str) -> str:
    """[PSI-1] Rolling reference window PSI. Returns 'OK', 'MONITOR', or 'RETRAIN'."""
    log.info("Running PSI check ...")

    baseline_start = run_date - timedelta(days=PSI_BASELINE_LOOKBACK_DAYS)
    baseline_df    = read_sql(
        """SELECT PD_Score FROM ml.PD_Scores
           WHERE ScoreDate BETWEEN %s AND %s
           ORDER BY RANDOM() LIMIT 50000""",
        conn,
        params=(baseline_start, run_date - timedelta(days=1)),
    )

    if baseline_df.empty:
        log.info(
            f"  No baseline scores in {PSI_BASELINE_LOOKBACK_DAYS}-day window "
            "— skipping PSI (likely first run)"
        )
        psi_score, psi_status = 0.0, "OK"
    else:
        psi_score = compute_psi(baseline_df["PD_Score"].values, current_scores)
        if psi_score < PSI_MONITOR_THRESHOLD:
            psi_status = "OK"
        elif psi_score < PSI_RETRAIN_THRESHOLD:
            psi_status = "MONITOR"
        else:
            psi_status = "RETRAIN"

    log.info(f"  PSI={psi_score:.4f}  →  {psi_status}")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ml.PSI_Monitor
                (MonitorDate, PSI_Score, PSI_Status, ModelVersion, AlertSent, ETL_RunID)
            VALUES (%s, %s, %s, %s, FALSE, %s)
            ON CONFLICT (MonitorDate, ModelVersion) DO UPDATE SET
                PSI_Score  = EXCLUDED.PSI_Score,
                PSI_Status = EXCLUDED.PSI_Status,
                ETL_RunID  = EXCLUDED.ETL_RunID
            """,
            (run_date, psi_score, psi_status, mv, run_id),
        )
    conn.commit()

    if psi_status == "RETRAIN":
        log.warning(
            f"PSI {psi_score:.4f} exceeds retrain threshold "
            f"{PSI_RETRAIN_THRESHOLD}. Retraining should be triggered."
        )
    return psi_status


# ═════════════════════════════════════════════════════════════════════════════
# STEP E — SHAP EXPLANATIONS
# ═════════════════════════════════════════════════════════════════════════════

def _unwrap_model(model) -> object:
    """[SHAP-3] Safely unwrap pyfunc wrapper to native model for TreeExplainer."""
    try:
        return model.unwrap_python_model()
    except Exception:
        pass
    try:
        inner = model._model_impl
        if hasattr(inner, "lgb_model"):
            return inner.lgb_model
        if hasattr(inner, "xgb_model"):
            return inner.xgb_model
        return inner
    except Exception:
        pass
    log.warning("Could not unwrap model — falling back to pyfunc for SHAP.")
    return model


# def compute_shap(df: pd.DataFrame, model) -> np.ndarray:
#     log.info("Computing SHAP values ...")
#     underlying = _unwrap_model(model)
#     X          = df.reindex(columns=FEATURE_COLS, fill_value=-999).values
#     explainer  = shap.TreeExplainer(underlying)
#     shap_vals  = explainer.shap_values(X)
#     if isinstance(shap_vals, list):
#         shap_vals = shap_vals[1]
#     shap_vals = np.asarray(shap_vals, dtype=float)
#     log.info(f"  SHAP shape: {shap_vals.shape}")
#     return shap_vals

def compute_shap(df: pd.DataFrame, model) -> np.ndarray:
    log.info("Computing SHAP values ...")
    underlying = _unwrap_model(model)
    X          = df.reindex(columns=FEATURE_COLS, fill_value=-999).values
    explainer  = shap.TreeExplainer(underlying)

    SHAP_BATCH = 5_000
    batches    = []
    for start in range(0, len(X), SHAP_BATCH):
        chunk = X[start : start + SHAP_BATCH]
        vals  = explainer.shap_values(chunk)
        if isinstance(vals, list):
            vals = vals[1]
        batches.append(np.asarray(vals, dtype=float))
        log.info(f"  SHAP batch {start // SHAP_BATCH + 1}"
                 f"/{-(-len(X) // SHAP_BATCH)} done")

    shap_vals = np.concatenate(batches, axis=0)
    log.info(f"  SHAP shape: {shap_vals.shape}")
    return shap_vals

def build_shap_rows(df: pd.DataFrame, shap_vals: np.ndarray,
                    run_date: date, run_id: int) -> list[tuple]:
    """[SHAP-1] Application-first iteration."""
    feature_names = np.array(FEATURE_COLS)
    app_ids       = df["ApplicationID"].values
    feat_values   = df.reindex(columns=FEATURE_COLS, fill_value=-999).values
    abs_shap      = np.abs(shap_vals)
    top_idx       = np.argsort(-abs_shap, axis=1)[:, :SHAP_TOP_N]

    records = []
    for i in range(shap_vals.shape[0]):
        for rank_0 in range(SHAP_TOP_N):
            col = top_idx[i, rank_0]
            sv  = shap_vals[i, col]
            fv  = feat_values[i, col]
            records.append((
                int(app_ids[i]),
                run_date,
                str(feature_names[col]),
                float(fv),
                float(sv),
                float(abs(sv)),
                rank_0 + 1,
                "+" if sv >= 0 else "-",
                run_id,
            ))

    log.info(f"  SHAP rows built: {len(records):,} ({SHAP_TOP_N} per application)")
    return records


# ═════════════════════════════════════════════════════════════════════════════
# STEP F — WRITE OUTPUTS
# ═════════════════════════════════════════════════════════════════════════════

def write_scores(conn, df: pd.DataFrame, pd_scores: np.ndarray,
                 tiers: np.ndarray, deciles: np.ndarray,
                 mv: str, mlflow_run_id: str, run_id: int) -> None:
    """[SCORE-1] ScoreDate sourced explicitly from df["AsOfDate"]."""
    rows = list(zip(
        df["ApplicationID"].astype(int).tolist(),
        df["ClientID"].astype(int).tolist(),
        df["AsOfDate"].tolist(),
        pd_scores.tolist(),
        tiers.tolist(),
        deciles.tolist(),
        [mv]            * len(df),
        [mlflow_run_id] * len(df),
        [run_id]        * len(df),
    ))
    sql = """
        INSERT INTO ml.PD_Scores
            (ApplicationID, ClientID, ScoreDate, PD_Score, RiskTier,
             ScoreDecile, ModelVersion, MLflowRunID, ETL_RunID)
        VALUES %s
        ON CONFLICT (ApplicationID, ScoreDate) DO UPDATE SET
            PD_Score     = EXCLUDED.PD_Score,
            RiskTier     = EXCLUDED.RiskTier,
            ScoreDecile  = EXCLUDED.ScoreDecile,
            ModelVersion = EXCLUDED.ModelVersion,
            MLflowRunID  = EXCLUDED.MLflowRunID,
            ETL_RunID    = EXCLUDED.ETL_RunID
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=2000)
    conn.commit()
    log.info(f"  ml.PD_Scores: {len(rows):,} rows upserted")


def write_shap_values(conn, shap_rows: list[tuple]) -> None:
    """[SHAP-2] ON CONFLICT DO NOTHING — idempotent on reruns."""
    sql = """
        INSERT INTO ml.SHAP_Values
            (ApplicationID, ScoreDate, FeatureName, FeatureValue,
             SHAPValue, AbsSHAPValue, SHAPRank, Direction, ETL_RunID)
        VALUES %s
        ON CONFLICT (ApplicationID, ScoreDate, FeatureName, SHAPRank)
        DO NOTHING
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, shap_rows, page_size=5000)
    conn.commit()
    log.info(f"  ml.SHAP_Values: {len(shap_rows):,} rows inserted")


# ═════════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════

def run_pipeline(run_date: date) -> None:
    log.info("=" * 60)
    log.info(f"CREDIT RISK ETL PIPELINE  —  run_date={run_date}")
    log.info("=" * 60)

    conn = get_connection()
    cleanup_stale_running_rows(conn, run_date)

    try:
        # ── A: Build feature store ─────────────────────────────────────────
        t0     = datetime.utcnow()
        run_id = log_step_start(conn, run_date, "features")
        try:
            df = build_feature_store(conn, run_date, run_id)
            # [FIX-3]: pass module-level model_version so FeatureStore.ModelVersion
            # matches PD_Scores.ModelVersion (both "v2-catboost").
            write_feature_store(conn, df, run_id, model_version)
            secs = int((datetime.utcnow() - t0).total_seconds())
            log_step_done(conn, run_id, "features", len(df), secs)
        except Exception:
            log_step_failed(conn, run_id, "features", traceback.format_exc())
            raise

        # ── B: Load champion model ─────────────────────────────────────────
        t0     = datetime.utcnow()
        run_id = log_step_start(conn, run_date, "model_load")
        try:
            model, mlflow_run_id, mv = load_champion_model()
            secs = int((datetime.utcnow() - t0).total_seconds())
            log_step_done(conn, run_id, "model_load", 1, secs)
        except Exception:
            log_step_failed(conn, run_id, "model_load", traceback.format_exc())
            raise

        # ── C: Score applications ──────────────────────────────────────────
        t0     = datetime.utcnow()
        run_id = log_step_start(conn, run_date, "scoring")
        try:
            pd_scores = score_applications(df, model)
            tiers     = assign_risk_tiers(pd_scores)
            deciles   = assign_deciles(pd_scores)
            secs = int((datetime.utcnow() - t0).total_seconds())
            log_step_done(conn, run_id, "scoring", len(pd_scores), secs)
        except Exception:
            log_step_failed(conn, run_id, "scoring", traceback.format_exc())
            raise

        # ── D: PSI monitoring ──────────────────────────────────────────────
        t0     = datetime.utcnow()
        run_id = log_step_start(conn, run_date, "psi")
        try:
            psi_status = check_psi(conn, run_date, pd_scores, run_id, mv)
            secs = int((datetime.utcnow() - t0).total_seconds())
            log_step_done(conn, run_id, "psi", 1, secs)
        except Exception:
            log_step_failed(conn, run_id, "psi", traceback.format_exc())
            raise

        # ── E: SHAP explanations ───────────────────────────────────────────
        t0     = datetime.utcnow()
        run_id = log_step_start(conn, run_date, "shap")
        try:
            shap_vals = compute_shap(df, model)
            shap_rows = build_shap_rows(df, shap_vals, run_date, run_id)
            secs = int((datetime.utcnow() - t0).total_seconds())
            log_step_done(conn, run_id, "shap", len(shap_rows), secs)
        except Exception:
            log_step_failed(conn, run_id, "shap", traceback.format_exc())
            raise

        # ── F: Write all outputs ───────────────────────────────────────────
        t0     = datetime.utcnow()
        run_id = log_step_start(conn, run_date, "write_outputs")
        try:
            write_scores(conn, df, pd_scores, tiers, deciles,
                         mv, mlflow_run_id, run_id)
            write_shap_values(conn, shap_rows)
            secs = int((datetime.utcnow() - t0).total_seconds())
            log_step_done(conn, run_id, "write_outputs", len(df), secs)
        except Exception:
            log_step_failed(conn, run_id, "write_outputs", traceback.format_exc())
            raise

        # ── Summary ────────────────────────────────────────────────────────
        tier_counts = dict(zip(*np.unique(tiers, return_counts=True)))
        log.info("=" * 60)
        log.info("PIPELINE COMPLETE")
        log.info(f"  Applications scored : {len(df):,}")
        log.info(f"  LOW risk            : {tier_counts.get('LOW',    0):,}")
        log.info(f"  MEDIUM risk         : {tier_counts.get('MEDIUM', 0):,}")
        log.info(f"  HIGH risk           : {tier_counts.get('HIGH',   0):,}")
        log.info(f"  Mean PD score       : {pd_scores.mean():.4f}")
        log.info(f"  PSI status          : {psi_status}")
        log.info(f"  Model version       : {mv}")
        log.info("=" * 60)

    except Exception as exc:
        log.critical(f"PIPELINE FAILED: {exc}")
        conn.close()
        sys.exit(1)

    conn.close()
    sys.exit(0)


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Credit Risk PD Model — Nightly ETL Pipeline"
    )
    parser.add_argument(
        "--run-date",
        type=str,
        default=str(date.today()),
        help="Processing date YYYY-MM-DD (default: today)",
    )
    args = parser.parse_args()

    try:
        run_date = date.fromisoformat(args.run_date)
    except ValueError:
        log.error(f"Invalid --run-date '{args.run_date}'. Use YYYY-MM-DD.")
        sys.exit(1)

    run_pipeline(run_date)

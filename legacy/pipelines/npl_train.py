# ============================================================
# npl_train.py
# NPL early-warning model — trains on DB data, saves artifacts,
# scores active loans, writes to ml.NPL_Scores.
#
# Usage:
#   python npl_train.py --mode train    # train + evaluate + save artifacts
#   python npl_train.py --mode score    # load artifacts + score + write DB
#   python npl_train.py --mode eval     # load artifacts + re-evaluate
#
# Model: XGBoost binary classifier
# Target: TARGET column from LoanApplications (8.1% positive rate)
# Features: stg.v_NPLFeatures (DPD history, payment ratios, bureau, txn)
# Validation: out-of-time split (earlier 80% train, later 20% test)
# ============================================================

import argparse, json, logging, os, sys, time
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
from sklearn.metrics import (roc_auc_score, average_precision_score,
                             brier_score_loss, classification_report)
from sklearn.model_selection import train_test_split
import xgboost as xgb
import shap
import joblib

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── DB connection ─────────────────────────────────────────────
PG = dict(
    host="localhost", port=5432,
    dbname="credit_risk_db_v2",
    user=os.getenv("PG_USER", "postgres"), password=os.getenv("PG_PASS", "")
)

def get_conn():
    return psycopg2.connect(**PG)

# ── Artifact paths ────────────────────────────────────────────
ARTIFACTS = Path("npl_artifacts")
ARTIFACTS.mkdir(exist_ok=True)

MODEL_VERSION = "npl-v1"

# ── Feature columns ───────────────────────────────────────────
# All columns from stg.v_NPLFeatures used as model features.
# Excludes: IDs, dates, label columns, and boolean flag columns
# that are represented numerically.
# removing cntchildren, cntfamilymembers, regionrating
FEATURE_COLS = [
    # Loan context
    "loanamount", "annuity", "loantenormonths", "loan_tenor_years",
    # Client demographics
    "incometotal", "employed_years",
    # PD at origination (strong prior)
    "pd_score_at_origin",
    # Installment volume
    "total_instalments", "loans_with_instalments",
    # Unpaid signal
    "unpaid_instalment_count",
    # DPD history
    "max_dpd_ever", "dpd_30_ever", "dpd_60_ever", "dpd_90_ever",
    "late_payment_count", "dpd30_count", "dpd60_count", "dpd90_count",
    # Payment ratio signals
    "payment_ratio_overall", "payment_ratio_3m", "payment_ratio_6m",
    "payment_ratio_min",
    # Payment trend
    "payment_ratio_trend",
    # Late payment recency
    "instalments_since_last_late",
    # Shortfall + stability
    "total_payment_shortfall", "instalment_amount_std",
    # POS signals
    "pos_dpd_avg", "pos_dpd_max", "pos_dpd_def_avg", "pos_dpd_def_max",
    "pos_late_months", "pos_dpd30_months", "pos_completed_count",
    # Bureau stress
    "bureau_bad_debt_count", "bureau_total_overdue", "bureau_max_overdue",
    "bureau_total_debt", "bureau_total_limit", "bureau_credit_util",
    "bureau_prolong_count",
    # Transaction stress (90d)
    "overdraft_count_90d", "salary_credit_count_90d",
    "min_balance_90d", "avg_balance_90d", "balance_cv_90d",
    # Composite score (useful as a meta-feature)
    "npl_composite_score",
]

# Categorical columns — encoded as integers for XGBoost
CAT_COLS = ["contracttype"]

# ── Top N SHAP features to write per application ──────────────
TOP_N_SHAP = 5


# ============================================================
# SECTION 1: DATA LOADING
# ============================================================

TRAINING_SQL = """
SELECT
    f.ApplicationID,
    f.ClientID,
    f.FeatureDate,
    la.ApplicationDate,

    -- ── Features ─────────────────────────────────────────────
    f.loanamount,
    f.annuity,
    f.loantenormonths,
    f.loan_tenor_years,
    f.incometotal,
    f.employed_years,
    f.cntchildren,
    f.cntfamilymembers,
    f.regionrating,
    COALESCE(f.pd_score_at_origin, 0.5)         AS pd_score_at_origin,
    f.total_instalments,
    f.loans_with_instalments,
    f.unpaid_instalment_count,
    f.max_dpd_ever,
    f.dpd_30_ever,
    f.dpd_60_ever,
    f.dpd_90_ever,
    f.late_payment_count,
    f.dpd30_count,
    f.dpd60_count,
    f.dpd90_count,
    COALESCE(f.payment_ratio_overall, 1.0)      AS payment_ratio_overall,
    COALESCE(f.payment_ratio_3m, 1.0)           AS payment_ratio_3m,
    COALESCE(f.payment_ratio_6m, 1.0)           AS payment_ratio_6m,
    COALESCE(f.payment_ratio_min, 1.0)          AS payment_ratio_min,
    COALESCE(f.payment_ratio_trend, 0.0)        AS payment_ratio_trend,
    COALESCE(f.instalments_since_last_late, 999) AS instalments_since_last_late,
    COALESCE(f.total_payment_shortfall, 0)      AS total_payment_shortfall,
    COALESCE(f.instalment_amount_std, 0)        AS instalment_amount_std,
    f.pos_dpd_avg,
    f.pos_dpd_max,
    f.pos_dpd_def_avg,
    f.pos_dpd_def_max,
    f.pos_late_months,
    f.pos_dpd30_months,
    f.pos_completed_count,
    f.bureau_bad_debt_count,
    f.bureau_total_overdue,
    f.bureau_max_overdue,
    f.bureau_total_debt,
    f.bureau_total_limit,
    COALESCE(f.bureau_credit_util, 0)           AS bureau_credit_util,
    f.bureau_prolong_count,
    f.overdraft_count_90d,
    f.salary_credit_count_90d,
    f.min_balance_90d,
    f.avg_balance_90d,
    COALESCE(f.balance_cv_90d, 0)               AS balance_cv_90d,
    f.npl_composite_score,

    -- ── Contract type encoded ─────────────────────────────────
    CASE la.ContractType
        WHEN 'Cash loans'      THEN 0
        WHEN 'Revolving loans' THEN 1
        ELSE -1
    END                                         AS contracttype,

    -- ── Label ────────────────────────────────────────────────
    CASE WHEN la.TARGET = TRUE THEN 1 ELSE 0 END AS npl_label

FROM stg.v_NPLFeatures f
JOIN public.LoanApplications la ON la.ApplicationID = f.ApplicationID
WHERE la.TARGET IS NOT NULL
ORDER BY la.ApplicationDate ASC   -- sort for out-of-time split
"""

SCORING_SQL = """
SELECT
    f.ApplicationID,
    f.ClientID,
    f.FeatureDate,
    la.ApplicationDate,
    COALESCE(f.pd_score_at_origin, 0.5)         AS pd_score_at_origin,
    la.LoanAmount,
    la.Annuity,
    la.LoanTenorMonths,
    f.loan_tenor_years,
    f.incometotal,
    f.employed_years,
    f.cntchildren,
    f.cntfamilymembers,
    f.regionrating,
    f.total_instalments,
    f.loans_with_instalments,
    f.unpaid_instalment_count,
    f.max_dpd_ever,
    f.dpd_30_ever,
    f.dpd_60_ever,
    f.dpd_90_ever,
    f.late_payment_count,
    f.dpd30_count,
    f.dpd60_count,
    f.dpd90_count,
    COALESCE(f.payment_ratio_overall, 1.0)      AS payment_ratio_overall,
    COALESCE(f.payment_ratio_3m, 1.0)           AS payment_ratio_3m,
    COALESCE(f.payment_ratio_6m, 1.0)           AS payment_ratio_6m,
    COALESCE(f.payment_ratio_min, 1.0)          AS payment_ratio_min,
    COALESCE(f.payment_ratio_trend, 0.0)        AS payment_ratio_trend,
    COALESCE(f.instalments_since_last_late, 999) AS instalments_since_last_late,
    COALESCE(f.total_payment_shortfall, 0)      AS total_payment_shortfall,
    COALESCE(f.instalment_amount_std, 0)        AS instalment_amount_std,
    f.pos_dpd_avg,
    f.pos_dpd_max,
    f.pos_dpd_def_avg,
    f.pos_dpd_def_max,
    f.pos_late_months,
    f.pos_dpd30_months,
    f.pos_completed_count,
    f.bureau_bad_debt_count,
    f.bureau_total_overdue,
    f.bureau_max_overdue,
    f.bureau_total_debt,
    f.bureau_total_limit,
    COALESCE(f.bureau_credit_util, 0)           AS bureau_credit_util,
    f.bureau_prolong_count,
    f.overdraft_count_90d,
    f.salary_credit_count_90d,
    f.min_balance_90d,
    f.avg_balance_90d,
    COALESCE(f.balance_cv_90d, 0)               AS balance_cv_90d,
    f.npl_composite_score,
    CASE la.ContractType
        WHEN 'Cash loans'      THEN 0
        WHEN 'Revolving loans' THEN 1
        ELSE -1
    END                                         AS contracttype,
    -- PD risk tier for context
    COALESCE(pd.RiskTier, 'UNKNOWN')            AS pd_risk_tier
FROM stg.v_NPLFeatures f
JOIN public.LoanApplications la ON la.ApplicationID = f.ApplicationID
LEFT JOIN LATERAL (
    SELECT RiskTier
    FROM ml.PD_Scores
    WHERE ApplicationID = la.ApplicationID
    ORDER BY ScoreDate DESC LIMIT 1
) pd ON TRUE
"""


def load_training_data(conn) -> pd.DataFrame:
    log.info("Loading NPL training data from DB ...")
    t0 = time.perf_counter()
    df = pd.read_sql(TRAINING_SQL, conn)
    log.info(f"  {len(df):,} rows loaded in {time.perf_counter()-t0:.1f}s")
    log.info(f"  Positive rate: {df['npl_label'].mean():.1%}  "
             f"({df['npl_label'].sum():,} defaults)")
    log.info(f"  Date range: {str(df['applicationdate'].min())} → "
             f"{str(df['applicationdate'].max())}")
    return df


def load_scoring_data(conn) -> pd.DataFrame:
    log.info("Loading scoring data from DB ...")
    t0 = time.perf_counter()
    df = pd.read_sql(SCORING_SQL, conn)
    log.info(f"  {len(df):,} rows loaded in {time.perf_counter()-t0:.1f}s")
    return df


# ============================================================
# SECTION 2: FEATURE PREPARATION
# ============================================================

def prepare_features(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce types, fill NULLs, clip outliers."""
    df = df.copy()
    for col in FEATURE_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Fill NULLs
    null_fills = {
        "payment_ratio_overall": 1.0,
        "payment_ratio_3m":      1.0,
        "payment_ratio_6m":      1.0,
        "payment_ratio_min":     1.0,
        "payment_ratio_trend":   0.0,
        "instalments_since_last_late": 999.0,
        "instalment_amount_std": 0.0,
        "bureau_credit_util":    0.0,
        "balance_cv_90d":        0.0,
        "pd_score_at_origin":    0.5,
        "employed_years":        0.0,
    }
    for col, val in null_fills.items():
        if col in df.columns:
            df[col] = df[col].fillna(val)

    # Fill remaining NULLs with 0
    for col in FEATURE_COLS:
        if col in df.columns:
            df[col] = df[col].fillna(0)

    # Clip extreme values to prevent overflow
    clip_cols = {
        "instalments_since_last_late": (0, 999),
        "total_payment_shortfall":     (0, 1e7),
        "bureau_total_debt":           (0, 1e9),
        "bureau_total_limit":          (0, 1e9),
        "bureau_total_overdue":        (0, 1e8),
        "max_dpd_ever":                (0, 365),
        "instalment_amount_std":       (0, 1e6),
        "balance_cv_90d":              (0, 100),
        "npl_composite_score":         (0, 1),
    }
    for col, (lo, hi) in clip_cols.items():
        if col in df.columns:
            df[col] = df[col].clip(lo, hi)

    return df


def get_X_y(df: pd.DataFrame):
    """Extract feature matrix and label vector."""
    X = df[FEATURE_COLS].copy()
    y = df["npl_label"].values if "npl_label" in df.columns else None
    return X, y


# ============================================================
# SECTION 3: OUT-OF-TIME SPLIT
# ============================================================

def out_of_time_split(df: pd.DataFrame, test_frac: float = 0.20):
    """
    Split by ApplicationDate order (not random).
    Earlier applications = training, later = test.
    This simulates real deployment: train on past, validate on future.
    """
    df_sorted = df.sort_values("applicationdate").reset_index(drop=True)
    split_idx = int(len(df_sorted) * (1 - test_frac))
    train = df_sorted.iloc[:split_idx].copy()
    test  = df_sorted.iloc[split_idx:].copy()
    log.info(f"  Out-of-time split: train={len(train):,} test={len(test):,}")
    log.info(f"  Train period: {str(train['applicationdate'].min())} → "
             f"{str(train['applicationdate'].max())}")
    log.info(f"  Test period:  {str(test['applicationdate'].min())} → "
             f"{str(test['applicationdate'].max())}")
    log.info(f"  Train positive rate: {train['npl_label'].mean():.1%}")
    log.info(f"  Test positive rate:  {test['npl_label'].mean():.1%}")
    return train, test


# ============================================================
# SECTION 4: MODEL TRAINING
# ============================================================

def train_xgboost(X_train, y_train, X_val=None, y_val=None) -> xgb.XGBClassifier:
    """
    Train XGBoost with:
    - scale_pos_weight to handle 8.1% positive rate (ratio ≈ 11.4)
    - early stopping on validation AUC
    - tree_method='hist' for speed on 307k rows
    """
    n_pos = int(y_train.sum())
    n_neg = len(y_train) - n_pos
    spw   = round(n_neg / n_pos, 2)
    log.info(f"  scale_pos_weight = {spw}  "
             f"(neg={n_neg:,}  pos={n_pos:,})")

    model = xgb.XGBClassifier(
        n_estimators        = 1000,
        learning_rate       = 0.05,
        max_depth           = 6,
        min_child_weight    = 50,   # conservative — prevents overfitting on small DPD groups
        subsample           = 0.8,
        colsample_bytree    = 0.8,
        scale_pos_weight    = spw,
        tree_method         = "hist",
        eval_metric         = "auc",
        early_stopping_rounds = 50,
        random_state        = 42,
        n_jobs              = -1,
    )

    eval_set = [(X_train, y_train)]
    if X_val is not None and y_val is not None:
        eval_set.append((X_val, y_val))

    model.fit(
        X_train, y_train,
        eval_set   = eval_set,
        verbose    = 50,
    )
    log.info(f"  Best iteration: {model.best_iteration}")
    return model


# ============================================================
# SECTION 5: EVALUATION
# ============================================================

def evaluate_model(model, X_test, y_test, label="test") -> dict:
    """Compute AUC, Gini, KS, Brier, AP."""
    proba = model.predict_proba(X_test)[:, 1]

    auc    = roc_auc_score(y_test, proba)
    gini   = 2 * auc - 1
    brier  = brier_score_loss(y_test, proba)
    ap     = average_precision_score(y_test, proba)

    # KS statistic
    from scipy.stats import ks_2samp
    pos_scores = proba[y_test == 1]
    neg_scores = proba[y_test == 0]
    ks         = ks_2samp(pos_scores, neg_scores).statistic

    log.info(f"  [{label}] AUC={auc:.4f}  Gini={gini:.4f}  "
             f"KS={ks:.4f}  Brier={brier:.4f}  AP={ap:.4f}")

    # Tier distribution at default thresholds
    tiers = pd.cut(proba,
                   bins=[-np.inf, 0.15, 0.30, np.inf],
                   labels=["NORMAL", "WATCH", "CONCERN/CRITICAL"])
    log.info(f"  [{label}] NPL tier distribution:")
    for tier, count in tiers.value_counts().items():
        log.info(f"    {tier:<20} {count:>8,} ({count/len(proba):.1%})")

    return {"auc": auc, "gini": gini, "ks": ks,
            "brier": brier, "ap": ap}


# ============================================================
# SECTION 6: SHAP VALUES
# ============================================================

def compute_shap(model, X: pd.DataFrame,
                 sample_n: int = 50000) -> np.ndarray:
    """
    Compute SHAP values on a sample for speed.
    Returns array shape (n_samples, n_features).
    Falls back to XGBoost native predict() SHAP if shap library
    has version incompatibility with the XGBoost base_score format.
    """
    log.info(f"  Computing SHAP values (sample={min(sample_n, len(X)):,}) ...")
    t0 = time.perf_counter()
    if len(X) > sample_n:
        X_sample = X.sample(sample_n, random_state=42)
    else:
        X_sample = X

    try:
        # Try shap library first
        booster   = model.get_booster()
        explainer = shap.TreeExplainer(booster)
        shap_vals = explainer.shap_values(X_sample)
    except (ValueError, TypeError) as e:
        # Fallback: use XGBoost native SHAP via predict(pred_contribs=True)
        # This is always version-compatible — it uses the booster directly.
        log.warning(f"  shap library failed ({e.__class__.__name__}), "
                    f"using XGBoost native pred_contribs instead.")
        import xgboost as xgb
        dmat      = xgb.DMatrix(X_sample)
        booster   = model.get_booster()
        # pred_contribs returns (n_samples, n_features + 1) — last col is bias
        contribs  = booster.predict(dmat, pred_contribs=True)
        shap_vals = contribs[:, :-1]   # drop bias column

    log.info(f"  SHAP done in {time.perf_counter()-t0:.1f}s  "
             f"shape={shap_vals.shape}")
    return shap_vals, X_sample


def build_global_importance(shap_vals: np.ndarray,
                            feature_names: list) -> pd.DataFrame:
    """Mean |SHAP| per feature — global importance."""
    mean_abs = np.abs(shap_vals).mean(axis=0)
    df = (pd.DataFrame({"feature": feature_names,
                         "mean_abs_shap": mean_abs})
            .sort_values("mean_abs_shap", ascending=False)
            .reset_index(drop=True))
    df["rank"] = df.index + 1
    return df


def build_shap_rows(app_ids, client_ids, score_dates,
                    shap_vals, X_sample, run_id) -> list:
    """
    Build rows for ml.NPL_SHAP_Values.
    Returns list of tuples: (ApplicationID, ClientID, ScoreDate,
    FeatureName, FeatureValue, SHAPValue, AbsSHAPValue, SHAPRank,
    Direction, Horizon, ETL_RunID)
    """
    rows = []
    feat_names = list(X_sample.columns)
    for i in range(len(X_sample)):
        app_id    = int(app_ids.iloc[i])
        client_id = int(client_ids.iloc[i])
        vals      = shap_vals[i]
        feat_vals = X_sample.iloc[i].values
        # Get top N by absolute value
        top_idx = np.argsort(np.abs(vals))[::-1][:TOP_N_SHAP]
        for rank, idx in enumerate(top_idx, start=1):
            rows.append((
                app_id,
                client_id,
                score_dates.iloc[i] if hasattr(score_dates, "iloc") else score_dates,
                feat_names[idx],
                float(feat_vals[idx]),
                float(vals[idx]),
                float(abs(vals[idx])),
                rank,
                "+" if vals[idx] > 0 else "-",
                90,   # horizon days
                run_id,
            ))
    return rows


# ============================================================
# SECTION 7: ARTIFACT SAVING / LOADING
# ============================================================

def save_artifacts(model, feature_cols, metrics_test, metrics_oot=None):
    log.info("Saving NPL artifacts ...")
    joblib.dump(model, ARTIFACTS / "npl_model.pkl")
    with open(ARTIFACTS / "npl_feature_cols.json", "w") as f:
        json.dump(feature_cols, f)
    config = {
        "model_version":   MODEL_VERSION,
        "feature_cols":    feature_cols,
        "top_n_shap":      TOP_N_SHAP,
        "trained_on":      pd.Timestamp.today().strftime("%Y-%m-%d"),
        "metrics_test":    metrics_test,
        "metrics_oot":     metrics_oot or {},
        "n_features":      len(feature_cols),
        "label":           "LoanApplications.TARGET",
        "split":           "out-of-time 80/20 by ApplicationDate",
    }
    with open(ARTIFACTS / "npl_config.json", "w") as f:
        json.dump(config, f, indent=2)
    log.info(f"  Artifacts saved to {ARTIFACTS}/")


def load_artifacts() -> dict:
    log.info("Loading NPL artifacts ...")
    with open(ARTIFACTS / "npl_config.json") as f:
        config = json.load(f)
    model = joblib.load(ARTIFACTS / "npl_model.pkl")
    with open(ARTIFACTS / "npl_feature_cols.json") as f:
        feature_cols = json.load(f)
    log.info(f"  Model: {config['model_version']}  "
             f"AUC={config['metrics_test'].get('auc', 'N/A')}")
    return {"model": model, "feature_cols": feature_cols, "config": config}


# ============================================================
# SECTION 8: NPL TIER ASSIGNMENT
# ============================================================

# def assign_npl_tier(score: float) -> str:
#     """
#     NORMAL   < 0.15  — no elevated risk
#     WATCH    0.15–0.30 — monitor
#     CONCERN  0.30–0.50 — escalate
#     CRITICAL >= 0.50  — immediate action
#     """
#     if score >= 0.50: return "CRITICAL"
#     if score >= 0.30: return "CONCERN"
#     if score >= 0.15: return "WATCH"
#     return "NORMAL"


def assign_npl_tier(score: float,
                    p10: float, p40: float, p75: float) -> str:
    """
    Percentile-based boundaries computed from scored population each run.
    NORMAL   <= p10  — no elevated risk
    WATCH    p10–p40 — monitor
    CONCERN  p40–p75 — escalate
    CRITICAL > p75   — immediate action
    """
    if score >  p75: return "CRITICAL"
    if score >  p40: return "CONCERN"
    if score >  p10: return "WATCH"
    return "NORMAL"


# ============================================================
# SECTION 9: DB WRITE FUNCTIONS
# ============================================================

def log_etl_step(conn, step, status, rows=None, error=None, run_id=None):
    with conn.cursor() as cur:
        if run_id is None:
            cur.execute("""
                INSERT INTO stg.ETL_RunLog
                    (RunDate, StepName, Status, StartedAt)
                VALUES (CURRENT_DATE, %s, %s, NOW())
                RETURNING RunID
            """, (step, status))
            run_id = cur.fetchone()[0]
        else:
            cur.execute("""
                UPDATE stg.ETL_RunLog
                SET Status=%s, RowsProcessed=%s, ErrorMessage=%s,
                    FinishedAt=NOW(),
                    DurationSec=EXTRACT(EPOCH FROM (NOW()-StartedAt))::INT
                WHERE RunID=%s
            """, (status, rows, error, run_id))
    conn.commit()
    return run_id


def ensure_model_version(conn):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ml.Models (ModelVersion, MLflowRunID, Notes)
            VALUES (%s, 'local', 'XGBoost NPL early warning — TARGET label')
            ON CONFLICT (ModelVersion) DO NOTHING
        """, (MODEL_VERSION,))
    conn.commit()


def ensure_npl_tables(conn):
    """Create ml.NPL_Scores and ml.NPL_SHAP_Values if not yet created."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ml.NPL_Scores (
                NPLScoreID       BIGINT      NOT NULL GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                ApplicationID    BIGINT      NOT NULL,
                ClientID         BIGINT      NOT NULL,
                ScoreDate        DATE        NOT NULL DEFAULT CURRENT_DATE,
                NPL_Score        DECIMAL(9,7) NOT NULL,
                NPL_Risk_Tier    VARCHAR(10) NOT NULL,
                PD_Score_Current DECIMAL(9,7) NULL,
                ModelVersion     VARCHAR(30) NOT NULL,
                ETL_RunID        BIGINT      NOT NULL,
                CreatedAt        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT UQ_NPL_AppDate   UNIQUE (ApplicationID, ScoreDate, ModelVersion),
                CONSTRAINT CK_NPL_Tier      CHECK (NPL_Risk_Tier IN
                    ('NORMAL','WATCH','CONCERN','CRITICAL')),
                CONSTRAINT FK_NPL_App       FOREIGN KEY (ApplicationID)
                    REFERENCES public.LoanApplications(ApplicationID),
                CONSTRAINT FK_NPL_Model     FOREIGN KEY (ModelVersion)
                    REFERENCES ml.Models(ModelVersion),
                CONSTRAINT FK_NPL_RunLog    FOREIGN KEY (ETL_RunID)
                    REFERENCES stg.ETL_RunLog(RunID)
            );
            CREATE INDEX IF NOT EXISTS IX_NPL_ClientDate
                ON ml.NPL_Scores (ClientID, ScoreDate DESC);
            CREATE INDEX IF NOT EXISTS IX_NPL_Tier
                ON ml.NPL_Scores (ScoreDate, NPL_Risk_Tier)
                WHERE NPL_Risk_Tier != 'NORMAL';

            CREATE TABLE IF NOT EXISTS ml.NPL_SHAP_Values (
                NPLSHAPRecordID  BIGINT      NOT NULL GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                ApplicationID    BIGINT      NOT NULL,
                ClientID         BIGINT      NOT NULL,
                ScoreDate        DATE        NOT NULL,
                FeatureName      VARCHAR(60) NOT NULL,
                FeatureValue     DECIMAL(18,6) NULL,
                SHAPValue        DECIMAL(12,6) NOT NULL,
                AbsSHAPValue     DECIMAL(12,6) NOT NULL,
                SHAPRank         SMALLINT    NOT NULL,
                Direction        CHAR(1)     NOT NULL,
                Horizon          SMALLINT    NOT NULL DEFAULT 90,
                ETL_RunID        BIGINT      NOT NULL,
                CreatedAt        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT CK_NPL_SHAP_Rank  CHECK (SHAPRank BETWEEN 1 AND 5),
                CONSTRAINT CK_NPL_SHAP_Dir   CHECK (Direction IN ('+','-')),
                CONSTRAINT FK_NPL_SHAP_RunLog FOREIGN KEY (ETL_RunID)
                    REFERENCES stg.ETL_RunLog(RunID)
            );
            CREATE INDEX IF NOT EXISTS IX_NPL_SHAP_AppDate
                ON ml.NPL_SHAP_Values (ApplicationID, ScoreDate);
        """)
    conn.commit()
    log.info("  NPL tables verified/created.")


def write_npl_scores(conn, records: list):
    sql = """
        INSERT INTO ml.NPL_Scores
            (ApplicationID, ClientID, ScoreDate, NPL_Score,
             NPL_Risk_Tier, PD_Score_Current, ModelVersion, ETL_RunID)
        VALUES %s
        ON CONFLICT (ApplicationID, ScoreDate, ModelVersion) DO UPDATE SET
            NPL_Score        = EXCLUDED.NPL_Score,
            NPL_Risk_Tier    = EXCLUDED.NPL_Risk_Tier,
            PD_Score_Current = EXCLUDED.PD_Score_Current,
            ETL_RunID        = EXCLUDED.ETL_RunID
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, records, page_size=5000)
    conn.commit()


def write_npl_shap(conn, rows: list):
    sql = """
        INSERT INTO ml.NPL_SHAP_Values
            (ApplicationID, ClientID, ScoreDate, FeatureName, FeatureValue,
             SHAPValue, AbsSHAPValue, SHAPRank, Direction, Horizon, ETL_RunID)
        VALUES %s
        ON CONFLICT (ApplicationID, ScoreDate, FeatureName, Horizon) DO UPDATE SET
            ClientID      = EXCLUDED.ClientID,
            FeatureValue  = EXCLUDED.FeatureValue,
            SHAPValue     = EXCLUDED.SHAPValue,
            AbsSHAPValue  = EXCLUDED.AbsSHAPValue,
            SHAPRank      = EXCLUDED.SHAPRank,
            Direction     = EXCLUDED.Direction,
            ETL_RunID     = EXCLUDED.ETL_RunID
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=5000)
    conn.commit()


# ============================================================
# SECTION 10: SCORE PIPELINE (monthly)
# ============================================================

def run_score(arts: dict):
    """
    Load model → score all applications → write ml.NPL_Scores + SHAP.
    Runs monthly after PD scoring completes.
    """
    conn   = get_conn()
    run_id = log_etl_step(conn, "npl_scoring", "running")
    try:
        ensure_model_version(conn)
        ensure_npl_tables(conn)

        model        = arts["model"]
        feature_cols = arts["feature_cols"]
        today        = pd.Timestamp.today().date()

        # Load scoring data
        df = load_scoring_data(conn)
        df = prepare_features(df)
        X  = df[feature_cols]

        # Score
        log.info("Scoring all applications ...")
        t0    = time.perf_counter()
        proba = model.predict_proba(X)[:, 1]
        log.info(f"  Scored {len(df):,} applications in "
                 f"{time.perf_counter()-t0:.1f}s")
        log.info(f"  Mean NPL score: {proba.mean():.4f}")

        # Compute percentile boundaries from this run's score distribution
        p10 = float(np.percentile(proba, 10))
        p40 = float(np.percentile(proba, 40))
        p75 = float(np.percentile(proba, 75))
        log.info(f"  Tier boundaries — p10={p10:.4f}  p40={p40:.4f}  p75={p75:.4f}")

        # Build score records
        score_records = []
        for i in range(len(df)):
            score = float(proba[i])
            score_records.append((
                int(df["applicationid"].iloc[i]),
                int(df["clientid"].iloc[i]),
                today,
                round(score, 7),
                assign_npl_tier(score, p10, p40, p75),
                float(df["pd_score_at_origin"].iloc[i]),
                MODEL_VERSION,
                run_id,
            ))

        log.info(f"  Writing {len(score_records):,} NPL score rows ...")
        write_npl_scores(conn, score_records)

        # Tier summary
        tiers = [assign_npl_tier(float(p), p10, p40, p75) for p in proba]
        for tier in ["NORMAL", "WATCH", "CONCERN", "CRITICAL"]:
            n = tiers.count(tier)
            log.info(f"  {tier:<10} {n:>8,} ({n/len(tiers):.1%})")

        # SHAP on full dataset
        log.info("Computing SHAP values ...")
        shap_vals, X_sample = compute_shap(model, X, sample_n=50000)
        shap_rows = build_shap_rows(
            df["applicationid"].iloc[:len(X_sample)],
            df["clientid"].iloc[:len(X_sample)],
            pd.Series([today] * len(X_sample)),
            shap_vals, X_sample, run_id
        )
        log.info(f"  Writing {len(shap_rows):,} SHAP rows ...")
        write_npl_shap(conn, shap_rows)

        log_etl_step(conn, "npl_scoring", "success",
                     rows=len(score_records), run_id=run_id)
        log.info("NPL scoring complete.")

    except Exception as e:
        log_etl_step(conn, "npl_scoring", "failed",
                     error=str(e)[:2000], run_id=run_id)
        raise
    finally:
        conn.close()


# ============================================================
# SECTION 11: TRAINING PIPELINE
# ============================================================

def train():
    log.info("=" * 60)
    log.info("NPL TRAINING PIPELINE")
    log.info("=" * 60)

    conn = get_conn()
    df   = load_training_data(conn)
    conn.close()

    # Prepare features
    df = prepare_features(df)

    # Out-of-time split
    log.info("Out-of-time split ...")
    train_df, test_df = out_of_time_split(df, test_frac=0.20)

    X_train, y_train = get_X_y(train_df)
    X_test,  y_test  = get_X_y(test_df)

    # Train
    log.info("Training XGBoost ...")
    model = train_xgboost(X_train, y_train, X_test, y_test)

    # Evaluate
    log.info("Evaluating ...")
    metrics_train = evaluate_model(model, X_train, y_train, "train")
    metrics_test  = evaluate_model(model, X_test,  y_test,  "OOT test")

    # Feature importance
    log.info("Top 15 features by XGBoost gain:")
    imp = (pd.Series(model.feature_importances_,
                     index=FEATURE_COLS)
             .sort_values(ascending=False)
             .head(15))
    for feat, score in imp.items():
        bar = "█" * int(score * 400)
        log.info(f"  {feat:<35} {score:.4f}  {bar}")

    # SHAP global importance on test set
    log.info("SHAP global importance (test set) ...")
    shap_vals, X_sample = compute_shap(model, X_test, sample_n=20000)
    shap_imp = build_global_importance(shap_vals, FEATURE_COLS)
    log.info("  Top 10 by mean |SHAP|:")
    for _, row in shap_imp.head(10).iterrows():
        bar = "█" * int(row["mean_abs_shap"] * 200)
        log.info(f"  {int(row['rank']):>2}. {row['feature']:<35} "
                 f"{row['mean_abs_shap']:.4f}  {bar}")

    # Save
    save_artifacts(model, FEATURE_COLS, metrics_test, metrics_train)

    log.info("=" * 60)
    log.info("TRAINING COMPLETE")
    log.info(f"  OOT AUC  = {metrics_test['auc']:.4f}")
    log.info(f"  OOT Gini = {metrics_test['gini']:.4f}")
    log.info(f"  OOT KS   = {metrics_test['ks']:.4f}")
    log.info("=" * 60)


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NPL early-warning pipeline")
    parser.add_argument("--mode",
                        choices=["train", "score", "eval"],
                        default="train")
    args = parser.parse_args()

    if args.mode == "train":
        train()

    elif args.mode == "score":
        arts = load_artifacts()
        run_score(arts)

    elif args.mode == "eval":
        arts  = load_artifacts()
        conn  = get_conn()
        df    = load_training_data(conn)
        conn.close()
        df    = prepare_features(df)
        _, test_df = out_of_time_split(df, test_frac=0.20)
        X_test, y_test = get_X_y(test_df)
        evaluate_model(arts["model"], X_test, y_test, "OOT test")

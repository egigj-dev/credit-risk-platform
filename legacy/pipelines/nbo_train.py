# ============================================================
# nbo_train.py
# NBO recommender — adapted from nbpp_A_hnsw_cf_rules.ipynb
# Trains on real DB data (307,511 clients), saves artifacts,
# evaluates on holdout, and exposes run_daily() for nbo_etl.
#
# Merged from: product_ownership.sql (ownership derivation logic).
#   Table DDL for public.NBO_ClientOwnership lives in credit_risk_ddl_pg.sql.
#   The INSERT/ON CONFLICT derivation block is now refresh_ownership() here.
#
# Usage:
#   python nbo_train.py --mode ownership  # monthly: derive product ownership flags
#   python nbo_train.py --mode train      # one-time: fit model + save artifacts
#   python nbo_train.py --mode daily      # nightly: score all clients → DB
#   python nbo_train.py --mode eval       # ad-hoc: re-evaluate on holdout
# ============================================================

import argparse, json, logging, os, sys, time
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import MinMaxScaler
from sklearn.decomposition import PCA
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
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
ARTIFACTS = Path("nbo_artifacts")
ARTIFACTS.mkdir(exist_ok=True)

# ── Products (must match public.Products seed order) ──────────
PRODUCTS = [
    "checking_account", "savings_account", "debit_card", "credit_card",
    "overdraft_facility", "personal_loan", "revolving_loan", "mortgage",
    "auto_loan", "term_deposit", "investment_fund",
    "life_insurance", "travel_insurance",
]
PRODUCT_LABELS = {p: p.replace("_", " ").title() for p in PRODUCTS}
EASY_PRODUCTS  = {"checking_account", "debit_card"}
HARD_PRODUCTS  = {"travel_insurance", "life_insurance", "auto_loan", "mortgage"}

# ── Model hyperparameters (from notebook, keep identical) ─────
SIM_THRESHOLD  = 0.78
MAX_NEIGHBOURS = 200
TOP_K          = 5
RFM_QUANTILES  = 4
KMEANS_K       = 8
RANDOM_SEED    = 42

EMP_CATEGORIES = ["employed", "self_employed", "retired", "student", "unemployed"]
CITY_TIERS     = ["tier_1", "tier_2", "tier_3"]

RFM_ALPHA = {"Champions": 0.85, "Loyal": 0.78, "Potential": 0.70,
             "At Risk": 0.55, "Lost": 0.50}
RFM_BETA  = {"Champions": 0.15, "Loyal": 0.22, "Potential": 0.30,
             "At Risk": 0.45, "Lost": 0.50}

# Feature blocks (must match notebook order exactly for scaler compatibility)
SPENDING_FEAT  = ["sp_groceries", "sp_dining", "sp_travel", "sp_utilities",
                  "sp_healthcare", "sp_education", "sp_entertainment", "sp_shopping"]
DEMO_FEAT      = ["annual_income", "credit_score", "years_as_customer"]
RFM_FEAT       = ["r_score", "f_score", "m_score"]
CHANNEL_FEAT   = ["atm_withdrawals_mo", "avg_atm_withdrawal", "branch_visits_mo",
                  "online_banking_days", "mobile_app_sessions"]
CASH_FEAT      = ["atm_cash_monthly", "estimated_total_spend", "card_coverage_ratio"]
FINANCIAL_FEAT = ["credit_util", "savings_rate", "debt_to_income", "late_payments",
                  "overdraft_cnt", "balance_trend"]
LIFE_FEAT      = ["life_marriage", "life_home", "life_job_change",
                  "life_retirement", "life_relocation"]
NUM_COLS       = (SPENDING_FEAT + DEMO_FEAT + RFM_FEAT + CHANNEL_FEAT +
                  CASH_FEAT + FINANCIAL_FEAT + LIFE_FEAT)


# ============================================================
# SECTION 1: PRODUCT OWNERSHIP DERIVATION
# Merged from product_ownership.sql.
# Table DDL for public.NBO_ClientOwnership is in credit_risk_ddl_pg.sql.
#
# Derivation logic for each of the 13 products:
#   has_checking      = has any Transactions OR any LoanApplication
#   has_savings       = salary_credits >= 2 AND avg_balance > 1000
#   has_debit_card    = has LoanApplication OR ATM/POS card transactions
#   has_credit_card   = active bureau credit line + ExtScore2 > 0.40
#   has_overdraft     = any OVERDRAFT transaction
#   has_personal_loan = ContractType = 'Cash loans' (direct mapping)
#   has_revolving_loan = ContractType = 'Revolving loans' (direct mapping)
#   has_mortgage      = approved previous credit > 50K
#   has_auto_loan     = OwnCar = TRUE + any credit history
#   has_term_deposit  = ClosedLoans >= 2 + income > 30K + low credit util
#   has_investment    = income > 50K + ExtScore2 > 0.65 + ClosedLoans >= 2
#   has_life_insurance = Married/Civil marriage + CNTChildren > 0
#   has_travel_insurance = income > 40K + online_txns >= 5
#
# NOTE: ownership is derived from proxy fields — not from real
# product ownership data. has_personal_loan (Cash loans) and
# has_revolving_loan (Revolving loans) are the only direct mappings.
# All others are inferred signals. Hit@5 = 0.982 reflects this simplicity.
# ============================================================

_OWNERSHIP_SQL = """
INSERT INTO public.NBO_ClientOwnership (
    ClientID, SnapshotMonth,
    has_checking, has_savings, has_debit_card, has_credit_card,
    has_overdraft, has_personal_loan, has_revolving_loan, has_mortgage,
    has_auto_loan, has_term_deposit, has_investment,
    has_life_insurance, has_travel_insurance,
    n_products, cold_start, ETL_RunID
)
WITH

txn_signals AS (
    SELECT
        ClientID,
        COUNT(*)                                                    AS txn_count,
        SUM(CASE WHEN TxnType = 'SALARY'    THEN 1 ELSE 0 END)     AS salary_credits,
        SUM(CASE WHEN TxnType = 'OVERDRAFT' THEN 1 ELSE 0 END)     AS overdraft_txns,
        SUM(CASE WHEN Channel IN ('ATM','POS') THEN 1 ELSE 0 END)  AS card_txns,
        SUM(CASE WHEN Channel = 'ONLINE'    THEN 1 ELSE 0 END)     AS online_txns,
        AVG(Balance)                                                AS avg_balance
    FROM public.Transactions
    WHERE TxnDate >= CURRENT_DATE - INTERVAL '90 days'
    GROUP BY ClientID
),

bureau_signals AS (
    SELECT
        ClientID,
        SUM(CASE WHEN CreditActive = 'Active'   THEN 1 ELSE 0 END) AS active_credit_lines,
        SUM(CASE WHEN CreditActive = 'Closed'   THEN 1 ELSE 0 END) AS closed_credit_lines,
        SUM(CASE WHEN CreditActive = 'Bad debt' THEN 1 ELSE 0 END) AS bad_debt_lines,
        SUM(COALESCE(AMT_CreditSum,     0))                         AS total_credit_limit,
        SUM(COALESCE(AMT_CreditSumDebt, 0))                         AS total_credit_debt
    FROM public.BureauLoans
    GROUP BY ClientID
),

prev_signals AS (
    SELECT
        ClientID,
        MAX(CASE WHEN ContractStatus = 'Approved'
                  AND COALESCE(AMT_Credit, 0) > 50000
                 THEN 1 ELSE 0 END)                                 AS approved_large_credit
    FROM public.PreviousApplications
    GROUP BY ClientID
),

latest_bureau AS (
    SELECT DISTINCT ON (ClientID)
        ClientID, ExtScore2, ActiveLoans, ClosedLoans
    FROM public.BureauSnapshots
    ORDER BY ClientID, SnapshotDate DESC
),

base AS (
    SELECT
        c.ClientID,
        DATE_TRUNC('month', CURRENT_DATE)::DATE                     AS SnapshotMonth,

        CASE WHEN la.ApplicationID IS NOT NULL
              OR t.txn_count > 0 THEN 1 ELSE 0 END                  AS has_checking,

        CASE WHEN t.salary_credits >= 2
              AND t.avg_balance > 1000 THEN 1 ELSE 0 END            AS has_savings,

        CASE WHEN la.ApplicationID IS NOT NULL
              OR t.card_txns > 0 THEN 1 ELSE 0 END                  AS has_debit_card,

        CASE WHEN b.active_credit_lines > 0
              AND COALESCE(lb.ExtScore2, 0) > 0.40 THEN 1 ELSE 0 END AS has_credit_card,

        CASE WHEN COALESCE(t.overdraft_txns, 0) > 0 THEN 1 ELSE 0 END AS has_overdraft,

        CASE WHEN la.ContractType = 'Cash loans' THEN 1 ELSE 0 END  AS has_personal_loan,

        CASE WHEN la.ContractType = 'Revolving loans' THEN 1 ELSE 0 END AS has_revolving_loan,

        CASE WHEN COALESCE(p.approved_large_credit, 0) = 1 THEN 1 ELSE 0 END AS has_mortgage,

        CASE WHEN c.OwnCar = TRUE
              AND COALESCE(b.total_credit_limit, 0) > 0 THEN 1 ELSE 0 END AS has_auto_loan,

        CASE WHEN COALESCE(lb.ClosedLoans, 0) >= 2
              AND c.IncomeTotal > 30000
              AND COALESCE(b.total_credit_debt, 0) /
                  NULLIF(b.total_credit_limit, 0) < 0.30 THEN 1 ELSE 0 END AS has_term_deposit,

        CASE WHEN c.IncomeTotal > 50000
              AND COALESCE(lb.ExtScore2, 0) > 0.65
              AND COALESCE(lb.ClosedLoans, 0) >= 2 THEN 1 ELSE 0 END AS has_investment,

        CASE WHEN c.FamilyStatus IN ('Married', 'Civil marriage')
              AND c.CNTChildren > 0 THEN 1 ELSE 0 END               AS has_life_insurance,

        CASE WHEN c.IncomeTotal > 40000
              AND COALESCE(t.online_txns, 0) >= 5 THEN 1 ELSE 0 END AS has_travel_insurance

    FROM public.Clients c
    JOIN public.LoanApplications la ON la.ClientID = c.ClientID
    LEFT JOIN txn_signals    t  ON t.ClientID  = c.ClientID
    LEFT JOIN bureau_signals b  ON b.ClientID  = c.ClientID
    LEFT JOIN latest_bureau  lb ON lb.ClientID = c.ClientID
    LEFT JOIN prev_signals   p  ON p.ClientID  = c.ClientID
)

SELECT
    ClientID, SnapshotMonth,
    has_checking, has_savings, has_debit_card, has_credit_card,
    has_overdraft, has_personal_loan, has_revolving_loan, has_mortgage,
    has_auto_loan, has_term_deposit, has_investment,
    has_life_insurance, has_travel_insurance,
    (has_checking + has_savings + has_debit_card + has_credit_card +
     has_overdraft + has_personal_loan + has_revolving_loan + has_mortgage +
     has_auto_loan + has_term_deposit + has_investment +
     has_life_insurance + has_travel_insurance)                      AS n_products,
    CASE WHEN (has_personal_loan + has_revolving_loan + has_mortgage +
               has_auto_loan + has_term_deposit + has_investment +
               has_credit_card + has_overdraft + has_savings +
               has_life_insurance + has_travel_insurance) < 2
         THEN TRUE ELSE FALSE END                                    AS cold_start,
    %(run_id)s::BIGINT                                               AS ETL_RunID
FROM base

ON CONFLICT (ClientID, SnapshotMonth) DO UPDATE SET
    has_checking         = EXCLUDED.has_checking,
    has_savings          = EXCLUDED.has_savings,
    has_debit_card       = EXCLUDED.has_debit_card,
    has_credit_card      = EXCLUDED.has_credit_card,
    has_overdraft        = EXCLUDED.has_overdraft,
    has_personal_loan    = EXCLUDED.has_personal_loan,
    has_revolving_loan   = EXCLUDED.has_revolving_loan,
    has_mortgage         = EXCLUDED.has_mortgage,
    has_auto_loan        = EXCLUDED.has_auto_loan,
    has_term_deposit     = EXCLUDED.has_term_deposit,
    has_investment       = EXCLUDED.has_investment,
    has_life_insurance   = EXCLUDED.has_life_insurance,
    has_travel_insurance = EXCLUDED.has_travel_insurance,
    n_products           = EXCLUDED.n_products,
    cold_start           = EXCLUDED.cold_start,
    ETL_RunID            = EXCLUDED.ETL_RunID,
    CreatedAt            = NOW()
"""

_OWNERSHIP_VERIFY_SQL = """
SELECT
    ROUND(AVG(n_products), 2)                               AS avg_products,
    SUM(has_personal_loan)                                  AS has_loan,
    SUM(has_checking)                                       AS has_checking,
    SUM(has_savings)                                        AS has_savings,
    SUM(has_credit_card)                                    AS has_cc,
    SUM(has_mortgage)                                       AS has_mortgage,
    SUM(has_investment)                                     AS has_investment,
    SUM(CASE WHEN cold_start THEN 1 ELSE 0 END)             AS cold_start_count,
    COUNT(*)                                                AS total_clients
FROM public.NBO_ClientOwnership
WHERE SnapshotMonth = DATE_TRUNC('month', CURRENT_DATE)::DATE
"""


def refresh_ownership():
    """
    Derives binary product ownership for all 13 NBO products and upserts into
    public.NBO_ClientOwnership for the current snapshot month.

    Run monthly — before nbo_train.py --mode train and before --mode daily
    on the monthly index-rebuild run. Nightly --mode daily runs can reuse
    the snapshot from the start of the month.
    """
    log.info("=" * 60)
    log.info("NBO OWNERSHIP REFRESH")
    log.info("=" * 60)
    conn   = get_conn()
    run_id = None
    try:
        # Log ETL step
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO stg.ETL_RunLog (RunDate, StepName, Status, StartedAt)
                VALUES (CURRENT_DATE, 'nbo_ownership', 'running', NOW())
                RETURNING RunID
            """)
            run_id = cur.fetchone()[0]
        conn.commit()

        t0 = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(_OWNERSHIP_SQL, {"run_id": run_id})
            rows_affected = cur.rowcount
        conn.commit()
        elapsed = time.perf_counter() - t0

        log.info(f"  Upserted {rows_affected:,} ownership rows in {elapsed:.1f}s")

        # Verify distribution
        with conn.cursor() as cur:
            cur.execute(_OWNERSHIP_VERIFY_SQL)
            cols = [d[0] for d in cur.description]
            row  = cur.fetchone()
        if row:
            stats = dict(zip(cols, row))
            log.info(f"  Total clients:  {stats['total_clients']:,}")
            log.info(f"  Avg products:   {stats['avg_products']}")
            log.info(f"  Cold-start:     {stats['cold_start_count']:,}")
            log.info(f"  Has checking:   {stats['has_checking']:,}")
            log.info(f"  Has loan:       {stats['has_loan']:,}")
            log.info(f"  Has savings:    {stats['has_savings']:,}")
            log.info(f"  Has credit card:{stats['has_cc']:,}")
            log.info(f"  Has mortgage:   {stats['has_mortgage']:,}")
            log.info(f"  Has investment: {stats['has_investment']:,}")

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE stg.ETL_RunLog
                SET Status='success', RowsProcessed=%s,
                    DurationSec=EXTRACT(EPOCH FROM (NOW()-StartedAt))::INT,
                    FinishedAt=NOW()
                WHERE RunID=%s
            """, (rows_affected, run_id))
        conn.commit()
        log.info("Ownership refresh complete.")

    except Exception as e:
        log.error(f"Ownership refresh failed: {e}")
        if run_id:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE stg.ETL_RunLog
                    SET Status='failed', ErrorMessage=%s, FinishedAt=NOW()
                    WHERE RunID=%s
                """, (str(e)[:2000], run_id))
            conn.commit()
        raise
    finally:
        conn.close()


# ============================================================
# SECTION 2: DATA LOADING FROM DB
# Replaces Section 3 (synthetic data generation) of notebook.
# ============================================================

CLIENT_SQL = """
SELECT
    c.ClientID                                              AS customer_id,

    -- ── Demographics ────────────────────────────────────────
    c.IncomeTotal::FLOAT                                    AS annual_income,
    -- ExtScore2 is 0-1; scale to 280-800 to match notebook credit_score range
    LEAST(GREATEST(
        COALESCE(lb.ExtScore2 * 850, 500),
    280), 800)                                              AS credit_score,
    LEAST(ABS(COALESCE(c.DAYS_REGISTRATION, 0)) / 365.25,
          40)                                               AS years_as_customer,

    -- ── Employment → notebook categories ────────────────────
    CASE c.IncomeType
        WHEN 'Working'              THEN 'employed'
        WHEN 'Commercial associate' THEN 'employed'
        WHEN 'State servant'        THEN 'employed'
        WHEN 'Self-employed'        THEN 'self_employed'
        WHEN 'Pensioner'            THEN 'retired'
        WHEN 'Student'              THEN 'student'
        ELSE 'unemployed'
    END                                                     AS employment_status,

    -- ── City tier from RegionRating ──────────────────────────
    CASE c.RegionRating
        WHEN 1 THEN 'tier_1'
        WHEN 2 THEN 'tier_2'
        ELSE        'tier_3'
    END                                                     AS city_tier,

    -- ── Spending category proxies (income-scaled) ────────────
    -- No merchant categories in DB — use income-scaled estimates.
    -- Ratios calibrated to Albanian banking averages.
    GREATEST(c.IncomeTotal * 0.15 / 12, 0)                 AS sp_groceries,
    GREATEST(c.IncomeTotal * 0.05 / 12, 0)                 AS sp_dining,
    GREATEST(c.IncomeTotal * 0.03 / 12, 0)                 AS sp_travel,
    GREATEST(c.IncomeTotal * 0.08 / 12, 0)                 AS sp_utilities,
    GREATEST(c.IncomeTotal * 0.04 / 12, 0)                 AS sp_healthcare,
    GREATEST(c.IncomeTotal * 0.02 / 12, 0)                 AS sp_education,
    GREATEST(c.IncomeTotal * 0.03 / 12, 0)                 AS sp_entertainment,
    GREATEST(c.IncomeTotal * 0.05 / 12, 0)                 AS sp_shopping,

    -- ── Transaction signals (90-day window) ─────────────────
    COALESCE(t.txn_count, 0)::FLOAT                         AS txn_frequency,
    COALESCE(t.last_txn_days, 365)::FLOAT                   AS last_txn_days,
    COALESCE(t.atm_count_mo, 0)::FLOAT                      AS atm_withdrawals_mo,
    GREATEST(COALESCE(t.avg_balance, 0) / 4.0, 0)::FLOAT   AS avg_atm_withdrawal,
    0::FLOAT                                                AS branch_visits_mo,
    COALESCE(t.online_count, 0)::FLOAT                      AS online_banking_days,
    0::FLOAT                                                AS mobile_app_sessions,
    COALESCE(t.atm_cash_monthly, 0)::FLOAT                  AS atm_cash_monthly,
    COALESCE(t.estimated_spend, c.IncomeTotal/12)::FLOAT    AS estimated_total_spend,
    COALESCE(t.card_coverage, 0.5)::FLOAT                   AS card_coverage_ratio,

    -- ── Financial health ─────────────────────────────────────
    COALESCE(b.credit_util, 0)::FLOAT                       AS credit_util,
    LEAST(GREATEST(
        COALESCE(t.avg_balance, 0) / NULLIF(c.IncomeTotal / 12, 0),
        0), 1)::FLOAT                                       AS savings_rate,
    COALESCE(b.debt_to_income, 0)::FLOAT                    AS debt_to_income,
    COALESCE(b.bad_debt_count, 0)::FLOAT                    AS late_payments,
    COALESCE(t.overdraft_count, 0)::FLOAT                   AS overdraft_cnt,
    COALESCE(t.balance_trend, 0)::FLOAT                     AS balance_trend,

    -- ── Life events ──────────────────────────────────────────
    CASE WHEN c.FamilyStatus IN ('Married', 'Civil marriage')
         THEN 1 ELSE 0 END::FLOAT                           AS life_marriage,
    CASE WHEN c.OwnRealty = TRUE THEN 1 ELSE 0 END::FLOAT   AS life_home,
    CASE WHEN ABS(COALESCE(c.Days_Employed, -9999)) < 365
          AND c.Days_Employed < 0
         THEN 1 ELSE 0 END::FLOAT                           AS life_job_change,
    CASE WHEN DATE_PART('year', AGE(c.BirthDate)) > 60
         THEN 1 ELSE 0 END::FLOAT                           AS life_retirement,
    0::FLOAT                                                AS life_relocation,

    -- ── Product ownership (from NBO_ClientOwnership) ─────────
    COALESCE(o.has_checking, 0)                             AS checking_account,
    COALESCE(o.has_savings, 0)                              AS savings_account,
    COALESCE(o.has_debit_card, 0)                           AS debit_card,
    COALESCE(o.has_credit_card, 0)                          AS credit_card,
    COALESCE(o.has_overdraft, 0)                            AS overdraft_facility,
    COALESCE(o.has_personal_loan, 0)                        AS personal_loan,
    COALESCE(o.has_revolving_loan, 0)                       AS revolving_loan,
    COALESCE(o.has_mortgage, 0)                             AS mortgage,
    COALESCE(o.has_auto_loan, 0)                            AS auto_loan,
    COALESCE(o.has_term_deposit, 0)                         AS term_deposit,
    COALESCE(o.has_investment, 0)                           AS investment_fund,
    COALESCE(o.has_life_insurance, 0)                       AS life_insurance,
    COALESCE(o.has_travel_insurance, 0)                     AS travel_insurance,
    COALESCE(o.cold_start, TRUE)                            AS is_cold_start,

    -- ── PD gate (from latest PD score) ──────────────────────
    COALESCE(pd.PD_Score, 0.5)::FLOAT                       AS pd_score,
    COALESCE(pd.RiskTier, 'HIGH')                           AS pd_risk_tier,

    -- NPL gate: clients needing servicing support must not receive cross-sell offers.
    COALESCE(npl.NPL_Score, 0.5)::FLOAT                     AS npl_score,
    COALESCE(npl.NPL_Risk_Tier, 'CRITICAL')                 AS npl_risk_tier

FROM public.Clients c

LEFT JOIN LATERAL (
    SELECT *
    FROM public.NBO_ClientOwnership
    WHERE ClientID = c.ClientID
    ORDER BY SnapshotMonth DESC LIMIT 1
) o ON TRUE

LEFT JOIN LATERAL (
    SELECT PD_Score, RiskTier
    FROM ml.PD_Scores
    WHERE ClientID = c.ClientID
    ORDER BY ScoreDate DESC LIMIT 1
) pd ON TRUE

LEFT JOIN LATERAL (
    SELECT NPL_Score, NPL_Risk_Tier
    FROM ml.NPL_Scores
    WHERE ClientID = c.ClientID
    ORDER BY ScoreDate DESC LIMIT 1
) npl ON TRUE

LEFT JOIN LATERAL (
    SELECT ExtScore2
    FROM public.BureauSnapshots
    WHERE ClientID = c.ClientID
    ORDER BY SnapshotDate DESC LIMIT 1
) lb ON TRUE

LEFT JOIN LATERAL (
    SELECT
        COUNT(*)                                            AS txn_count,
        MIN(CURRENT_DATE - TxnDate)                         AS last_txn_days,
        COUNT(*) FILTER (WHERE Channel='ONLINE')            AS online_count,
        COUNT(*) FILTER (WHERE TxnType='OVERDRAFT')         AS overdraft_count,
        COUNT(*) FILTER (WHERE TxnType='ATM') / 3.0        AS atm_count_mo,
        AVG(Balance)                                        AS avg_balance,
        MIN(Balance)                                        AS min_balance,
        SUM(ABS(Amount)) FILTER (WHERE TxnType='ATM')       AS atm_cash_monthly,
        SUM(ABS(Amount))                                    AS estimated_spend,
        COUNT(*) FILTER (WHERE TxnType='ATM')::DECIMAL
            / NULLIF(COUNT(*), 0)                           AS cash_affinity,
        COUNT(*) FILTER (WHERE TxnType != 'ATM')::DECIMAL
            / NULLIF(COUNT(*), 0)                           AS card_coverage,
        ROUND((MAX(Balance) - MIN(Balance))
            / NULLIF(ABS(AVG(Balance)), 0), 4)              AS balance_trend
    FROM public.Transactions
    WHERE ClientID = c.ClientID
      AND TxnDate >= CURRENT_DATE - INTERVAL '90 days'
) t ON TRUE

LEFT JOIN LATERAL (
    SELECT
        ROUND(SUM(COALESCE(AMT_CreditSumDebt, 0))
            / NULLIF(SUM(COALESCE(AMT_CreditSum, 0)), 0), 4)    AS credit_util,
        ROUND(SUM(COALESCE(AMT_CreditSumDebt, 0))
            / NULLIF(c.IncomeTotal, 0), 4)                       AS debt_to_income,
        SUM(CASE WHEN CreditActive = 'Bad debt' THEN 1 ELSE 0 END) AS bad_debt_count
    FROM public.BureauLoans
    WHERE ClientID = c.ClientID
) b ON TRUE

WHERE EXISTS (
    SELECT 1 FROM ml.PD_Scores ps WHERE ps.ClientID = c.ClientID
)
"""


def load_clients(conn) -> pd.DataFrame:
    log.info("Loading client features from DB ...")
    t0 = time.perf_counter()
    df = pd.read_sql(CLIENT_SQL, conn)
    df = df.drop_duplicates(subset="customer_id").reset_index(drop=True)
    log.info(f"  {len(df):,} clients loaded in {time.perf_counter()-t0:.1f}s")
    coerce_cols = [c for c in NUM_COLS if c not in RFM_FEAT] + PRODUCTS
    for col in coerce_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


# ============================================================
# SECTION 3: FEATURE ENGINEERING
# Identical to notebook Sections 5a-5c.
# ============================================================

def score_rfm(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["r_score"] = (pd.qcut(-out["last_txn_days"], q=RFM_QUANTILES,
                               labels=False, duplicates="drop") + 1)
    out["f_score"] = (pd.qcut(out["txn_frequency"], q=RFM_QUANTILES,
                               labels=False, duplicates="drop") + 1)
    out["m_score"] = (pd.qcut(out["estimated_total_spend"], q=RFM_QUANTILES,
                               labels=False, duplicates="drop") + 1)
    out["rfm_score"] = out["r_score"] + out["f_score"] + out["m_score"]
    def _seg(s):
        if s >= 12: return "Champions"
        if s >=  9: return "Loyal"
        if s >=  6: return "Potential"
        if s >=  4: return "At Risk"
        return "Lost"
    out["rfm_segment"] = out["rfm_score"].apply(_seg)
    out["rfm_alpha"]   = out["rfm_segment"].map(RFM_ALPHA).astype(np.float32)
    out["rfm_beta"]    = out["rfm_segment"].map(RFM_BETA).astype(np.float32)
    return out


def build_feature_matrix(df: pd.DataFrame,
                          scaler: MinMaxScaler | None = None,
                          fit_scaler: bool = False) -> tuple:
    """
    Returns (feature_matrix [N,42], scaler).
    fit_scaler=True: fits a new scaler (training mode).
    fit_scaler=False: uses the provided scaler (inference mode).
    """
    raw = df[NUM_COLS].values.astype(np.float32)
    if fit_scaler:
        scaler = MinMaxScaler()
        scaled = scaler.fit_transform(raw).astype(np.float32)
    else:
        scaled = scaler.transform(raw).astype(np.float32)
    col_idx = {c: i for i, c in enumerate(NUM_COLS)}
    for col in ["last_txn_days", "debt_to_income", "late_payments", "overdraft_cnt"]:
        if col in col_idx:
            scaled[:, col_idx[col]] = 1.0 - scaled[:, col_idx[col]]
    emp_oh  = (pd.get_dummies(df["employment_status"], prefix="emp")
               .reindex(columns=[f"emp_{c}" for c in EMP_CATEGORIES], fill_value=0)
               .values.astype(np.float32))
    city_oh = (pd.get_dummies(df["city_tier"], prefix="city")
               .reindex(columns=[f"city_{t}" for t in CITY_TIERS], fill_value=0)
               .values.astype(np.float32))
    fm = np.hstack([scaled, emp_oh, city_oh]).astype(np.float32)
    return fm, scaler


def compute_iuf(df: pd.DataFrame) -> np.ndarray:
    N = len(df)
    return np.array([np.log(N / (df[p].sum() + 1)) for p in PRODUCTS],
                    dtype=np.float32)


# ============================================================
# SECTION 4: NEAREST NEIGHBOURS INDEX (sklearn, replaces hnswlib)
# ============================================================

def build_nn_index(fm: np.ndarray) -> tuple:
    """L2-normalise → build sklearn NearestNeighbors cosine index.
    Returns (index, normed_fm). Drop-in replacement for hnswlib on Windows."""
    norms  = np.linalg.norm(fm, axis=1, keepdims=True) + 1e-9
    normed = (fm / norms).astype(np.float32)
    N      = len(normed)
    t0     = time.perf_counter()
    idx    = NearestNeighbors(
        n_neighbors = MAX_NEIGHBOURS + 1,
        metric      = "cosine",
        algorithm   = "brute",   # exact search, no DLL conflicts
        n_jobs      = -1,
    )
    idx.fit(normed)
    log.info(f"  NN index built: N={N:,} in {(time.perf_counter()-t0)*1000:.0f}ms")
    return idx, normed


# ============================================================
# SECTION 5: RECOMMENDER CLASSES (identical to notebook)
# ============================================================

class NNCFRecommender:
    def __init__(self, index, normed_fm, iuf, pop, df):
        self.idx = index
        self.nm  = normed_fm
        self.iuf = iuf
        self.pop = pop
        self.df  = df

    def _neighbours(self, tidx):
        dists, labs = self.idx.kneighbors(self.nm[[tidx]], n_neighbors=MAX_NEIGHBOURS + 1)
        dists, labs = dists[0], labs[0]
        mask = labs != tidx
        labs, dists = labs[mask], dists[mask]
        sims = (1.0 - dists[:MAX_NEIGHBOURS]).astype(np.float64)
        labs = labs[:MAX_NEIGHBOURS]
        above = sims >= SIM_THRESHOLD
        return (sims[above], labs[above]) if above.sum() >= 5 else (sims[:5], labs[:5])

    def recommend(self, tidx, top_k=TOP_K):
        sims, nbr = self._neighbours(tidx)
        own   = self.df.iloc[nbr][PRODUCTS].values.astype(np.float64)
        raw   = (sims[:, None] * own).sum(0) / (sims.sum() + 1e-9)
        score = raw * self.iuf.astype(np.float64)
        owned = self.df.iloc[tidx][PRODUCTS].values.astype(int)
        score[owned == 1] = -1.0
        unowned = owned == 0
        if not unowned.any():
            return pd.DataFrame(columns=["product", "final_score", "rank"])
        u = score[unowned]; lo, hi = u.min(), u.max()
        score[unowned] = (u - lo) / (hi - lo) if hi > lo else np.zeros_like(u)
        alpha = float(self.df.at[tidx, "rfm_alpha"])
        beta  = float(self.df.at[tidx, "rfm_beta"])
        final = np.where(unowned, alpha * score + beta * self.pop, -1.0)
        result = (pd.DataFrame({"product": PRODUCTS, "final_score": final})
                  .query("final_score >= 0")
                  .sort_values("final_score", ascending=False)
                  .reset_index(drop=True))
        result["rank"] = result.index + 1
        return result.head(top_k)


def apply_rules(recs_df: pd.DataFrame, row: pd.Series,
                income_median: float) -> tuple:
    """3 strong inferred rules from notebook (lift >= 2.5x)."""
    result = recs_df.copy()
    fired  = {}

    def _boost(product, amount, desc):
        if product in result["product"].values:
            result.loc[result["product"] == product, "final_score"] += amount
            fired[product] = desc

    # Rule 1: self-employed + tenure >= 1yr → business_account (lift 4.1x)
    if (row.get("employment_status") == "self_employed"
            and row.get("years_as_customer", 0) >= 1
            and row.get("business_account", 0) == 0):
        _boost("business_account", 0.36, "Self-employed + tenure≥1yr [lift 4.1×]")

    # Rule 2: life event + income > median → life_insurance (lift 2.6x)
    if ((row.get("life_marriage", 0) == 1 or row.get("life_home", 0) == 1)
            and row.get("annual_income", 0) > income_median
            and row.get("life_insurance", 0) == 0):
        _boost("life_insurance", 0.30, "Life event + income>median [lift 2.6×]")

    # Rule 3: working age + life event → mortgage (lift 2.5x)
    # NOTE: age column excluded from compliance — using life_retirement=0 as proxy
    if (row.get("life_retirement", 0) == 0
            and (row.get("life_home", 0) == 1 or row.get("life_marriage", 0) == 1)
            and row.get("mortgage", 0) == 0):
        _boost("mortgage", 0.30, "Working age + life event → mortgage [lift 2.5×]")

    result = result.sort_values("final_score", ascending=False).reset_index(drop=True)
    result["rank"] = result.index + 1
    return result, fired


class FullNBOSystem:
    """Complete system: NearestNeighbors-CF + rules + KMeans cold-start."""

    def __init__(self, cf, km, km_strata, pca, pop, df, income_median):
        self.cf            = cf
        self.km            = km
        self.km_strata     = km_strata
        self.pca           = pca
        self.pop           = pop
        self.df            = df
        self.income_median = income_median

    def recommend(self, tidx: int, top_k: int = TOP_K,
                  fm: np.ndarray | None = None) -> tuple:
        row     = self.df.iloc[tidx]
        n_owned = int(self.df.iloc[tidx][PRODUCTS].sum())
        method  = "COLD" if n_owned < 2 else "CF"

        if method == "COLD":
            x_pca = self.pca.transform(fm[[tidx]])
            c     = int(self.km.predict(x_pca)[0])
            pop   = self.km_strata.get(c, self.pop)
            owned = self.df.iloc[tidx][PRODUCTS].values.astype(int)
            s     = pop.copy(); s[owned == 1] = -1.0
            recs  = (pd.DataFrame({"product": PRODUCTS, "final_score": s.astype(np.float64)})
                     .query("final_score >= 0")
                     .sort_values("final_score", ascending=False)
                     .reset_index(drop=True))
            recs["rank"] = recs.index + 1
        else:
            recs = self.cf.recommend(tidx, top_k=len(PRODUCTS))

        recs, fired = apply_rules(recs, row, self.income_median)
        if fired:
            method = "RULE"
        return recs.head(top_k), fired, method


# ============================================================
# SECTION 6: HOLDOUT EVALUATION (identical to notebook logic)
# ============================================================

def make_holdout(df: pd.DataFrame, eval_n: int = 2000, seed: int = 7) -> tuple:
    rng        = np.random.default_rng(seed)
    candidates = [p for p in PRODUCTS if p not in EASY_PRODUCTS]
    eligible   = df[(df[PRODUCTS].sum(axis=1) >= 2) &
                    (df[candidates].sum(axis=1) >= 1)]
    sampled    = eligible.sample(min(eval_n, len(eligible)), random_state=seed)
    train_df   = df.copy()
    holdout    = {}
    for _, row in sampled.iterrows():
        owned = [p for p in candidates if row[p] == 1]
        if not owned:
            continue
        withheld = rng.choice(owned)
        train_df.at[row.name, withheld] = 0
        holdout[int(row["customer_id"])] = withheld
    return train_df, holdout


def ndcg_at_k(rel, k):
    rel = rel[:k]
    if not any(rel): return 0.0
    dcg  = sum(r / np.log2(i + 2) for i, r in enumerate(rel))
    idcg = sum(1 / np.log2(i + 2) for i in range(min(sum(rel), k)))
    return dcg / idcg if idcg > 0 else 0.0


def evaluate(system: FullNBOSystem, df: pd.DataFrame,
             holdout: dict, fm: np.ndarray) -> pd.DataFrame:
    rows = []
    t0   = time.perf_counter()
    for cid, withheld in holdout.items():
        mask = df["customer_id"] == cid
        if not mask.any():
            continue
        tidx = df.index[mask][0]
        recs, _, method = system.recommend(tidx, top_k=TOP_K, fm=fm)
        rec_products = list(recs["product"])
        hit   = int(withheld in rec_products)
        rank  = rec_products.index(withheld) + 1 if hit else TOP_K + 1
        rel   = [1 if p == withheld else 0 for p in rec_products]
        rows.append({
            "customer_id": cid, "withheld": withheld, "method": method,
            "hit_rate": hit, "ndcg": ndcg_at_k(rel, TOP_K),
            "precision": hit / TOP_K, "avg_rank_of_hit": rank,
        })
    elapsed = time.perf_counter() - t0
    res = pd.DataFrame(rows)
    m   = res[["hit_rate", "ndcg", "precision"]].mean()
    log.info(f"  Evaluation complete ({len(rows):,} clients, {elapsed:.1f}s)")
    log.info(f"  Hit@5={m.hit_rate:.4f}  NDCG@5={m.ndcg:.4f}  Prec@5={m.precision:.4f}")
    for p in sorted(HARD_PRODUCTS):
        sub = res[res["withheld"] == p]
        if len(sub):
            log.info(f"  {PRODUCT_LABELS[p]:<22} Hit@5={sub.hit_rate.mean():.3f}  n={len(sub)}")
    return res


# ============================================================
# SECTION 7: ARTIFACT SAVING / LOADING
# ============================================================

def save_artifacts(nn_idx, normed_fm, scaler, iuf, pop, km, km_strata,
                   pca_cs, income_median, df):
    log.info("Saving artifacts ...")
    joblib.dump(nn_idx,     ARTIFACTS / "nn_index.pkl")
    np.save(ARTIFACTS / "normed_fm.npy",    normed_fm)
    joblib.dump(scaler,     ARTIFACTS / "feature_scaler.pkl")
    np.save(ARTIFACTS / "iuf_weights.npy",  iuf)
    np.save(ARTIFACTS / "pop_array.npy",    pop)
    joblib.dump(km,         ARTIFACTS / "kmeans.pkl")
    joblib.dump(km_strata,  ARTIFACTS / "km_strata.pkl")
    joblib.dump(pca_cs,     ARTIFACTS / "pca_coldstart.pkl")

    cid_map = {int(r.customer_id): int(i) for i, r in df.iterrows()}
    with open(ARTIFACTS / "cid_to_idx.json", "w") as f:
        json.dump(cid_map, f)

    config = {
        "income_median": float(income_median),
        "SIM_THRESHOLD": SIM_THRESHOLD,
        "MAX_NEIGHBOURS": MAX_NEIGHBOURS,
        "TOP_K": TOP_K,
        "KMEANS_K": KMEANS_K,
        "PRODUCTS": PRODUCTS,
        "n_clients": len(df),
        "trained_on": pd.Timestamp.today().strftime("%Y-%m-%d"),
    }
    with open(ARTIFACTS / "nbo_config.json", "w") as f:
        json.dump(config, f, indent=2)
    log.info(f"  Artifacts saved to {ARTIFACTS}/")


def load_artifacts() -> dict:
    log.info("Loading artifacts ...")
    with open(ARTIFACTS / "nbo_config.json") as f:
        config = json.load(f)
    nn_idx = joblib.load(ARTIFACTS / "nn_index.pkl")
    return {
        "hnsw":          nn_idx,
        "normed_fm":     np.load(ARTIFACTS / "normed_fm.npy"),
        "scaler":        joblib.load(ARTIFACTS / "feature_scaler.pkl"),
        "iuf":           np.load(ARTIFACTS / "iuf_weights.npy"),
        "pop":           np.load(ARTIFACTS / "pop_array.npy"),
        "km":            joblib.load(ARTIFACTS / "kmeans.pkl"),
        "km_strata":     joblib.load(ARTIFACTS / "km_strata.pkl"),
        "pca_cs":        joblib.load(ARTIFACTS / "pca_coldstart.pkl"),
        "income_median": config["income_median"],
        "config":        config,
    }


# ============================================================
# SECTION 8: DAILY ETL — SCORE ALL CLIENTS → DB
# ============================================================

def log_etl_step(conn, step, status, rows=None, error=None, run_id=None):
    with conn.cursor() as cur:
        if run_id is None:
            cur.execute("""
                INSERT INTO stg.ETL_RunLog (RunDate, StepName, Status, StartedAt)
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


def get_cooldown_set(conn) -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT ClientID, ProductID FROM rpt.vw_NBO_Cooldown")
        return set(cur.fetchall())


def get_product_id_map(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT ProductCode, ProductID FROM public.Products WHERE IsActive=TRUE")
        return {row[0]: row[1] for row in cur.fetchall()}


def write_nbo_scores(conn, records: list, run_id: int):
    sql = """
        INSERT INTO ml.NBO_Scores
            (ClientID, SnapshotDate, ProductID, Rank, PropensityScore,
             EligibleFlag, SuppressReason, PD_Score_Used, PD_RiskTier_Used,
             Method, ModelVersion, ETL_RunID)
        VALUES %s
        ON CONFLICT (ClientID, SnapshotDate, ProductID, ModelVersion)
        DO UPDATE SET
            Rank            = EXCLUDED.Rank,
            PropensityScore = EXCLUDED.PropensityScore,
            EligibleFlag    = EXCLUDED.EligibleFlag,
            SuppressReason  = EXCLUDED.SuppressReason,
            Method          = EXCLUDED.Method,
            ETL_RunID       = EXCLUDED.ETL_RunID
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, records, page_size=5000)
    conn.commit()


def run_daily(df: pd.DataFrame, arts: dict):
    """
    Load artifacts → score all eligible clients → write ml.NBO_Scores.
    Runs every night after PD scoring completes.
    """
    conn   = get_conn()
    run_id = log_etl_step(conn, "nbo_scoring", "running")
    try:
        today        = pd.Timestamp.today().date()
        product_map  = get_product_id_map(conn)
        cooldown_set = get_cooldown_set(conn)
        log.info(f"  Product map: {len(product_map)} products")
        log.info(f"  Cooldown pairs: {len(cooldown_set):,}")

        fm, _ = build_feature_matrix(df, scaler=arts["scaler"], fit_scaler=False)

        cf = NNCFRecommender(
            arts["hnsw"], arts["normed_fm"],
            arts["iuf"], arts["pop"], df
        )
        system = FullNBOSystem(
            cf, arts["km"], arts["km_strata"],
            arts["pca_cs"], arts["pop"], df,
            arts["income_median"]
        )

        records            = []
        pd_high_suppressed = 0
        npl_concern_suppressed = 0
        cooldown_suppressed = 0

        for tidx in range(len(df)):
            row       = df.iloc[tidx]
            client_id = int(row["customer_id"])

            if row["pd_risk_tier"] in ("HIGH", "CRITICAL"):
                pd_high_suppressed += 1
                continue

            if row["npl_risk_tier"] in ("CONCERN", "CRITICAL"):
                npl_concern_suppressed += 1
                continue

            recs, _, method = system.recommend(tidx, top_k=TOP_K, fm=fm)

            for _, rec in recs.iterrows():
                product_code = rec["product"]
                product_id   = product_map.get(product_code)
                if product_id is None:
                    continue
                in_cooldown = (client_id, product_id) in cooldown_set
                if in_cooldown:
                    cooldown_suppressed += 1
                records.append((
                    client_id,
                    today,
                    product_id,
                    int(rec["rank"]),
                    round(float(rec["final_score"]), 4),
                    not in_cooldown,
                    "COOLDOWN" if in_cooldown else None,
                    round(float(row["pd_score"]), 7),
                    row["pd_risk_tier"],
                    method,
                    "nbo-v1",
                    run_id,
                ))

            if tidx % 10000 == 0 and tidx > 0:
                log.info(f"  Scored {tidx:,} / {len(df):,} clients ...")

        log.info(f"  PD HIGH/CRITICAL suppressed: {pd_high_suppressed:,}")
        log.info(f"  NPL CONCERN/CRITICAL suppressed: {npl_concern_suppressed:,}")
        log.info(f"  Cooldown suppressed: {cooldown_suppressed:,}")
        log.info(f"  Writing {len(records):,} recommendation rows ...")
        write_nbo_scores(conn, records, run_id)
        log_etl_step(conn, "nbo_scoring", "success", rows=len(records), run_id=run_id)
        log.info("  NBO daily scoring complete.")

    except Exception as e:
        log_etl_step(conn, "nbo_scoring", "failed", error=str(e)[:2000], run_id=run_id)
        raise
    finally:
        conn.close()


# ============================================================
# SECTION 9: TRAINING PIPELINE
# ============================================================

def train():
    log.info("=" * 60)
    log.info("NBO TRAINING PIPELINE")
    log.info("=" * 60)

    conn = get_conn()
    df   = load_clients(conn)
    conn.close()

    if len(df) == 0:
        raise ValueError(
            "No clients loaded — run `python nbo_train.py --mode ownership` first "
            "to populate public.NBO_ClientOwnership."
        )

    log.info("Computing RFM scores ...")
    df = score_rfm(df)
    seg_counts = df["rfm_segment"].value_counts()
    for seg, n in seg_counts.items():
        log.info(f"  {seg:<12} {n:>8,} ({n/len(df):.1%})")

    log.info("Creating holdout split (n=2000) ...")
    train_df, holdout = make_holdout(df, eval_n=2000)
    log.info(f"  Holdout: {len(holdout):,} clients with withheld products")

    log.info("Building feature matrix ...")
    fm_train, scaler = build_feature_matrix(train_df, fit_scaler=True)
    log.info(f"  Feature matrix: {fm_train.shape}  ({fm_train.nbytes/1e6:.1f} MB)")

    iuf   = compute_iuf(train_df)
    adopt = train_df[PRODUCTS].mean().values.astype(np.float32)
    pop   = (adopt - adopt.min()) / (adopt.max() - adopt.min() + 1e-9)
    log.info(f"  IUF range: [{iuf.min():.3f}, {iuf.max():.3f}]")
    log.info(f"  Rarest product: {PRODUCT_LABELS[PRODUCTS[iuf.argmax()]]}")

    log.info("Product ownership distribution:")
    for p, v in sorted(zip(PRODUCTS, adopt), key=lambda x: -x[1]):
        bar = "█" * int(v * 40)
        log.info(f"  {PRODUCT_LABELS[p]:<25} {v:.1%}  {bar}")

    log.info("Building NN index (sklearn brute cosine) ...")
    hnsw_idx, normed_fm = build_nn_index(fm_train)

    log.info("Building KMeans cold-start clusters ...")
    pca_cs = PCA(n_components=15, random_state=RANDOM_SEED)
    X_pca  = pca_cs.fit_transform(fm_train)
    km     = KMeans(n_clusters=KMEANS_K, random_state=RANDOM_SEED, n_init=10)
    km.fit(X_pca)
    sil    = silhouette_score(X_pca, km.labels_, sample_size=3000, random_state=42)
    n_cold = (train_df[PRODUCTS].sum(axis=1) < 2).sum()
    km_strata = {c: train_df.iloc[np.where(km.labels_ == c)[0]][PRODUCTS]
                    .mean().values.astype(np.float32)
                 for c in range(KMEANS_K)}
    log.info(f"  k={KMEANS_K}  sil={sil:.3f}  cold-start: {n_cold:,} ({n_cold/len(train_df):.1%})")

    income_median = float(train_df["annual_income"].median())
    cf     = NNCFRecommender(hnsw_idx, normed_fm, iuf, pop, train_df)
    system = FullNBOSystem(cf, km, km_strata, pca_cs, pop, train_df, income_median)

    log.info("Running holdout evaluation ...")
    results = evaluate(system, train_df, holdout, fm_train)

    save_artifacts(hnsw_idx, normed_fm, scaler, iuf, pop,
                   km, km_strata, pca_cs, income_median, train_df)

    log.info("=" * 60)
    log.info("TRAINING COMPLETE")
    log.info("=" * 60)
    return system, train_df, results


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NBO recommender pipeline")
    parser.add_argument(
        "--mode",
        choices=["ownership", "train", "daily", "eval"],
        default="train",
        help=(
            "ownership = derive product flags → public.NBO_ClientOwnership (monthly); "
            "train = fit model + save artifacts (one-time / monthly); "
            "daily = load artifacts + score all clients + write DB (nightly); "
            "eval = load artifacts + re-run holdout evaluation (ad-hoc)"
        ),
    )
    args = parser.parse_args()

    if args.mode == "ownership":
        # Run monthly — must complete before --mode train or the first --mode daily
        # of the month to ensure ownership data is fresh.
        refresh_ownership()

    elif args.mode == "train":
        train()

    elif args.mode == "daily":
        arts = load_artifacts()
        conn = get_conn()
        df   = load_clients(conn)
        conn.close()
        df = score_rfm(df)
        run_daily(df, arts)

    elif args.mode == "eval":
        arts = load_artifacts()
        conn = get_conn()
        df   = load_clients(conn)
        conn.close()
        df = score_rfm(df)
        _, holdout = make_holdout(df, eval_n=2000)
        fm, _      = build_feature_matrix(df, scaler=arts["scaler"])
        cf         = NNCFRecommender(arts["hnsw"], arts["normed_fm"],
                                     arts["iuf"], arts["pop"], df)
        system     = FullNBOSystem(cf, arts["km"], arts["km_strata"],
                                   arts["pca_cs"], arts["pop"], df,
                                   arts["income_median"])
        evaluate(system, df, holdout, fm)

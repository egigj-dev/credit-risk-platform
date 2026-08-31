-- ============================================================
-- Credit Risk DB v2 — Stored Procedures & Views (Clean Merged)
-- Data Science Division | Bank | April 2026
-- ============================================================
-- Execution order:
--   1. create_database_pg.sql
--   2. credit_risk_ddl_pg.sql
--   3. THIS FILE
--
-- Objects defined here (in execution order):
--   0. Prerequisite DDL — 3 delta tables + ETL_Control rows
--      missing from credit_risk_ddl_pg.sql (gap noted below)
--   1. stg.usp_LogAndRaiseError     — internal helper
--   2. stg.v_InstallmentDPD         — DPD view for NPL pipeline
--   3. stg.v_NPLFeatures            — NPL feature matrix view
--   4. stg.usp_StageSourceData      — pg_cron Step 1
--   5. stg.usp_ValidateStagedData   — pg_cron Step 2
--   6. ml.usp_BuildFeatureStoreRaw  — pg_cron Step 3
--   7. rpt.usp_RefreshReportingCache — pg_cron Step 5
--
-- What's merged vs old files:
--   fix_stage_proc.sql         — usp_StageSourceData fully replaced
--                                with PERF-1/2/3 patches and TIMESTAMPTZ
--                                watermarks baked in
--   usp_BuildFeatureStoreRaw_v2.sql — 138-feature version replaces
--                                     the 27-feature version
--   npl_calculations.sql       — stg.v_InstallmentDPD and
--                                stg.v_NPLFeatures added as section 2–3
--
-- DDL GAP — three delta tables are referenced by usp_StageSourceData
-- but were absent from credit_risk_ddl_pg.sql. They are created here
-- in Section 0.  Two source table columns are also missing:
--   public.BureauLoans.AMT_Annuity        (bureau annuity amount)
--   public.BureauLoans.DaysCreditUpdate   (days since last credit update)
--   public.PreviousApplications.AMT_Application (original application amount)
-- These ALTER TABLE statements are also in Section 0.
-- ============================================================


-- ============================================================
-- 0. PREREQUISITE DDL — objects missing from credit_risk_ddl_pg.sql
-- ============================================================

-- 0.1  Missing source table columns on public.BureauLoans
ALTER TABLE public.BureauLoans
    ADD COLUMN IF NOT EXISTS AMT_Annuity       DECIMAL(18,2) NULL,
    ADD COLUMN IF NOT EXISTS DaysCreditUpdate  INT           NULL;

-- 0.2  Missing source table column on public.PreviousApplications
ALTER TABLE public.PreviousApplications
    ADD COLUMN IF NOT EXISTS AMT_Application   DECIMAL(18,2) NULL;

-- 0.3  Missing delta tables for BureauLoans, PreviousApplications, InstallmentsPayments
--      Pattern matches existing delta tables in credit_risk_ddl_pg.sql:
--      composite PK (entity_id, ETL_RunID), append-only, FK to stg.ETL_RunLog.

CREATE TABLE IF NOT EXISTS stg.BureauLoans_Delta (
    BureauLoanID            BIGINT          NOT NULL,
    ClientID                BIGINT          NOT NULL,
    CreditActive            VARCHAR(12)     NOT NULL,
    DaysCredit              INT             NULL,
    DaysCreditEndDate       INT             NULL,
    AMT_CreditSum           DECIMAL(18,2)   NULL,
    AMT_CreditSumDebt       DECIMAL(18,2)   NULL,
    AMT_CreditSumOverdue    DECIMAL(18,2)   NULL,
    AMT_CreditMaxOverdue    DECIMAL(18,2)   NULL,
    CNT_CreditProlong       SMALLINT        NULL,
    AMT_Annuity             DECIMAL(18,2)   NULL,
    DaysCreditUpdate        INT             NULL,
    ETL_RunID               BIGINT          NOT NULL,
    ETL_LoadedAt            TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),
    CONSTRAINT PK_BurLoanDelta        PRIMARY KEY (BureauLoanID, ETL_RunID),
    CONSTRAINT FK_BurLoanDelta_RunLog FOREIGN KEY (ETL_RunID) REFERENCES stg.ETL_RunLog(RunID)
);

CREATE TABLE IF NOT EXISTS stg.PreviousApplications_Delta (
    PrevAppID               BIGINT          NOT NULL,
    ClientID                BIGINT          NOT NULL,
    ContractType            VARCHAR(30)     NULL,
    ContractStatus          VARCHAR(20)     NULL,
    AMT_Annuity             DECIMAL(18,2)   NULL,
    AMT_Application         DECIMAL(18,2)   NULL,
    AMT_Credit              DECIMAL(18,2)   NULL,
    AMT_DownPayment         DECIMAL(18,2)   NULL,
    AMT_GoodsPrice          DECIMAL(18,2)   NULL,
    DaysDecision            INT             NULL,
    ETL_RunID               BIGINT          NOT NULL,
    ETL_LoadedAt            TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),
    CONSTRAINT PK_PrevAppDelta        PRIMARY KEY (PrevAppID, ETL_RunID),
    CONSTRAINT FK_PrevAppDelta_RunLog FOREIGN KEY (ETL_RunID) REFERENCES stg.ETL_RunLog(RunID)
);

-- InstallmentsPayments has no natural surrogate key per payment row
-- (PrevAppID alone is not unique). Surrogate identity PK used.
CREATE TABLE IF NOT EXISTS stg.InstallmentsPayments_Delta (
    InstDeltaID             BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    PrevAppID               BIGINT          NOT NULL,
    ClientID                BIGINT          NOT NULL,
    DaysInstalment          INT             NULL,
    DaysEntryPayment        INT             NULL,
    AMT_Instalment          DECIMAL(18,2)   NULL,
    AMT_Payment             DECIMAL(18,2)   NULL,
    ETL_RunID               BIGINT          NOT NULL,
    ETL_LoadedAt            TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),
    CONSTRAINT FK_InstDelta_RunLog FOREIGN KEY (ETL_RunID) REFERENCES stg.ETL_RunLog(RunID)
);

CREATE INDEX IF NOT EXISTS IX_BurLoanDelta_ClientRun
    ON stg.BureauLoans_Delta (ClientID, ETL_RunID);
CREATE INDEX IF NOT EXISTS IX_PrevAppDelta_ClientRun
    ON stg.PreviousApplications_Delta (ClientID, ETL_RunID);
CREATE INDEX IF NOT EXISTS IX_InstDelta_ClientRun
    ON stg.InstallmentsPayments_Delta (ClientID, ETL_RunID);

-- 0.4  Seed missing ETL_Control watermark rows for the three new source tables
INSERT INTO stg.ETL_Control (SourceTable, LastWatermarkSource) VALUES
    ('public.BureauLoans',          'CreatedAt'),
    ('public.PreviousApplications', 'CreatedAt'),
    ('public.InstallmentsPayments', 'CreatedAt')
ON CONFLICT (SourceTable) DO NOTHING;


-- ============================================================
-- 1. HELPER — log error and re-raise
-- ============================================================
CREATE OR REPLACE FUNCTION stg.usp_LogAndRaiseError(
    p_RunID    BIGINT,
    p_StepName VARCHAR(50),
    p_Message  TEXT
)
RETURNS VOID AS $$
BEGIN
    UPDATE stg.ETL_RunLog
    SET    Status       = 'failed',
           ErrorMessage = p_Message,
           FinishedAt   = NOW()
    WHERE  RunID = p_RunID;

    RAISE EXCEPTION '[%] %', p_StepName, p_Message;
END;
$$ LANGUAGE plpgsql;


-- ============================================================
-- 2. VIEW — stg.v_InstallmentDPD
-- ============================================================
-- DPD (Days Past Due) computation per instalment row.
-- Source: public.InstallmentsPayments
--
-- DPD = DaysEntryPayment - DaysInstalment
--   positive  = client paid late (days overdue)
--   zero/neg  = paid on time or early
--   NULL      = payment not yet recorded (UNPAID — treated separately)
--
-- [H1] DPD is NULL for unpaid instalments rather than 0.
--      Coalescing to 0 would silently hide the most severe default signal.
--      Downstream aggregations handle NULL explicitly.
-- ============================================================
CREATE OR REPLACE VIEW stg.v_InstallmentDPD AS
SELECT
    ClientID,
    PrevAppID,
    DaysInstalment,
    DaysEntryPayment,
    AMT_Instalment,
    AMT_Payment,

    -- Core DPD: NULL when no payment recorded; non-negative otherwise.
    CASE
        WHEN DaysEntryPayment IS NULL THEN NULL
        ELSE GREATEST(DaysEntryPayment - DaysInstalment, 0)::SMALLINT
    END                                             AS DPD,

    -- DPD bucket: UNPAID is distinct from DPD=0
    CASE
        WHEN DaysEntryPayment IS NULL               THEN 'UNPAID'
        WHEN DaysEntryPayment - DaysInstalment >= 90 THEN '90+'
        WHEN DaysEntryPayment - DaysInstalment >= 60 THEN '60-89'
        WHEN DaysEntryPayment - DaysInstalment >= 30 THEN '30-59'
        WHEN DaysEntryPayment - DaysInstalment > 0   THEN '1-29'
        ELSE '0'
    END                                             AS DPD_Bucket,

    -- Payment ratio: 1.0 = paid exactly, >1 = overpaid, <1 = underpaid.
    -- NULL when no payment recorded.
    CASE
        WHEN DaysEntryPayment IS NOT NULL
         AND COALESCE(AMT_Instalment, 0) > 0
             THEN ROUND(COALESCE(AMT_Payment, 0) / AMT_Instalment, 4)
        ELSE NULL
    END                                             AS PaymentRatio,

    -- Recency rank: rank 1 = most recent instalment per loan.
    -- Negative DaysInstalment = past; more negative = older.
    RANK() OVER (
        PARTITION BY ClientID, PrevAppID
        ORDER BY DaysInstalment DESC
    )                                               AS Instalment_RecencyRank

FROM public.InstallmentsPayments
WHERE DaysInstalment IS NOT NULL;


-- ============================================================
-- 3. VIEW — stg.v_NPLFeatures
-- ============================================================
-- NPL feature matrix: one row per client per active loan.
--
-- TEMPORAL DESIGN — eliminates label leakage [H2, H4]:
--   FEATURE window : DaysInstalment < -365 (well before current date)
--   LABEL window   : DaysInstalment >= -365 (recent / forward-looking)
--   txn_stress     : last 90 days from CURRENT_DATE [H4]
--
-- Key fixes vs v1:
--   [H1] Unpaid instalments kept as NULL DPD (not zeroed)
--   [H2] Label derived from forward window only (no leakage)
--   [H3] instalments_since_last_late kept NULL for "never been late"
--   [H4] txn_stress window anchored to CURRENT_DATE
--   [M1] loan_tenor_years: negation removed; renamed from loan_age_years_proxy
--   [M2] payment_ratio_trend: NULL when insufficient history (<4 instalments)
--   [M3] balance_volatility replaced with coefficient of variation (STDDEV/|mean|)
--   [M4] npl_composite_score: mutually exclusive DPD buckets; capped at 1.0
--   [L1] instalment_amount_std surfaced in SELECT
--   [L2] pos_completed_count surfaced in SELECT
--   [L3] bureau_has_credit_data flag added; raw debt/limit surfaced
-- ============================================================
CREATE OR REPLACE VIEW stg.v_NPLFeatures AS
WITH

inst_feat AS (
    -- Feature window: instalments due well before today (historical signals)
    SELECT * FROM stg.v_InstallmentDPD WHERE DaysInstalment < -365
),

inst_label AS (
    -- Label window: recent / future instalments (what WILL happen)
    SELECT * FROM stg.v_InstallmentDPD WHERE DaysInstalment >= -365
),

inst_agg AS (
    SELECT
        ClientID,
        COUNT(*)                                                AS total_instalments,
        COUNT(DISTINCT PrevAppID)                               AS loans_with_instalments,
        SUM(CASE WHEN DPD IS NULL THEN 1 ELSE 0 END)           AS unpaid_instalment_count,  -- [H1]
        MAX(DPD)                                                AS max_dpd_ever,
        MAX(CASE WHEN DPD >= 30 THEN 1 ELSE 0 END)             AS dpd_30_ever,
        MAX(CASE WHEN DPD >= 60 THEN 1 ELSE 0 END)             AS dpd_60_ever,
        MAX(CASE WHEN DPD >= 90 THEN 1 ELSE 0 END)             AS dpd_90_ever,
        SUM(CASE WHEN DPD > 0   THEN 1 ELSE 0 END)             AS late_payment_count,
        SUM(CASE WHEN DPD >= 30 THEN 1 ELSE 0 END)             AS dpd30_count,
        SUM(CASE WHEN DPD >= 60 THEN 1 ELSE 0 END)             AS dpd60_count,
        SUM(CASE WHEN DPD >= 90 THEN 1 ELSE 0 END)             AS dpd90_count,
        ROUND(AVG(PaymentRatio), 4)                             AS payment_ratio_overall,
        ROUND(MIN(PaymentRatio), 4)                             AS payment_ratio_min,
        ROUND(AVG(CASE WHEN Instalment_RecencyRank <= 3 THEN PaymentRatio END), 4)
                                                                AS payment_ratio_3m,
        ROUND(AVG(CASE WHEN Instalment_RecencyRank <= 6 THEN PaymentRatio END), 4)
                                                                AS payment_ratio_6m,
        -- [H3] NULL = never been late; do not sentinel to 999
        MIN(CASE WHEN DPD > 0 THEN Instalment_RecencyRank END) AS instalments_since_last_late,
        ROUND(SUM(GREATEST(COALESCE(AMT_Instalment,0) - COALESCE(AMT_Payment,0), 0)), 2)
                                                                AS total_payment_shortfall,
        ROUND(STDDEV(AMT_Instalment), 2)                        AS instalment_amount_std   -- [L1]
    FROM inst_feat
    GROUP BY ClientID
),

inst_trend AS (
    -- [M2] has_trend_data guards against NULL trend for clients with <4 instalments
    SELECT
        ClientID,
        (COUNT(*) FILTER (WHERE Instalment_RecencyRank BETWEEN 4 AND 6)) > 0
                                                                AS has_trend_data,
        ROUND(
            AVG(CASE WHEN Instalment_RecencyRank <= 3         THEN PaymentRatio END)
          - AVG(CASE WHEN Instalment_RecencyRank BETWEEN 4 AND 6 THEN PaymentRatio END),
        4)                                                      AS payment_ratio_trend
    FROM inst_feat
    GROUP BY ClientID
),

inst_label_agg AS (
    -- [H2] Labels from forward window only
    SELECT
        ClientID,
        MAX(CASE WHEN DPD >= 30   THEN 1 ELSE 0 END)           AS npl_label_30d,
        MAX(CASE WHEN DPD >= 60   THEN 1 ELSE 0 END)           AS npl_label_60d,
        MAX(CASE WHEN DPD >= 90   THEN 1 ELSE 0 END)           AS npl_label_90d,
        MAX(CASE WHEN DPD IS NULL THEN 1 ELSE 0 END)           AS npl_label_unpaid
    FROM inst_label
    GROUP BY ClientID
),

pos_agg AS (
    SELECT
        ClientID,
        COUNT(*)                                                AS pos_months_count,
        ROUND(AVG(SK_DPD), 2)                                  AS pos_dpd_avg,
        MAX(SK_DPD)                                             AS pos_dpd_max,
        ROUND(AVG(SK_DPD_DEF), 2)                              AS pos_dpd_def_avg,
        MAX(SK_DPD_DEF)                                         AS pos_dpd_def_max,
        SUM(CASE WHEN NAME_Contract_Status = 'Completed' THEN 1 ELSE 0 END)
                                                                AS pos_completed_count,   -- [L2]
        SUM(CASE WHEN SK_DPD > 0   THEN 1 ELSE 0 END)         AS pos_late_months,
        SUM(CASE WHEN SK_DPD >= 30 THEN 1 ELSE 0 END)         AS pos_dpd30_months
    FROM public.POS_CashBalance
    GROUP BY ClientID
),

bureau_stress AS (
    -- [L3] has_credit_data flag distinguishes "no bureau data" from "0% used"
    SELECT
        ClientID,
        COUNT(*) > 0                                            AS bureau_has_credit_data,
        SUM(CASE WHEN CreditActive = 'Bad debt' THEN 1 ELSE 0 END)
                                                                AS bureau_bad_debt_count,
        SUM(COALESCE(AMT_CreditSumOverdue, 0))                 AS bureau_total_overdue,
        MAX(COALESCE(AMT_CreditMaxOverdue, 0))                 AS bureau_max_overdue,
        SUM(COALESCE(AMT_CreditSumDebt, 0))                    AS bureau_total_debt,
        SUM(COALESCE(AMT_CreditSum, 0))                        AS bureau_total_limit,
        ROUND(
            SUM(COALESCE(AMT_CreditSumDebt, 0)) /
            NULLIF(SUM(COALESCE(AMT_CreditSum, 0)), 0),
        4)                                                      AS bureau_credit_util,
        SUM(COALESCE(CNT_CreditProlong, 0))                    AS bureau_prolong_count
    FROM public.BureauLoans
    GROUP BY ClientID
),

txn_stress AS (
    -- [H4] Window anchored to CURRENT_DATE for live scoring
    -- [M3] Coefficient of variation (STDDEV / |mean|) replaces range/mean ratio
    SELECT
        ClientID,
        SUM(CASE WHEN TxnType = 'OVERDRAFT' THEN 1    ELSE 0 END) AS overdraft_count,
        SUM(CASE WHEN TxnType = 'SALARY'    THEN 1    ELSE 0 END) AS salary_credit_count,
        MIN(Balance)                                               AS min_balance,
        AVG(Balance)                                               AS avg_balance,
        ROUND(STDDEV(Balance) / NULLIF(ABS(AVG(Balance)), 0), 4)  AS balance_cv
    FROM public.Transactions
    WHERE TxnDate BETWEEN CURRENT_DATE - INTERVAL '90 days' AND CURRENT_DATE
    GROUP BY ClientID
)

SELECT
    c.ClientID,
    la.ApplicationID,
    CURRENT_DATE                                            AS FeatureDate,

    -- ── Loan context ──────────────────────────────────────────────
    la.LoanAmount,
    la.Annuity,
    la.LoanTenorMonths,
    la.ContractType,
    la.LoanTenorMonths::DECIMAL / 12                        AS loan_tenor_years,  -- [M1]

    -- ── Client demographics ───────────────────────────────────────
    c.IncomeTotal,
    (-c.Days_Employed / 365.25)::DECIMAL(6,2)               AS employed_years,
    c.CNTChildren,
    c.CNTFamilyMembers,
    c.RegionRating,

    -- ── PD score at origination ───────────────────────────────────
    -- [M5] Uses earliest ScoreDate per ApplicationID (origination score)
    pd.PD_Score                                             AS pd_score_at_origin,
    pd.RiskTier                                             AS pd_tier_at_origin,

    -- ── Instalment volume ─────────────────────────────────────────
    COALESCE(ia.total_instalments, 0)                       AS total_instalments,
    COALESCE(ia.loans_with_instalments, 0)                  AS loans_with_instalments,

    -- ── Unpaid signal [H1] ────────────────────────────────────────
    COALESCE(ia.unpaid_instalment_count, 0)                 AS unpaid_instalment_count,

    -- ── DPD history ───────────────────────────────────────────────
    COALESCE(ia.max_dpd_ever, 0)                            AS max_dpd_ever,
    COALESCE(ia.dpd_30_ever, 0)                             AS dpd_30_ever,
    COALESCE(ia.dpd_60_ever, 0)                             AS dpd_60_ever,
    COALESCE(ia.dpd_90_ever, 0)                             AS dpd_90_ever,
    COALESCE(ia.late_payment_count, 0)                      AS late_payment_count,
    COALESCE(ia.dpd30_count, 0)                             AS dpd30_count,
    COALESCE(ia.dpd60_count, 0)                             AS dpd60_count,
    COALESCE(ia.dpd90_count, 0)                             AS dpd90_count,

    -- ── Payment ratio signals ─────────────────────────────────────
    COALESCE(ia.payment_ratio_overall, 1.0)                 AS payment_ratio_overall,
    COALESCE(ia.payment_ratio_3m, 1.0)                      AS payment_ratio_3m,
    COALESCE(ia.payment_ratio_6m, 1.0)                      AS payment_ratio_6m,
    COALESCE(ia.payment_ratio_min, 1.0)                     AS payment_ratio_min,

    -- ── Payment trend [M2] ────────────────────────────────────────
    it.has_trend_data                                       AS has_payment_trend_data,
    it.payment_ratio_trend                                  AS payment_ratio_trend,

    -- ── Late payment recency [H3] ─────────────────────────────────
    (ia.instalments_since_last_late IS NULL)                AS never_been_late,
    ia.instalments_since_last_late                          AS instalments_since_last_late,

    -- ── Payment shortfall + stability ─────────────────────────────
    COALESCE(ia.total_payment_shortfall, 0)                 AS total_payment_shortfall,
    ia.instalment_amount_std                                AS instalment_amount_std,

    -- ── POS Cash signals ──────────────────────────────────────────
    COALESCE(po.pos_dpd_avg, 0)                             AS pos_dpd_avg,
    COALESCE(po.pos_dpd_max, 0)                             AS pos_dpd_max,
    COALESCE(po.pos_dpd_def_avg, 0)                         AS pos_dpd_def_avg,
    COALESCE(po.pos_dpd_def_max, 0)                         AS pos_dpd_def_max,
    COALESCE(po.pos_late_months, 0)                         AS pos_late_months,
    COALESCE(po.pos_dpd30_months, 0)                        AS pos_dpd30_months,
    COALESCE(po.pos_completed_count, 0)                     AS pos_completed_count,   -- [L2]

    -- ── Bureau stress signals [L3] ────────────────────────────────
    COALESCE(bs.bureau_has_credit_data, FALSE)              AS bureau_has_credit_data,
    COALESCE(bs.bureau_bad_debt_count, 0)                   AS bureau_bad_debt_count,
    COALESCE(bs.bureau_total_overdue, 0)                    AS bureau_total_overdue,
    COALESCE(bs.bureau_max_overdue, 0)                      AS bureau_max_overdue,
    COALESCE(bs.bureau_total_debt, 0)                       AS bureau_total_debt,
    COALESCE(bs.bureau_total_limit, 0)                      AS bureau_total_limit,
    bs.bureau_credit_util                                   AS bureau_credit_util,
    COALESCE(bs.bureau_prolong_count, 0)                    AS bureau_prolong_count,

    -- ── Transaction stress signals (90d) [H4] ─────────────────────
    COALESCE(ts.overdraft_count, 0)                         AS overdraft_count_90d,
    COALESCE(ts.salary_credit_count, 0)                     AS salary_credit_count_90d,
    COALESCE(ts.min_balance, 0)                             AS min_balance_90d,
    COALESCE(ts.avg_balance, 0)                             AS avg_balance_90d,
    ts.balance_cv                                           AS balance_cv_90d,   -- [M3]

    -- ── Composite NPL score [M4] ──────────────────────────────────
    -- Mutually exclusive DPD buckets prevent double-counting.
    -- Total weight budget: 0.55 + 0.15 + 0.10 + 0.12 + 0.08 + 0.10 = 1.10;
    -- LEAST(..., 1.0) caps the score.
    LEAST(ROUND((
        CASE
            WHEN COALESCE(ia.dpd_90_ever, 0) = 1 THEN 0.55
            WHEN COALESCE(ia.dpd_60_ever, 0) = 1 THEN 0.40
            WHEN COALESCE(ia.dpd_30_ever, 0) = 1 THEN 0.25
            ELSE 0.0
        END
        + LEAST(COALESCE(ia.dpd30_count, 0) / 5.0, 1.0) * 0.15
        + CASE WHEN COALESCE(it.payment_ratio_trend, 0) < -0.10 THEN 0.10 ELSE 0 END
        + LEAST(COALESCE(bs.bureau_bad_debt_count, 0) / 2.0, 1.0) * 0.12
        + CASE WHEN COALESCE(ts.overdraft_count, 0) > 3        THEN 0.08 ELSE 0 END
        + CASE WHEN COALESCE(ia.unpaid_instalment_count, 0) > 0 THEN 0.10 ELSE 0 END
    )::NUMERIC, 4), 1.0)                                    AS npl_composite_score,

    -- ── NPL labels (forward window only) [H2] ─────────────────────
    -- NULL in production scoring (no future instalments yet observable).
    COALESCE(il.npl_label_30d, 0)                           AS npl_label_30d,
    COALESCE(il.npl_label_60d, 0)                           AS npl_label_60d,
    COALESCE(il.npl_label_90d, 0)                           AS npl_label_90d,
    COALESCE(il.npl_label_unpaid, 0)                        AS npl_label_unpaid

FROM public.Clients c
JOIN public.LoanApplications la ON la.ClientID = c.ClientID

LEFT JOIN LATERAL (
    -- Origination PD score: earliest score for this application [M5]
    SELECT PD_Score, RiskTier
    FROM ml.PD_Scores
    WHERE ApplicationID = la.ApplicationID
    ORDER BY ScoreDate ASC
    LIMIT 1
) pd ON TRUE

LEFT JOIN inst_agg       ia ON ia.ClientID = c.ClientID
LEFT JOIN inst_trend      it ON it.ClientID = c.ClientID
LEFT JOIN inst_label_agg  il ON il.ClientID = c.ClientID
LEFT JOIN pos_agg         po ON po.ClientID = c.ClientID
LEFT JOIN bureau_stress   bs ON bs.ClientID = c.ClientID
LEFT JOIN txn_stress      ts ON ts.ClientID = c.ClientID;


-- ============================================================
-- 4. PROCEDURE — stg.usp_StageSourceData
-- ============================================================
-- Watermark-based incremental ingestion for all 7 source tables.
--
-- Performance patches (all from fix_stage_proc.sql):
--   [PERF-1] Transactions: v_WM_Txn cast to DATE → enables partition pruning.
--            Without the cast, PostgreSQL promotes TxnDate to TIMESTAMP,
--            disabling partition elimination and causing full scans.
--   [PERF-2] NOT EXISTS correlated subqueries replaced with LEFT JOIN
--            anti-joins everywhere. Equivalent semantics but the planner
--            can use a hash anti-join instead of a nested loop.
--   [PERF-3] BureauSnapshots now uses UpdatedAt (FIX-4 from DDL) instead
--            of CreatedAt, consistent with other mutable source tables.
--
-- Watermarks changed from TIMESTAMP to TIMESTAMPTZ throughout to prevent
-- implicit timezone coercion issues.
--
-- Flow per source table:
--   1. Read LastWatermark from stg.ETL_Control.
--   2. Pull rows where UpdatedAt/CreatedAt > watermark.
--   3. Detect INSERT vs UPDATE vs prior run.
--   4. Anti-join to skip rows already staged in this run (idempotency).
--   5. APPEND to delta table (composite PK = entity_id + ETL_RunID).
--   6. Advance watermark from MAX timestamp of staged rows.
-- ============================================================
CREATE OR REPLACE FUNCTION stg.usp_StageSourceData(
    p_RunDate DATE DEFAULT NULL
)
RETURNS VOID AS $$
DECLARE
    v_RunID             BIGINT;
    v_BatchID           UUID        := gen_random_uuid();
    v_StepName          VARCHAR(50) := 'staging';
    v_T0                TIMESTAMP   := NOW();
    v_ActualRunDate     DATE;

    -- Watermarks as TIMESTAMPTZ to prevent coercion issues
    v_WM_App            TIMESTAMPTZ;
    v_WM_Client         TIMESTAMPTZ;
    v_WM_Txn            TIMESTAMPTZ;
    v_WM_Bureau         TIMESTAMPTZ;
    v_WM_BureauLoans    TIMESTAMPTZ;
    v_WM_PrevApp        TIMESTAMPTZ;
    v_WM_Inst           TIMESTAMPTZ;

    v_NewWM_App         TIMESTAMPTZ;
    v_NewWM_Client      TIMESTAMPTZ;
    v_NewWM_Txn         TIMESTAMPTZ;
    v_NewWM_Bureau      TIMESTAMPTZ;
    v_NewWM_BureauLoans TIMESTAMPTZ;
    v_NewWM_PrevApp     TIMESTAMPTZ;
    v_NewWM_Inst        TIMESTAMPTZ;

    v_AppRows     INT := 0;
    v_ClientRows  INT := 0;
    v_TxnRows     INT := 0;
    v_BurRows     INT := 0;
    v_BurLoanRows INT := 0;
    v_PrevAppRows INT := 0;
    v_InstRows    INT := 0;
    v_RowsTotal   INT := 0;
BEGIN
    v_ActualRunDate := COALESCE(p_RunDate, CURRENT_DATE - 1);

    INSERT INTO stg.ETL_RunLog (BatchID, RunDate, StepName, Status)
    VALUES (v_BatchID, v_ActualRunDate, v_StepName, 'running')
    RETURNING RunID INTO v_RunID;

    BEGIN
        -- ── Read watermarks ──────────────────────────────────────────────────
        SELECT COALESCE(LastWatermark, '1970-01-01'::TIMESTAMPTZ) INTO v_WM_App
        FROM stg.ETL_Control WHERE SourceTable = 'public.LoanApplications';

        SELECT COALESCE(LastWatermark, '1970-01-01'::TIMESTAMPTZ) INTO v_WM_Client
        FROM stg.ETL_Control WHERE SourceTable = 'public.Clients';

        SELECT COALESCE(LastWatermark, '1970-01-01'::TIMESTAMPTZ) INTO v_WM_Txn
        FROM stg.ETL_Control WHERE SourceTable = 'public.Transactions';

        SELECT COALESCE(LastWatermark, '1970-01-01'::TIMESTAMPTZ) INTO v_WM_Bureau
        FROM stg.ETL_Control WHERE SourceTable = 'public.BureauSnapshots';

        SELECT COALESCE(LastWatermark, '1970-01-01'::TIMESTAMPTZ) INTO v_WM_BureauLoans
        FROM stg.ETL_Control WHERE SourceTable = 'public.BureauLoans';

        SELECT COALESCE(LastWatermark, '1970-01-01'::TIMESTAMPTZ) INTO v_WM_PrevApp
        FROM stg.ETL_Control WHERE SourceTable = 'public.PreviousApplications';

        SELECT COALESCE(LastWatermark, '1970-01-01'::TIMESTAMPTZ) INTO v_WM_Inst
        FROM stg.ETL_Control WHERE SourceTable = 'public.InstallmentsPayments';

        -- Guard: fail fast if any ETL_Control row is missing
        IF v_WM_App         IS NULL THEN RAISE EXCEPTION 'Missing ETL_Control row for public.LoanApplications';     END IF;
        IF v_WM_Client      IS NULL THEN RAISE EXCEPTION 'Missing ETL_Control row for public.Clients';              END IF;
        IF v_WM_Txn         IS NULL THEN RAISE EXCEPTION 'Missing ETL_Control row for public.Transactions';         END IF;
        IF v_WM_Bureau      IS NULL THEN RAISE EXCEPTION 'Missing ETL_Control row for public.BureauSnapshots';      END IF;
        IF v_WM_BureauLoans IS NULL THEN RAISE EXCEPTION 'Missing ETL_Control row for public.BureauLoans';          END IF;
        IF v_WM_PrevApp     IS NULL THEN RAISE EXCEPTION 'Missing ETL_Control row for public.PreviousApplications'; END IF;
        IF v_WM_Inst        IS NULL THEN RAISE EXCEPTION 'Missing ETL_Control row for public.InstallmentsPayments'; END IF;

        -- ── Applications ─────────────────────────────────────────────────────
        -- [PERF-2] LEFT JOIN anti-join replaces NOT EXISTS correlated subquery.
        INSERT INTO stg.Applications_Delta (
            ApplicationID, ClientID, ApplicationDate, LoanAmount, Annuity,
            LoanTenorMonths, GoodsPriceAmount, ContractType, TARGET, DeltaAction, ETL_RunID
        )
        SELECT
            la.ApplicationID, la.ClientID, la.ApplicationDate,
            la.LoanAmount, la.Annuity, la.LoanTenorMonths, la.GoodsPriceAmount,
            la.ContractType, la.TARGET,
            CASE WHEN prev.ApplicationID IS NULL THEN 'INSERT' ELSE 'UPDATE' END,
            v_RunID
        FROM public.LoanApplications la
        -- Detect INSERT vs UPDATE vs prior successful run
        LEFT JOIN (
            SELECT ApplicationID FROM stg.Applications_Delta
            WHERE ETL_RunID = (
                SELECT MAX(r.RunID) FROM stg.ETL_RunLog r
                WHERE r.StepName = 'staging' AND r.Status = 'success'
            )
        ) prev ON prev.ApplicationID = la.ApplicationID
        -- [PERF-2] Anti-join: skip rows already staged in this run (idempotency)
        LEFT JOIN stg.Applications_Delta ex
               ON ex.ApplicationID = la.ApplicationID AND ex.ETL_RunID = v_RunID
        WHERE la.UpdatedAt > v_WM_App
          AND ex.ApplicationID IS NULL;

        GET DIAGNOSTICS v_AppRows = ROW_COUNT;
        v_NewWM_App := GREATEST(v_WM_App,
            (v_ActualRunDate::TIMESTAMPTZ + INTERVAL '1 day' - INTERVAL '1 second'));

        -- ── Clients ──────────────────────────────────────────────────────────
        INSERT INTO stg.Clients_Delta (
            ClientID, BirthDate, Gender, EducationType, FamilyStatus,
            IncomeTotal, IncomeType, OwnCar, OwnRealty, CNTChildren,
            CNTFamilyMembers, RegionRating, Days_Employed, DeltaAction, ETL_RunID
        )
        SELECT
            c.ClientID, c.BirthDate, c.Gender, c.EducationType, c.FamilyStatus,
            c.IncomeTotal, c.IncomeType, c.OwnCar, c.OwnRealty, c.CNTChildren,
            c.CNTFamilyMembers, c.RegionRating, c.Days_Employed, 'INSERT', v_RunID
        FROM public.Clients c
        LEFT JOIN stg.Clients_Delta ex
               ON ex.ClientID = c.ClientID AND ex.ETL_RunID = v_RunID
        WHERE c.UpdatedAt > v_WM_Client
          AND ex.ClientID IS NULL;

        GET DIAGNOSTICS v_ClientRows = ROW_COUNT;
        v_NewWM_Client := GREATEST(v_WM_Client,
            (v_ActualRunDate::TIMESTAMPTZ + INTERVAL '1 day' - INTERVAL '1 second'));

        -- ── Transactions ─────────────────────────────────────────────────────
        -- [PERF-1] v_WM_Txn cast to DATE so the planner can use partition pruning.
        --          Without ::DATE, PostgreSQL promotes TxnDate (DATE) to TIMESTAMP
        --          for comparison, disabling partition elimination across all partitions.
        INSERT INTO stg.Transactions_Delta (
            TxnID, ClientID, TxnDate, TxnType, Amount, Balance, Channel, ETL_RunID
        )
        SELECT t.TxnID, t.ClientID, t.TxnDate, t.TxnType, t.Amount, t.Balance, t.Channel, v_RunID
        FROM public.Transactions t
        LEFT JOIN stg.Transactions_Delta ex
               ON ex.TxnID = t.TxnID AND ex.TxnDate = t.TxnDate AND ex.ETL_RunID = v_RunID
        WHERE t.TxnDate > v_WM_Txn::DATE   -- [PERF-1] cast enables partition pruning
          AND ex.TxnID IS NULL;

        GET DIAGNOSTICS v_TxnRows = ROW_COUNT;
        v_NewWM_Txn := GREATEST(v_WM_Txn,
            (v_ActualRunDate::TIMESTAMPTZ + INTERVAL '1 day' - INTERVAL '1 second'));

        -- ── Bureau snapshots ─────────────────────────────────────────────────
        -- [PERF-3] UpdatedAt (added by FIX-4 in DDL) replaces CreatedAt.
        INSERT INTO stg.Bureau_Delta (
            SnapshotID, ClientID, SnapshotDate,
            ExtScore1, ExtScore2, ExtScore3, ActiveLoans, ClosedLoans, EnquiriesYear, ETL_RunID
        )
        SELECT
            bs.SnapshotID, bs.ClientID, bs.SnapshotDate,
            bs.ExtScore1, bs.ExtScore2, bs.ExtScore3,
            bs.ActiveLoans, bs.ClosedLoans, bs.EnquiriesYear, v_RunID
        FROM public.BureauSnapshots bs
        LEFT JOIN stg.Bureau_Delta ex
               ON ex.SnapshotID = bs.SnapshotID AND ex.ETL_RunID = v_RunID
        WHERE bs.UpdatedAt > v_WM_Bureau   -- [PERF-3] UpdatedAt (was CreatedAt)
          AND ex.SnapshotID IS NULL;

        GET DIAGNOSTICS v_BurRows = ROW_COUNT;
        SELECT COALESCE(MAX(bs.UpdatedAt), v_WM_Bureau) INTO v_NewWM_Bureau
        FROM public.BureauSnapshots bs
        INNER JOIN stg.Bureau_Delta bd ON bd.SnapshotID = bs.SnapshotID AND bd.ETL_RunID = v_RunID;

        -- ── Bureau loans ─────────────────────────────────────────────────────
        INSERT INTO stg.BureauLoans_Delta (
            BureauLoanID, ClientID, CreditActive, DaysCredit, DaysCreditEndDate,
            AMT_CreditSum, AMT_CreditSumDebt, AMT_CreditSumOverdue, AMT_CreditMaxOverdue,
            CNT_CreditProlong, AMT_Annuity, DaysCreditUpdate, ETL_RunID
        )
        SELECT
            bl.BureauLoanID, bl.ClientID, bl.CreditActive, bl.DaysCredit, bl.DaysCreditEndDate,
            bl.AMT_CreditSum, bl.AMT_CreditSumDebt, bl.AMT_CreditSumOverdue, bl.AMT_CreditMaxOverdue,
            bl.CNT_CreditProlong, bl.AMT_Annuity, bl.DaysCreditUpdate, v_RunID
        FROM public.BureauLoans bl
        LEFT JOIN stg.BureauLoans_Delta ex
               ON ex.BureauLoanID = bl.BureauLoanID AND ex.ETL_RunID = v_RunID
        WHERE bl.CreatedAt > v_WM_BureauLoans
          AND ex.BureauLoanID IS NULL;

        GET DIAGNOSTICS v_BurLoanRows = ROW_COUNT;
        SELECT COALESCE(MAX(bl.CreatedAt), v_WM_BureauLoans) INTO v_NewWM_BureauLoans
        FROM public.BureauLoans bl
        INNER JOIN stg.BureauLoans_Delta bd ON bd.BureauLoanID = bl.BureauLoanID AND bd.ETL_RunID = v_RunID;

        -- ── Previous applications ─────────────────────────────────────────────
        INSERT INTO stg.PreviousApplications_Delta (
            PrevAppID, ClientID, ContractType, ContractStatus,
            AMT_Annuity, AMT_Application, AMT_Credit, AMT_DownPayment, AMT_GoodsPrice,
            DaysDecision, ETL_RunID
        )
        SELECT
            pa.PrevAppID, pa.ClientID, pa.ContractType, pa.ContractStatus,
            pa.AMT_Annuity, pa.AMT_Application, pa.AMT_Credit,
            pa.AMT_DownPayment, pa.AMT_GoodsPrice, pa.DaysDecision, v_RunID
        FROM public.PreviousApplications pa
        LEFT JOIN stg.PreviousApplications_Delta ex
               ON ex.PrevAppID = pa.PrevAppID AND ex.ETL_RunID = v_RunID
        WHERE pa.CreatedAt > v_WM_PrevApp
          AND ex.PrevAppID IS NULL;

        GET DIAGNOSTICS v_PrevAppRows = ROW_COUNT;
        SELECT COALESCE(MAX(pa.CreatedAt), v_WM_PrevApp) INTO v_NewWM_PrevApp
        FROM public.PreviousApplications pa
        INNER JOIN stg.PreviousApplications_Delta pd ON pd.PrevAppID = pa.PrevAppID AND pd.ETL_RunID = v_RunID;

        -- ── Installment payments ──────────────────────────────────────────────
        -- NOTE: No anti-join here — InstallmentsPayments has no reliable natural key
        -- per payment row (PrevAppID is not unique per row). Watermark-only guard.
        -- Rerun safety relies on the watermark not advancing until COMMIT.
        INSERT INTO stg.InstallmentsPayments_Delta (
            PrevAppID, ClientID, DaysInstalment, DaysEntryPayment, AMT_Instalment, AMT_Payment, ETL_RunID
        )
        SELECT
            ip.PrevAppID, ip.ClientID, ip.DaysInstalment, ip.DaysEntryPayment,
            ip.AMT_Instalment, ip.AMT_Payment, v_RunID
        FROM public.InstallmentsPayments ip
        WHERE ip.CreatedAt > v_WM_Inst;

        GET DIAGNOSTICS v_InstRows = ROW_COUNT;
        v_NewWM_Inst := GREATEST(v_WM_Inst,
            (v_ActualRunDate::TIMESTAMPTZ + INTERVAL '1 day' - INTERVAL '1 second'));

        v_RowsTotal := v_AppRows + v_ClientRows + v_TxnRows + v_BurRows
                     + v_BurLoanRows + v_PrevAppRows + v_InstRows;

        -- ── Advance all watermarks ────────────────────────────────────────────
        UPDATE stg.ETL_Control SET LastWatermark = v_NewWM_App,         LastRunID = v_RunID WHERE SourceTable = 'public.LoanApplications';
        UPDATE stg.ETL_Control SET LastWatermark = v_NewWM_Client,      LastRunID = v_RunID WHERE SourceTable = 'public.Clients';
        UPDATE stg.ETL_Control SET LastWatermark = v_NewWM_Txn,         LastRunID = v_RunID WHERE SourceTable = 'public.Transactions';
        UPDATE stg.ETL_Control SET LastWatermark = v_NewWM_Bureau,      LastRunID = v_RunID WHERE SourceTable = 'public.BureauSnapshots';
        UPDATE stg.ETL_Control SET LastWatermark = v_NewWM_BureauLoans, LastRunID = v_RunID WHERE SourceTable = 'public.BureauLoans';
        UPDATE stg.ETL_Control SET LastWatermark = v_NewWM_PrevApp,     LastRunID = v_RunID WHERE SourceTable = 'public.PreviousApplications';
        UPDATE stg.ETL_Control SET LastWatermark = v_NewWM_Inst,        LastRunID = v_RunID WHERE SourceTable = 'public.InstallmentsPayments';

        UPDATE stg.ETL_RunLog
        SET    Status        = 'success',
               RowsProcessed = v_RowsTotal,
               DurationSec   = EXTRACT(EPOCH FROM (NOW() - v_T0))::INT,
               FinishedAt    = NOW()
        WHERE  RunID = v_RunID;

        RAISE NOTICE
            'Staging complete  RunDate=%  Apps=%  Clients=%  Txns=%  Bureau=%  BurLoans=%  PrevApps=%  Inst=%',
            v_ActualRunDate,
            v_AppRows, v_ClientRows, v_TxnRows, v_BurRows,
            v_BurLoanRows, v_PrevAppRows, v_InstRows;

    EXCEPTION WHEN OTHERS THEN
        PERFORM stg.usp_LogAndRaiseError(v_RunID, v_StepName, SQLERRM);
        RAISE;
    END;
END;
$$ LANGUAGE plpgsql;


-- ============================================================
-- 5. PROCEDURE — stg.usp_ValidateStagedData
-- ============================================================
-- Eight data-quality checks against THIS RUN's staged rows.
-- CRITICAL failures halt the pipeline.
-- WARNING failures are logged but allow the pipeline to proceed
-- unless p_HaltOnWarning = TRUE.
--
-- [VAL-1] Application counts filtered by ETL_RunID so comparison is
--         new-rows-this-run vs. 30-day average of new-rows-per-run
--         (was previously cumulative total vs. per-run average).
-- [VAL-2] Transaction freshness check downgrades to WARNING for backfill
--         runs (p_RunDate < CURRENT_DATE - 1).
-- ============================================================
CREATE OR REPLACE FUNCTION stg.usp_ValidateStagedData(
    p_RunDate        DATE    DEFAULT NULL,
    p_HaltOnWarning  BOOLEAN DEFAULT FALSE
)
RETURNS VOID AS $$
DECLARE
    v_RunID         BIGINT;
    v_StagingRunID  BIGINT;
    v_BatchID       UUID        := gen_random_uuid();
    v_StepName      VARCHAR(50) := 'validation';
    v_T0            TIMESTAMP   := NOW();
    v_ActualRunDate DATE;
    v_IsBackfill    BOOLEAN;
    v_Errors        INT := 0;
    v_Warnings      INT := 0;
    v_AppCount      INT;
    v_TxnCount      INT;
    v_AvgAppCount   DECIMAL(18,2);
    v_NullAppID     INT;
    v_NullClientID  INT;
    v_BadExtScore   INT;
    v_OrphanClients INT;
    v_DupApps       INT;
    v_TotalProblems INT;
    v_SummaryMsg    TEXT;
BEGIN
    v_ActualRunDate := COALESCE(p_RunDate, CURRENT_DATE - 1);
    v_IsBackfill    := (v_ActualRunDate < CURRENT_DATE - 1);

    SELECT RunID INTO v_StagingRunID
    FROM   stg.ETL_RunLog
    WHERE  RunDate  = v_ActualRunDate AND StepName = 'staging' AND Status = 'success'
    ORDER  BY RunID DESC LIMIT 1;

    IF v_StagingRunID IS NULL THEN
        RAISE EXCEPTION 'No successful staging run for %. Run stg.usp_StageSourceData first.', v_ActualRunDate;
    END IF;

    INSERT INTO stg.ETL_RunLog (BatchID, RunDate, StepName, Status)
    VALUES (v_BatchID, v_ActualRunDate, v_StepName, 'running')
    RETURNING RunID INTO v_RunID;

    BEGIN
        -- CHECK 1: Application rows staged in THIS run (CRITICAL) [VAL-1]
        SELECT COUNT(*) INTO v_AppCount
        FROM stg.Applications_Delta WHERE ETL_RunID = v_StagingRunID;

        IF v_AppCount = 0 THEN
            v_Errors := v_Errors + 1;
            INSERT INTO stg.ValidationErrors (RunID, RunDate, SourceTable, CheckName, Severity, FailedValue, ExpectedValue)
            VALUES (v_RunID, v_ActualRunDate, 'stg.Applications_Delta', 'ROW_COUNT', 'CRITICAL', '0 rows in this run', '> 0');
        END IF;

        -- CHECK 2: Transactions for v_ActualRunDate (WARNING for backfills) [VAL-2]
        SELECT COUNT(*) INTO v_TxnCount
        FROM stg.Transactions_Delta
        WHERE TxnDate = v_ActualRunDate AND ETL_RunID = v_StagingRunID;

        IF v_TxnCount = 0 THEN
            IF v_IsBackfill THEN
                v_Warnings := v_Warnings + 1;
                INSERT INTO stg.ValidationErrors (RunID, RunDate, SourceTable, CheckName, Severity, FailedValue, ExpectedValue)
                VALUES (v_RunID, v_ActualRunDate, 'stg.Transactions_Delta', 'FRESHNESS', 'WARNING',
                        '0 rows for ' || v_ActualRunDate::TEXT || ' (backfill run)', '> 0 rows');
            ELSE
                v_Errors := v_Errors + 1;
                INSERT INTO stg.ValidationErrors (RunID, RunDate, SourceTable, CheckName, Severity, FailedValue, ExpectedValue)
                VALUES (v_RunID, v_ActualRunDate, 'stg.Transactions_Delta', 'FRESHNESS', 'CRITICAL',
                        '0 rows for ' || v_ActualRunDate::TEXT, '> 0 rows');
            END IF;
        END IF;

        -- CHECK 3: Volume anomaly — ±40% of 30-day per-run average [VAL-1]
        SELECT AVG(sub.run_app_rows) INTO v_AvgAppCount
        FROM (
            SELECT COUNT(ad.ApplicationID) AS run_app_rows
            FROM   stg.ETL_RunLog          rl
            LEFT   JOIN stg.Applications_Delta ad ON ad.ETL_RunID = rl.RunID
            WHERE  rl.StepName = 'staging' AND rl.Status = 'success'
            AND    rl.RunDate  < v_ActualRunDate
            GROUP  BY rl.RunID
            ORDER  BY MAX(rl.RunDate) DESC
            LIMIT  30
        ) sub;

        IF v_AvgAppCount IS NOT NULL AND v_AvgAppCount > 0
        AND ABS(v_AppCount - v_AvgAppCount) / v_AvgAppCount > 0.40
        THEN
            v_Warnings := v_Warnings + 1;
            INSERT INTO stg.ValidationErrors (RunID, RunDate, SourceTable, CheckName, Severity, FailedValue, ExpectedValue)
            VALUES (v_RunID, v_ActualRunDate, 'stg.Applications_Delta', 'VOLUME_ANOMALY', 'WARNING',
                    v_AppCount::TEXT, 'Within 40% of 30-day avg=' || round(v_AvgAppCount)::TEXT);
        END IF;

        -- CHECK 4: NULL ApplicationID in this run (CRITICAL)
        SELECT COUNT(*) INTO v_NullAppID
        FROM stg.Applications_Delta WHERE ApplicationID IS NULL AND ETL_RunID = v_StagingRunID;

        IF v_NullAppID > 0 THEN
            v_Errors := v_Errors + 1;
            INSERT INTO stg.ValidationErrors (RunID, RunDate, SourceTable, CheckName, Severity, FailedValue, ExpectedValue)
            VALUES (v_RunID, v_ActualRunDate, 'stg.Applications_Delta', 'NULL_CHECK', 'CRITICAL',
                    v_NullAppID || ' NULL ApplicationIDs', '0 NULLs');
        END IF;

        -- CHECK 5: NULL ClientID in this run (CRITICAL)
        SELECT COUNT(*) INTO v_NullClientID
        FROM stg.Applications_Delta WHERE ClientID IS NULL AND ETL_RunID = v_StagingRunID;

        IF v_NullClientID > 0 THEN
            v_Errors := v_Errors + 1;
            INSERT INTO stg.ValidationErrors (RunID, RunDate, SourceTable, CheckName, Severity, FailedValue, ExpectedValue)
            VALUES (v_RunID, v_ActualRunDate, 'stg.Applications_Delta', 'NULL_CHECK', 'CRITICAL',
                    v_NullClientID || ' NULL ClientIDs', '0 NULLs');
        END IF;

        -- CHECK 6: ExtScore range [0,1] in bureau rows (WARNING)
        SELECT COUNT(*) INTO v_BadExtScore
        FROM stg.Bureau_Delta
        WHERE ETL_RunID = v_StagingRunID
        AND (
            (ExtScore1 IS NOT NULL AND (ExtScore1 < 0 OR ExtScore1 > 1))
         OR (ExtScore2 IS NOT NULL AND (ExtScore2 < 0 OR ExtScore2 > 1))
         OR (ExtScore3 IS NOT NULL AND (ExtScore3 < 0 OR ExtScore3 > 1))
        );

        IF v_BadExtScore > 0 THEN
            v_Warnings := v_Warnings + 1;
            INSERT INTO stg.ValidationErrors (RunID, RunDate, SourceTable, CheckName, Severity, FailedValue, ExpectedValue)
            VALUES (v_RunID, v_ActualRunDate, 'stg.Bureau_Delta', 'RANGE_CHECK', 'WARNING',
                    v_BadExtScore || ' rows with ExtScore outside [0,1]', 'All ExtScores in [0,1]');
        END IF;

        -- CHECK 7: Orphaned ClientIDs in this run (CRITICAL)
        SELECT COUNT(DISTINCT a.ClientID) INTO v_OrphanClients
        FROM stg.Applications_Delta a
        WHERE a.ETL_RunID = v_StagingRunID
        AND NOT EXISTS (SELECT 1 FROM public.Clients c WHERE c.ClientID = a.ClientID);

        IF v_OrphanClients > 0 THEN
            v_Errors := v_Errors + 1;
            INSERT INTO stg.ValidationErrors (RunID, RunDate, SourceTable, CheckName, Severity, FailedValue, ExpectedValue)
            VALUES (v_RunID, v_ActualRunDate, 'stg.Applications_Delta', 'REF_INTEGRITY', 'CRITICAL',
                    v_OrphanClients || ' ClientIDs not in public.Clients', '0 orphaned ClientIDs');
        END IF;

        -- CHECK 8: Duplicate ApplicationIDs within this run (WARNING)
        -- The composite PK prevents this via INSERT, but guards against direct-load bypasses.
        SELECT COUNT(*) INTO v_DupApps
        FROM (
            SELECT ApplicationID FROM stg.Applications_Delta
            WHERE ETL_RunID = v_StagingRunID
            GROUP BY ApplicationID HAVING COUNT(*) > 1
        ) d;

        IF v_DupApps > 0 THEN
            v_Warnings := v_Warnings + 1;
            INSERT INTO stg.ValidationErrors (RunID, RunDate, SourceTable, CheckName, Severity, FailedValue, ExpectedValue)
            VALUES (v_RunID, v_ActualRunDate, 'stg.Applications_Delta', 'DUPLICATE_KEY', 'WARNING',
                    v_DupApps || ' duplicate ApplicationIDs in this run', '0 duplicates');
        END IF;

        -- ── Halt or continue ───────────────────────────────────────────────
        v_TotalProblems := v_Errors + CASE WHEN p_HaltOnWarning THEN v_Warnings ELSE 0 END;
        v_SummaryMsg    := 'Validation  RunDate=' || v_ActualRunDate::TEXT
                        || '  CRITICAL=' || v_Errors || '  WARNING=' || v_Warnings;

        UPDATE stg.ETL_RunLog
        SET    Status        = CASE WHEN v_TotalProblems > 0 THEN 'failed' ELSE 'success' END,
               RowsProcessed = v_AppCount,
               DurationSec   = EXTRACT(EPOCH FROM (NOW() - v_T0))::INT,
               ErrorMessage  = CASE WHEN v_TotalProblems > 0 THEN v_SummaryMsg ELSE NULL END,
               FinishedAt    = NOW()
        WHERE  RunID = v_RunID;

        IF v_TotalProblems > 0 THEN RAISE EXCEPTION '%', v_SummaryMsg; END IF;
        RAISE NOTICE '%', v_SummaryMsg;

    EXCEPTION WHEN OTHERS THEN
        PERFORM stg.usp_LogAndRaiseError(v_RunID, v_StepName, SQLERRM);
        RAISE;
    END;
END;
$$ LANGUAGE plpgsql;


-- ============================================================
-- 6. PROCEDURE — ml.usp_BuildFeatureStoreRaw (138-feature version)
-- ============================================================
-- Heavy SQL aggregation producing one row per client.
-- Python ETL reads stg.FeatureStore_Raw and adds derived ratios,
-- then writes ml.FeatureStore.
--
-- [FEAT-1] Advisory lock prevents concurrent execution race.
-- [FEAT-2] Days_Employed carried through so Python can derive
--          EmployedYears without a conditional column check.
-- [FEAT-3] Bureau loan aggregations (bureau_loan_count, etc.)
-- [FEAT-4] Previous application aggregations (prev_app_count, etc.)
-- [FEAT-5] Installment aggregations (inst_count, inst_avg_payment, etc.)
-- [FEAT-6] POS cash aggregations (pos_dpd_avg, pos_dpd_max, etc.)
-- [FEAT-7] All 138-feature model raw columns carried through from
--          LoanApplications and Clients (ORGANIZATION_TYPE, FLAG_DOCUMENT_*,
--          AMT_REQ_CREDIT_BUREAU_*, etc.)
--
-- Bureau snapshot uses LATERAL join (most recent on or before
-- ApplicationDate) to enforce the temporal boundary and prevent leakage.
-- ============================================================
CREATE OR REPLACE FUNCTION ml.usp_BuildFeatureStoreRaw(
    p_RunDate    DATE DEFAULT NULL,
    p_WindowDays INT  DEFAULT 90
)
RETURNS VOID AS $$
DECLARE
    v_RunID         BIGINT;
    v_StagingRunID  BIGINT;
    v_BatchID       UUID        := gen_random_uuid();
    v_StepName      VARCHAR(50) := 'feature_build';
    v_T0            TIMESTAMP   := NOW();
    v_ActualRunDate DATE;
    v_WindowStart   DATE;
    v_FeatureRows   INT := 0;
BEGIN
    v_ActualRunDate := COALESCE(p_RunDate, CURRENT_DATE - 1);
    v_WindowStart   := v_ActualRunDate - p_WindowDays;

    SELECT RunID INTO v_StagingRunID
    FROM   stg.ETL_RunLog
    WHERE  RunDate = v_ActualRunDate AND StepName = 'staging' AND Status = 'success'
    ORDER  BY RunID DESC LIMIT 1;

    IF v_StagingRunID IS NULL THEN
        RAISE EXCEPTION 'No successful staging run for %. Run stg.usp_StageSourceData first.', v_ActualRunDate;
    END IF;

    -- [FEAT-1] Advisory lock prevents concurrent execution race on DELETE + INSERT
    PERFORM pg_advisory_xact_lock(hashtext('usp_BuildFeatureStoreRaw:' || v_ActualRunDate::TEXT));

    INSERT INTO stg.ETL_RunLog (BatchID, RunDate, StepName, Status, StartedAt)
    VALUES (v_BatchID, v_ActualRunDate, v_StepName, 'running', NOW())
    RETURNING RunID INTO v_RunID;

    BEGIN
        -- Idempotent: delete today's rows before rebuilding
        DELETE FROM stg.FeatureStore_Raw WHERE AsOfDate = v_ActualRunDate;

        WITH

        txn_agg AS (
            SELECT
                ClientID,
                AVG(Balance)                                                    AS AvgBalance_90d,
                MIN(Balance)                                                    AS MinBalance_90d,
                MAX(Balance)                                                    AS MaxBalance_90d,
                SUM(CASE WHEN TxnType = 'SALARY' THEN 1 ELSE 0 END)            AS SalaryCredits_90d,
                SUM(CASE WHEN Balance < 0         THEN 1 ELSE 0 END)            AS OverdraftDays_90d,
                COUNT(*)                                                        AS TxnCount_90d,
                AVG(Amount)                                                     AS AvgTxnAmount_90d,
                SUM(CASE WHEN TxnType = 'ATM'     THEN 1 ELSE 0 END)            AS CashWithdraws_90d
            FROM public.Transactions
            WHERE TxnDate BETWEEN v_WindowStart AND v_ActualRunDate
            GROUP BY ClientID
        ),

        -- [FEAT-3] Bureau tradeline aggregations
        bureau_agg AS (
            SELECT
                ClientID,
                COUNT(*)                                                        AS bureau_loan_count,
                SUM(CASE WHEN CreditActive = 'Active'   THEN 1 ELSE 0 END)     AS bureau_active_count,
                SUM(CASE WHEN CreditActive = 'Closed'   THEN 1 ELSE 0 END)     AS bureau_closed_count,
                SUM(CASE WHEN CreditActive = 'Bad debt' THEN 1 ELSE 0 END)     AS bureau_bad_debt_count,
                AVG(DaysCredit)                                                 AS bureau_avg_days_credit,
                AVG(DaysCreditEndDate)                                          AS bureau_avg_days_enddate,
                SUM(COALESCE(AMT_CreditSumDebt,    0))                          AS bureau_total_debt,
                SUM(COALESCE(AMT_CreditSum,         0))                          AS bureau_total_limit,
                SUM(COALESCE(AMT_CreditSumOverdue,  0))                          AS bureau_total_overdue,
                MAX(COALESCE(AMT_CreditMaxOverdue,  0))                          AS bureau_max_overdue,
                SUM(COALESCE(CNT_CreditProlong,     0))                          AS bureau_prolong_count,
                MAX(DaysCredit)                                                 AS bureau_last_credit,
                MIN(DaysCredit)                                                 AS bureau_first_credit,
                MAX(DaysCredit) - MIN(DaysCredit)                               AS bureau_credit_span,
                STDDEV(DaysCredit)                                              AS bureau_days_credit_std,
                STDDEV(AMT_CreditSumDebt)                                       AS bureau_debt_std
            FROM public.BureauLoans
            GROUP BY ClientID
        ),

        -- [FEAT-4] Previous application aggregations
        prev_agg AS (
            SELECT
                ClientID,
                COUNT(*)                                                        AS prev_app_count,
                SUM(CASE WHEN ContractStatus = 'Approved'     THEN 1 ELSE 0 END) AS prev_approved_count,
                SUM(CASE WHEN ContractStatus = 'Refused'      THEN 1 ELSE 0 END) AS prev_refused_count,
                AVG(AMT_Credit)                                                 AS prev_avg_credit,
                AVG(AMT_Annuity)                                                AS prev_avg_annuity,
                AVG(AMT_DownPayment)                                            AS prev_avg_down_payment,
                AVG(AMT_GoodsPrice)                                             AS prev_avg_goods_price,
                MAX(DaysDecision)                                               AS prev_max_days_decision,
                AVG(DaysDecision)                                               AS prev_avg_days_decision,
                SUM(CASE WHEN ContractType = 'Consumer loans'  THEN 1 ELSE 0 END) AS prev_consumer_count,
                SUM(CASE WHEN ContractType = 'Cash loans'      THEN 1 ELSE 0 END) AS prev_cash_count,
                SUM(CASE WHEN ContractType = 'Revolving loans' THEN 1 ELSE 0 END) AS prev_revolving_count,
                MIN(DaysDecision)                                               AS prev_first_app,
                MAX(DaysDecision) - MIN(DaysDecision)                          AS prev_app_span,
                STDDEV(DaysDecision)                                            AS prev_days_std,
                STDDEV(AMT_Credit)                                              AS prev_credit_std
            FROM public.PreviousApplications
            GROUP BY ClientID
        ),

        -- [FEAT-5] Installment aggregations
        inst_agg AS (
            SELECT
                ClientID,
                COUNT(*)                                                        AS inst_count,
                AVG(AMT_Payment)                                                AS inst_avg_payment,
                MAX(AMT_Payment)                                                AS inst_max_payment,
                MIN(AMT_Payment)                                                AS inst_min_payment,
                AVG(DaysEntryPayment)                                           AS inst_avg_days_delay,
                MAX(DaysEntryPayment)                                           AS inst_max_days_delay
            FROM public.InstallmentsPayments
            GROUP BY ClientID
        ),

        -- [FEAT-6] POS cash aggregations
        pos_agg AS (
            SELECT
                ClientID,
                COUNT(*)                                                        AS pos_count,
                AVG(MonthsBalance)                                              AS pos_months_avg,
                AVG(SK_DPD)                                                     AS pos_dpd_avg,
                MAX(SK_DPD)                                                     AS pos_dpd_max,
                AVG(SK_DPD_DEF)                                                 AS pos_dpd_def_avg,
                SUM(CASE WHEN NAME_Contract_Status = 'Completed' THEN 1 ELSE 0 END) AS pos_completed
            FROM public.POS_CashBalance
            GROUP BY ClientID
        )

        INSERT INTO stg.FeatureStore_Raw (
            ClientID, AsOfDate,
            -- Transaction aggregates (90-day window)
            AvgBalance_90d, MinBalance_90d, MaxBalance_90d,
            SalaryCredits_90d, OverdraftDays_90d, TxnCount_90d,
            AvgTxnAmount_90d, CashWithdraws_90d,
            -- Client master + [FEAT-2] Days_Employed
            BirthDate, Gender, EducationType, FamilyStatus,
            IncomeTotal, IncomeType, OwnCar, OwnRealty,
            CNTChildren, CNTFamilyMembers, RegionRating, Days_Employed,
            -- [FEAT-7] Extended client columns
            NAME_HOUSING_TYPE, REGION_POPULATION_RELATIVE,
            DAYS_REGISTRATION, DAYS_ID_PUBLISH,
            FLAG_MOBIL, FLAG_WORK_PHONE, FLAG_CONT_MOBILE, FLAG_PHONE, FLAG_EMAIL,
            OCCUPATION_TYPE, ORGANIZATION_TYPE,
            REG_REGION_NOT_LIVE_REGION, REG_REGION_NOT_WORK_REGION,
            LIVE_REGION_NOT_WORK_REGION, REG_CITY_NOT_LIVE_CITY,
            REG_CITY_NOT_WORK_CITY, LIVE_CITY_NOT_WORK_CITY,
            YEARS_BEGINEXPLUATATION_AVG, FLOORSMAX_AVG, TOTALAREA_MODE,
            EMERGENCYSTATE_MODE, OBS_30_CNT_SOCIAL_CIRCLE,
            DEF_30_CNT_SOCIAL_CIRCLE, DEF_60_CNT_SOCIAL_CIRCLE,
            -- Latest application
            ApplicationID, ApplicationDate, LoanAmount, Annuity,
            LoanTenorMonths, GoodsPriceAmount, ContractType,
            -- [FEAT-7] Extended application columns
            NAME_TYPE_SUITE, WEEKDAY_APPR_PROCESS_START, HOUR_APPR_PROCESS_START,
            DAYS_LAST_PHONE_CHANGE,
            FLAG_DOCUMENT_2,  FLAG_DOCUMENT_3,  FLAG_DOCUMENT_4,  FLAG_DOCUMENT_5,
            FLAG_DOCUMENT_6,  FLAG_DOCUMENT_7,  FLAG_DOCUMENT_8,  FLAG_DOCUMENT_9,
            FLAG_DOCUMENT_10, FLAG_DOCUMENT_11, FLAG_DOCUMENT_12, FLAG_DOCUMENT_13,
            FLAG_DOCUMENT_14, FLAG_DOCUMENT_15, FLAG_DOCUMENT_16, FLAG_DOCUMENT_17,
            FLAG_DOCUMENT_18, FLAG_DOCUMENT_19, FLAG_DOCUMENT_20, FLAG_DOCUMENT_21,
            AMT_REQ_CREDIT_BUREAU_HOUR, AMT_REQ_CREDIT_BUREAU_DAY,
            AMT_REQ_CREDIT_BUREAU_WEEK, AMT_REQ_CREDIT_BUREAU_MON,
            AMT_REQ_CREDIT_BUREAU_QRT,  AMT_REQ_CREDIT_BUREAU_YEAR,
            -- Bureau snapshot (ext scores)
            ExtScore1, ExtScore2, ExtScore3, ActiveLoans, ClosedLoans, EnquiriesYear,
            -- [FEAT-3] Bureau loan aggregations
            bureau_loan_count, bureau_active_count, bureau_closed_count,
            bureau_bad_debt_count, bureau_avg_days_credit, bureau_avg_days_enddate,
            bureau_total_debt, bureau_total_limit, bureau_total_overdue,
            bureau_max_overdue, bureau_prolong_count,
            bureau_last_credit, bureau_first_credit, bureau_credit_span,
            bureau_days_credit_std, bureau_debt_std,
            -- [FEAT-4] Previous application aggregations
            prev_app_count, prev_approved_count, prev_refused_count,
            prev_avg_credit, prev_avg_annuity, prev_avg_down_payment,
            prev_avg_goods_price, prev_max_days_decision, prev_avg_days_decision,
            prev_consumer_count, prev_cash_count, prev_revolving_count,
            prev_first_app, prev_app_span, prev_days_std, prev_credit_std,
            -- [FEAT-5] Installment aggregations
            inst_count, inst_avg_payment, inst_max_payment, inst_min_payment,
            inst_avg_days_delay, inst_max_days_delay,
            -- [FEAT-6] POS cash aggregations
            pos_count, pos_months_avg, pos_dpd_avg, pos_dpd_max,
            pos_dpd_def_avg, pos_completed,
            -- Audit
            ETL_RunID
        )
        SELECT
            a.ClientID,
            v_ActualRunDate                             AS AsOfDate,
            -- Transactions (from CTE)
            t.AvgBalance_90d,    t.MinBalance_90d,    t.MaxBalance_90d,
            t.SalaryCredits_90d, t.OverdraftDays_90d, t.TxnCount_90d,
            t.AvgTxnAmount_90d,  t.CashWithdraws_90d,
            -- Client master
            c.BirthDate, c.Gender, c.EducationType, c.FamilyStatus,
            c.IncomeTotal, c.IncomeType, c.OwnCar, c.OwnRealty,
            c.CNTChildren, c.CNTFamilyMembers, c.RegionRating, c.Days_Employed,
            -- Extended client columns [FEAT-7]
            c.NAME_HOUSING_TYPE, c.REGION_POPULATION_RELATIVE,
            c.DAYS_REGISTRATION, c.DAYS_ID_PUBLISH,
            c.FLAG_MOBIL, c.FLAG_WORK_PHONE, c.FLAG_CONT_MOBILE,
            c.FLAG_PHONE, c.FLAG_EMAIL,
            c.OCCUPATION_TYPE, c.ORGANIZATION_TYPE,
            c.REG_REGION_NOT_LIVE_REGION, c.REG_REGION_NOT_WORK_REGION,
            c.LIVE_REGION_NOT_WORK_REGION, c.REG_CITY_NOT_LIVE_CITY,
            c.REG_CITY_NOT_WORK_CITY, c.LIVE_CITY_NOT_WORK_CITY,
            c.YEARS_BEGINEXPLUATATION_AVG, c.FLOORSMAX_AVG, c.TOTALAREA_MODE,
            c.EMERGENCYSTATE_MODE, c.OBS_30_CNT_SOCIAL_CIRCLE,
            c.DEF_30_CNT_SOCIAL_CIRCLE, c.DEF_60_CNT_SOCIAL_CIRCLE,
            -- Latest application
            la.ApplicationID, la.ApplicationDate, la.LoanAmount, la.Annuity,
            la.LoanTenorMonths, la.GoodsPriceAmount, la.ContractType,
            -- Extended application columns [FEAT-7]
            la.NAME_TYPE_SUITE, la.WEEKDAY_APPR_PROCESS_START,
            la.HOUR_APPR_PROCESS_START, la.DAYS_LAST_PHONE_CHANGE,
            la.FLAG_DOCUMENT_2,  la.FLAG_DOCUMENT_3,  la.FLAG_DOCUMENT_4,
            la.FLAG_DOCUMENT_5,  la.FLAG_DOCUMENT_6,  la.FLAG_DOCUMENT_7,
            la.FLAG_DOCUMENT_8,  la.FLAG_DOCUMENT_9,  la.FLAG_DOCUMENT_10,
            la.FLAG_DOCUMENT_11, la.FLAG_DOCUMENT_12, la.FLAG_DOCUMENT_13,
            la.FLAG_DOCUMENT_14, la.FLAG_DOCUMENT_15, la.FLAG_DOCUMENT_16,
            la.FLAG_DOCUMENT_17, la.FLAG_DOCUMENT_18, la.FLAG_DOCUMENT_19,
            la.FLAG_DOCUMENT_20, la.FLAG_DOCUMENT_21,
            la.AMT_REQ_CREDIT_BUREAU_HOUR, la.AMT_REQ_CREDIT_BUREAU_DAY,
            la.AMT_REQ_CREDIT_BUREAU_WEEK, la.AMT_REQ_CREDIT_BUREAU_MON,
            la.AMT_REQ_CREDIT_BUREAU_QRT,  la.AMT_REQ_CREDIT_BUREAU_YEAR,
            -- Bureau snapshot (LATERAL: most recent on or before ApplicationDate)
            bs.ExtScore1, bs.ExtScore2, bs.ExtScore3,
            bs.ActiveLoans, bs.ClosedLoans, bs.EnquiriesYear,
            -- Bureau loan aggregations (from CTE)
            ba.bureau_loan_count,      ba.bureau_active_count,
            ba.bureau_closed_count,    ba.bureau_bad_debt_count,
            ba.bureau_avg_days_credit, ba.bureau_avg_days_enddate,
            ba.bureau_total_debt,      ba.bureau_total_limit,
            ba.bureau_total_overdue,   ba.bureau_max_overdue,
            ba.bureau_prolong_count,
            ba.bureau_last_credit,     ba.bureau_first_credit,
            ba.bureau_credit_span,     ba.bureau_days_credit_std,
            ba.bureau_debt_std,
            -- Previous application aggregations (from CTE)
            pa.prev_app_count,         pa.prev_approved_count,
            pa.prev_refused_count,     pa.prev_avg_credit,
            pa.prev_avg_annuity,       pa.prev_avg_down_payment,
            pa.prev_avg_goods_price,   pa.prev_max_days_decision,
            pa.prev_avg_days_decision, pa.prev_consumer_count,
            pa.prev_cash_count,        pa.prev_revolving_count,
            pa.prev_first_app,         pa.prev_app_span,
            pa.prev_days_std,          pa.prev_credit_std,
            -- Installment aggregations (from CTE)
            ia.inst_count,          ia.inst_avg_payment,
            ia.inst_max_payment,    ia.inst_min_payment,
            ia.inst_avg_days_delay, ia.inst_max_days_delay,
            -- POS cash aggregations (from CTE)
            po.pos_count,       po.pos_months_avg,
            po.pos_dpd_avg,     po.pos_dpd_max,
            po.pos_dpd_def_avg, po.pos_completed,
            -- Audit
            v_RunID

        FROM (
            -- One row per client: most recent application as of run date
            SELECT DISTINCT ON (ClientID)
                ClientID, ApplicationID
            FROM public.LoanApplications
            WHERE ApplicationDate <= v_ActualRunDate
            ORDER BY ClientID, ApplicationDate DESC
        ) a

        JOIN public.Clients          c   ON c.ClientID      = a.ClientID
        JOIN public.LoanApplications la  ON la.ApplicationID = a.ApplicationID

        -- LATERAL: most recent bureau snapshot on or before ApplicationDate
        -- (temporal boundary — prevents leakage from post-application bureau data)
        LEFT JOIN LATERAL (
            SELECT ExtScore1, ExtScore2, ExtScore3, ActiveLoans, ClosedLoans, EnquiriesYear
            FROM   public.BureauSnapshots
            WHERE  ClientID     = a.ClientID
            AND    SnapshotDate <= la.ApplicationDate
            ORDER  BY SnapshotDate DESC
            LIMIT  1
        ) bs ON TRUE

        LEFT JOIN txn_agg    t   ON t.ClientID  = a.ClientID
        LEFT JOIN bureau_agg ba  ON ba.ClientID = a.ClientID
        LEFT JOIN prev_agg   pa  ON pa.ClientID = a.ClientID
        LEFT JOIN inst_agg   ia  ON ia.ClientID = a.ClientID
        LEFT JOIN pos_agg    po  ON po.ClientID = a.ClientID

        GROUP BY
            a.ClientID,
            c.BirthDate, c.Gender, c.EducationType, c.FamilyStatus,
            c.IncomeTotal, c.IncomeType, c.OwnCar, c.OwnRealty,
            c.CNTChildren, c.CNTFamilyMembers, c.RegionRating, c.Days_Employed,
            c.NAME_HOUSING_TYPE, c.REGION_POPULATION_RELATIVE,
            c.DAYS_REGISTRATION, c.DAYS_ID_PUBLISH,
            c.FLAG_MOBIL, c.FLAG_WORK_PHONE, c.FLAG_CONT_MOBILE,
            c.FLAG_PHONE, c.FLAG_EMAIL,
            c.OCCUPATION_TYPE, c.ORGANIZATION_TYPE,
            c.REG_REGION_NOT_LIVE_REGION, c.REG_REGION_NOT_WORK_REGION,
            c.LIVE_REGION_NOT_WORK_REGION, c.REG_CITY_NOT_LIVE_CITY,
            c.REG_CITY_NOT_WORK_CITY, c.LIVE_CITY_NOT_WORK_CITY,
            c.YEARS_BEGINEXPLUATATION_AVG, c.FLOORSMAX_AVG, c.TOTALAREA_MODE,
            c.EMERGENCYSTATE_MODE, c.OBS_30_CNT_SOCIAL_CIRCLE,
            c.DEF_30_CNT_SOCIAL_CIRCLE, c.DEF_60_CNT_SOCIAL_CIRCLE,
            la.ApplicationID, la.ApplicationDate, la.LoanAmount, la.Annuity,
            la.LoanTenorMonths, la.GoodsPriceAmount, la.ContractType,
            la.NAME_TYPE_SUITE, la.WEEKDAY_APPR_PROCESS_START,
            la.HOUR_APPR_PROCESS_START, la.DAYS_LAST_PHONE_CHANGE,
            la.FLAG_DOCUMENT_2,  la.FLAG_DOCUMENT_3,  la.FLAG_DOCUMENT_4,
            la.FLAG_DOCUMENT_5,  la.FLAG_DOCUMENT_6,  la.FLAG_DOCUMENT_7,
            la.FLAG_DOCUMENT_8,  la.FLAG_DOCUMENT_9,  la.FLAG_DOCUMENT_10,
            la.FLAG_DOCUMENT_11, la.FLAG_DOCUMENT_12, la.FLAG_DOCUMENT_13,
            la.FLAG_DOCUMENT_14, la.FLAG_DOCUMENT_15, la.FLAG_DOCUMENT_16,
            la.FLAG_DOCUMENT_17, la.FLAG_DOCUMENT_18, la.FLAG_DOCUMENT_19,
            la.FLAG_DOCUMENT_20, la.FLAG_DOCUMENT_21,
            la.AMT_REQ_CREDIT_BUREAU_HOUR, la.AMT_REQ_CREDIT_BUREAU_DAY,
            la.AMT_REQ_CREDIT_BUREAU_WEEK, la.AMT_REQ_CREDIT_BUREAU_MON,
            la.AMT_REQ_CREDIT_BUREAU_QRT,  la.AMT_REQ_CREDIT_BUREAU_YEAR,
            bs.ExtScore1, bs.ExtScore2, bs.ExtScore3,
            bs.ActiveLoans, bs.ClosedLoans, bs.EnquiriesYear,
            t.AvgBalance_90d, t.MinBalance_90d, t.MaxBalance_90d,
            t.SalaryCredits_90d, t.OverdraftDays_90d, t.TxnCount_90d,
            t.AvgTxnAmount_90d, t.CashWithdraws_90d,
            ba.bureau_loan_count, ba.bureau_active_count,
            ba.bureau_closed_count, ba.bureau_bad_debt_count,
            ba.bureau_avg_days_credit, ba.bureau_avg_days_enddate,
            ba.bureau_total_debt, ba.bureau_total_limit,
            ba.bureau_total_overdue, ba.bureau_max_overdue,
            ba.bureau_prolong_count, ba.bureau_last_credit,
            ba.bureau_first_credit, ba.bureau_credit_span,
            ba.bureau_days_credit_std, ba.bureau_debt_std,
            pa.prev_app_count, pa.prev_approved_count, pa.prev_refused_count,
            pa.prev_avg_credit, pa.prev_avg_annuity, pa.prev_avg_down_payment,
            pa.prev_avg_goods_price, pa.prev_max_days_decision,
            pa.prev_avg_days_decision, pa.prev_consumer_count,
            pa.prev_cash_count, pa.prev_revolving_count,
            pa.prev_first_app, pa.prev_app_span, pa.prev_days_std, pa.prev_credit_std,
            ia.inst_count, ia.inst_avg_payment, ia.inst_max_payment,
            ia.inst_min_payment, ia.inst_avg_days_delay, ia.inst_max_days_delay,
            po.pos_count, po.pos_months_avg, po.pos_dpd_avg, po.pos_dpd_max,
            po.pos_dpd_def_avg, po.pos_completed;

        GET DIAGNOSTICS v_FeatureRows = ROW_COUNT;

        IF v_FeatureRows = 0 THEN
            PERFORM stg.usp_LogAndRaiseError(
                v_RunID, v_StepName,
                'usp_BuildFeatureStoreRaw produced 0 rows — check staging tables'
            );
            RETURN;
        END IF;

        UPDATE stg.ETL_RunLog
        SET    Status        = 'success',
               RowsProcessed = v_FeatureRows,
               DurationSec   = EXTRACT(EPOCH FROM (NOW() - v_T0))::INT,
               FinishedAt    = NOW()
        WHERE  RunID = v_RunID;

        RAISE NOTICE
            'Feature store raw complete  RunDate=%  Rows=%  Window=% days (% – %)',
            v_ActualRunDate, v_FeatureRows, p_WindowDays, v_WindowStart, v_ActualRunDate;

    EXCEPTION WHEN OTHERS THEN
        PERFORM stg.usp_LogAndRaiseError(v_RunID, v_StepName, SQLERRM);
        RAISE;
    END;
END;
$$ LANGUAGE plpgsql;


-- ============================================================
-- 7. PROCEDURE — rpt.usp_RefreshReportingCache
-- ============================================================
-- Runs after the Python scoring step (pg_cron Step 5).
-- Sends a PSI alert notification if status is MONITOR or RETRAIN.
--
-- [RPT-1] ANALYZE calls removed from this procedure.
--         ANALYZE acquires ShareUpdateExclusiveLock and cannot safely run
--         inside a plpgsql function that also writes to the same tables.
--         Schedule ANALYZE separately via pg_cron:
--
--   SELECT cron.schedule(
--       'credit_risk_analyze_nightly',
--       '30 2 * * *',   -- 30 min after ETL
--       $$ ANALYZE public.LoanApplications, public.Clients,
--                  ml.PD_Scores, ml.SHAP_Values,
--                  ml.PSI_Monitor, ml.ModelRegistry; $$
--   );
-- ============================================================
CREATE OR REPLACE FUNCTION rpt.usp_RefreshReportingCache(
    p_RunDate DATE DEFAULT NULL
)
RETURNS VOID AS $$
DECLARE
    v_RunID         BIGINT;
    v_StepName      VARCHAR(50) := 'reporting';
    v_T0            TIMESTAMP   := NOW();
    v_ActualRunDate DATE;
    v_PSIStatus     VARCHAR(10);
    v_PSIScore      DECIMAL(8,4);
    v_ModelVer      VARCHAR(30);
    v_AlertMsg      TEXT;
BEGIN
    v_ActualRunDate := COALESCE(p_RunDate, CURRENT_DATE - 1);

    INSERT INTO stg.ETL_RunLog (RunDate, StepName, Status)
    VALUES (v_ActualRunDate, v_StepName, 'running')
    RETURNING RunID INTO v_RunID;

    BEGIN
        -- Check PSI status for today
        SELECT PSI_Status, PSI_Score, ModelVersion
        INTO   v_PSIStatus, v_PSIScore, v_ModelVer
        FROM   ml.PSI_Monitor
        WHERE  MonitorDate = v_ActualRunDate
        ORDER  BY MonitorID DESC LIMIT 1;

        IF v_PSIStatus IN ('MONITOR','RETRAIN') THEN
            v_AlertMsg :=
                '[Credit Risk PD] PSI Alert — '   || v_PSIStatus      ||
                ' | Score='     || v_PSIScore                          ||
                ' | Model='     || v_ModelVer                          ||
                ' | Date='      || v_ActualRunDate                     ||
                E'\n' ||
                'PSI thresholds: MONITOR > 0.10  |  RETRAIN > 0.25'   ||
                E'\n' ||
                CASE WHEN v_PSIStatus = 'RETRAIN'
                     THEN 'ACTION REQUIRED: Trigger quarterly retraining immediately.'
                     ELSE 'ACTION: Monitor closely. Retraining not yet required.'
                END ||
                E'\n\nReview rpt.vw_PSITrend in Power BI for trend details.';

            -- Option A: pg_notify (consumed by an application-level listener)
            PERFORM pg_notify('credit_risk_psi_alert', v_AlertMsg);
            -- Option B: pg_sendmail extension (uncomment if installed)
            -- PERFORM pg_sendmail('model-risk@yourbank.al',
            --     '[Credit Risk PD] PSI Alert — ' || v_PSIStatus, v_AlertMsg);

            RAISE NOTICE 'PSI ALERT: %', v_AlertMsg;

            UPDATE ml.PSI_Monitor
            SET    AlertSent   = TRUE, AlertSentAt = NOW()
            WHERE  MonitorDate = v_ActualRunDate AND AlertSent = FALSE;
        END IF;

        -- PostgreSQL forbids CONCURRENTLY until a materialized view has been populated once.
        IF EXISTS (
            SELECT 1 FROM pg_matviews
            WHERE schemaname = 'rpt' AND matviewname = 'vw_applicationdetail'
              AND NOT ispopulated
        ) THEN
            REFRESH MATERIALIZED VIEW rpt.vw_ApplicationDetail;
        ELSE
            REFRESH MATERIALIZED VIEW CONCURRENTLY rpt.vw_ApplicationDetail;
        END IF;

        UPDATE stg.ETL_RunLog
        SET    Status      = 'success',
               DurationSec = EXTRACT(EPOCH FROM (NOW() - v_T0))::INT,
               FinishedAt  = NOW()
        WHERE  RunID = v_RunID;

        RAISE NOTICE 'Reporting refresh complete  RunDate=%  PSIStatus=%',
            v_ActualRunDate, COALESCE(v_PSIStatus, 'none');

    EXCEPTION WHEN OTHERS THEN
        PERFORM stg.usp_LogAndRaiseError(v_RunID, v_StepName, SQLERRM);
        RAISE;
    END;
END;
$$ LANGUAGE plpgsql;


-- ============================================================
-- SANITY CHECK — run after deployment
-- ============================================================
SELECT n.nspname AS SchemaName, p.proname AS FunctionName
FROM   pg_proc p
JOIN   pg_namespace n ON p.pronamespace = n.oid
WHERE  n.nspname IN ('stg','ml','rpt') AND p.proname LIKE 'usp_%'
ORDER  BY n.nspname, p.proname;

SELECT schemaname, viewname FROM pg_views
WHERE schemaname = 'stg' AND viewname LIKE 'v_%'
ORDER BY viewname;

SELECT schemaname, tablename FROM pg_tables
WHERE schemaname = 'stg' AND tablename LIKE '%_Delta'
ORDER BY tablename;

-- ============================================================
-- QUICK MANUAL TEST (run each line separately in psql)
-- ============================================================
-- SELECT stg.usp_StageSourceData('2026-04-14');
-- SELECT stg.usp_ValidateStagedData('2026-04-14');
-- SELECT ml.usp_BuildFeatureStoreRaw('2026-04-14');
-- SELECT rpt.usp_RefreshReportingCache('2026-04-14');
--
-- SELECT * FROM stg.ETL_RunLog       ORDER BY RunID DESC LIMIT 20;
-- SELECT * FROM stg.ValidationErrors ORDER BY ErrorID DESC LIMIT 20;
-- SELECT * FROM stg.FeatureStore_Raw WHERE AsOfDate = '2026-04-14' LIMIT 5;
-- SELECT * FROM stg.v_NPLFeatures    LIMIT 5;
-- ============================================================
-- END OF STORED PROCEDURES
-- ============================================================

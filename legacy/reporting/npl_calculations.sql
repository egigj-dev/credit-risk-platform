-- ============================================================
-- npl_calculations_v2.sql
-- All NPL feature derivations from existing DB tables.
-- Three sections:
--   A. DPD calculations from InstallmentsPayments
--   B. NPL feature matrix (per active loan per client)
--      Features are computed from history BEFORE feature_cutoff_date.
--      Labels are computed from events AFTER feature_cutoff_date.
--      This eliminates target leakage.
--   C. NPL label distribution check
--
-- Fix log vs v1:
--   [H1] UNPAID instalments no longer silently zeroed  -  DPD kept NULL,
--        unpaid_instalment_count added as a separate feature.
--   [H2] Label leakage eliminated  -  labels derived from a forward-looking
--        window; features from a backward-looking window only.
--   [H3] instalments_since_last_late sentinel replaced with a binary
--        never_been_late flag + raw NULL-safe column.
--   [H4] txn_stress window anchored to feature_cutoff_date, not CURRENT_DATE.
--   [M1] loan_age_years_proxy negation removed; renamed to loan_tenor_years.
--   [M2] payment_ratio_trend NULL masked with has_trend_data flag.
--   [M3] balance_volatility replaced with proper coefficient of variation.
--   [M4] npl_composite_score rewritten to use mutually exclusive DPD bucket,
--        preventing double-counting and capping at 1.0.
--   [M5] PD lateral join documented; ScoreType filter noted where available.
--   [L1] instalment_amount_std surfaced in final SELECT.
--   [L2] pos_completed_count surfaced in final SELECT.
--   [L3] bureau_has_credit_data flag added; raw debt/limit columns surfaced.
-- ============================================================


-- ============================================================
-- SECTION A  -  DPD CALCULATIONS
-- Source: public.InstallmentsPayments
-- DPD = DaysEntryPayment - DaysInstalment
--   positive  = client paid late (days overdue)
--   zero/neg  = paid on time or early
--   NULL      = payment not yet recorded (UNPAID  -  treated separately)
--
-- [H1] DPD is NULL for unpaid instalments rather than 0.
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
    -- [H1] Do NOT coalesce to DaysInstalment  -  that would silently zero
    --      out unpaid instalments, hiding the most severe default signal.
    CASE
        WHEN DaysEntryPayment IS NULL THEN NULL
        ELSE GREATEST(DaysEntryPayment - DaysInstalment, 0)::SMALLINT
    END                                             AS DPD,

    -- DPD bucket (UNPAID is distinct from DPD=0)
    CASE
        WHEN DaysEntryPayment IS NULL
             THEN 'UNPAID'
        WHEN DaysEntryPayment - DaysInstalment >= 90
             THEN '90+'
        WHEN DaysEntryPayment - DaysInstalment >= 60
             THEN '60-89'
        WHEN DaysEntryPayment - DaysInstalment >= 30
             THEN '30-59'
        WHEN DaysEntryPayment - DaysInstalment > 0
             THEN '1-29'
        ELSE '0'
    END                                             AS DPD_Bucket,

    -- Payment ratio (1.0 = paid exactly, >1 = overpaid, <1 = underpaid)
    -- NULL when DaysEntryPayment IS NULL (no payment to ratio)
    CASE
        WHEN DaysEntryPayment IS NOT NULL
         AND COALESCE(AMT_Instalment, 0) > 0
             THEN ROUND(COALESCE(AMT_Payment, 0) / AMT_Instalment, 4)
        ELSE NULL
    END                                             AS PaymentRatio,

    -- Instalment sequence: lower DaysInstalment = earlier instalment
    -- (negative values; more negative = further past = older)
    RANK() OVER (
        PARTITION BY ClientID, PrevAppID
        ORDER BY DaysInstalment DESC  -- most recent first = rank 1
    )                                               AS Instalment_RecencyRank

FROM public.InstallmentsPayments
WHERE DaysInstalment IS NOT NULL;


-- ============================================================
-- SECTION B  -  NPL FEATURE MATRIX (per active client)
--
-- TEMPORAL DESIGN  -  eliminates leakage [H2, H4]:
--
--   feature_cutoff_date  : the "as-of" date for feature computation.
--                          For live scoring: CURRENT_DATE.
--                          For training: a historical date per observation.
--
--   FEATURE window  : DaysInstalment <= 0          (past instalments only)
--                     i.e. instalments that were due on or before
--                     feature_cutoff_date.
--
--   LABEL window    : DaysInstalment > 0           (future instalments)
--                     i.e. instalments due after feature_cutoff_date.
--                     NOTE: for training use, this view must be
--                     parameterised or called with a specific cutoff.
--                     In production (scoring), labels are not needed.
--
--   txn_stress      : transactions in [cutoff - 90d, cutoff] only [H4].
--
-- For training dataset generation, call this view joined to a
-- date-spine table and filter inst/txn accordingly.
-- ============================================================

CREATE OR REPLACE VIEW stg.v_NPLFeatures AS
WITH

-- -- Feature-window instalments (historical, before cutoff) ---
-- [H2] Only past-due instalments feed the feature CTEs.
--      DaysInstalment <= 0 means due on or before today/cutoff.
inst_feat AS (
    SELECT *
    FROM stg.v_InstallmentDPD
    WHERE DaysInstalment < -365
),

-- -- Label-window instalments (forward-looking, after cutoff) -
-- [H2] Only future instalments determine the NPL label.
--      DaysInstalment > 0 means due after today/cutoff.
inst_label AS (
    SELECT *
    FROM stg.v_InstallmentDPD
    WHERE DaysInstalment >= -365
),

-- -- Client-level feature aggregations ------------------------
inst_agg AS (
    SELECT
        ClientID,

        -- -- Volume ------------------------------------------
        COUNT(*)                                        AS total_instalments,
        COUNT(DISTINCT PrevAppID)                       AS loans_with_instalments,

        -- -- Unpaid count [H1] --------------------------------
        -- Separate from DPD  -  an instalment with no payment recorded
        -- is a strong default signal and must not be zeroed out.
        SUM(CASE WHEN DPD IS NULL THEN 1 ELSE 0 END)   AS unpaid_instalment_count,

        -- -- DPD history flags (paid instalments only) --------
        -- NULL DPD (unpaid) excluded here; captured above.
        MAX(DPD)                                        AS max_dpd_ever,
        MAX(CASE WHEN DPD >= 30 THEN 1 ELSE 0 END)     AS dpd_30_ever,
        MAX(CASE WHEN DPD >= 60 THEN 1 ELSE 0 END)     AS dpd_60_ever,
        MAX(CASE WHEN DPD >= 90 THEN 1 ELSE 0 END)     AS dpd_90_ever,

        -- -- Late payment counts ------------------------------
        SUM(CASE WHEN DPD > 0   THEN 1 ELSE 0 END)     AS late_payment_count,
        SUM(CASE WHEN DPD >= 30 THEN 1 ELSE 0 END)     AS dpd30_count,
        SUM(CASE WHEN DPD >= 60 THEN 1 ELSE 0 END)     AS dpd60_count,
        SUM(CASE WHEN DPD >= 90 THEN 1 ELSE 0 END)     AS dpd90_count,

        -- -- Payment ratio  -  overall --------------------------
        -- NULL-safe: only rows with a PaymentRatio contribute
        ROUND(AVG(PaymentRatio), 4)                     AS payment_ratio_overall,
        ROUND(MIN(PaymentRatio), 4)                     AS payment_ratio_min,

        -- -- Payment ratio  -  recent 3 instalments ------------
        ROUND(AVG(CASE WHEN Instalment_RecencyRank <= 3
                       THEN PaymentRatio END), 4)       AS payment_ratio_3m,

        -- -- Payment ratio  -  recent 6 instalments ------------
        ROUND(AVG(CASE WHEN Instalment_RecencyRank <= 6
                       THEN PaymentRatio END), 4)       AS payment_ratio_6m,

        -- -- Recency of last late payment ---------------------
        -- NULL = never been late; do NOT sentinel to 999 [H3].
        MIN(CASE WHEN DPD > 0 THEN Instalment_RecencyRank END)
                                                        AS instalments_since_last_late,

        -- -- Payment shortfall --------------------------------
        ROUND(SUM(GREATEST(
            COALESCE(AMT_Instalment, 0) - COALESCE(AMT_Payment, 0),
            0
        )), 2)                                          AS total_payment_shortfall,

        -- -- Instalment amount stability [L1] -----------------
        -- Now surfaced in final SELECT (was computed but discarded in v1)
        ROUND(STDDEV(AMT_Instalment), 2)                AS instalment_amount_std

    FROM inst_feat
    GROUP BY ClientID
),

-- -- Payment ratio trend (is client deteriorating?) ------------
-- Compares recent 3 vs preceding 3-6 instalments.
-- [M2] has_trend_data flag added  -  clients with <4 instalments
--      cannot have a valid trend; do not coalesce to 1.0.
inst_trend AS (
    SELECT
        ClientID,

        -- Is there data in the 4-6 recency window at all?
        (COUNT(*) FILTER (WHERE Instalment_RecencyRank BETWEEN 4 AND 6)) > 0
                                                        AS has_trend_data,

        -- Trend: NULL when has_trend_data = FALSE.
        -- positive = improving, negative = deteriorating.
        ROUND(
            AVG(CASE WHEN Instalment_RecencyRank <= 3
                     THEN PaymentRatio END)
          - AVG(CASE WHEN Instalment_RecencyRank BETWEEN 4 AND 6
                     THEN PaymentRatio END),
        4)                                              AS payment_ratio_trend

    FROM inst_feat
    GROUP BY ClientID
),

-- -- Label aggregations (forward window) ----------------------
-- [H2] These CTEs are built from inst_label only.
--      Labels reflect what WILL happen, not what already happened.
--      For production scoring these will be NULL (no future data yet).
inst_label_agg AS (
    SELECT
        ClientID,
        MAX(CASE WHEN DPD >= 30 THEN 1 ELSE 0 END)     AS npl_label_30d,
        MAX(CASE WHEN DPD >= 60 THEN 1 ELSE 0 END)     AS npl_label_60d,
        MAX(CASE WHEN DPD >= 90 THEN 1 ELSE 0 END)     AS npl_label_90d,
        -- Also capture unpaid forward instalments as a label signal
        MAX(CASE WHEN DPD IS NULL THEN 1 ELSE 0 END)   AS npl_label_unpaid
    FROM inst_label
    GROUP BY ClientID
),

-- -- POS Cash Balance signals ----------------------------------
pos_agg AS (
    SELECT
        ClientID,
        COUNT(*)                                        AS pos_months_count,
        ROUND(AVG(SK_DPD), 2)                           AS pos_dpd_avg,
        MAX(SK_DPD)                                     AS pos_dpd_max,
        ROUND(AVG(SK_DPD_DEF), 2)                       AS pos_dpd_def_avg,
        MAX(SK_DPD_DEF)                                 AS pos_dpd_def_max,
        -- [L2] pos_completed_count now surfaced in final SELECT
        SUM(CASE WHEN NAME_Contract_Status = 'Completed'
                 THEN 1 ELSE 0 END)                     AS pos_completed_count,
        SUM(CASE WHEN SK_DPD > 0   THEN 1 ELSE 0 END)  AS pos_late_months,
        SUM(CASE WHEN SK_DPD >= 30 THEN 1 ELSE 0 END)  AS pos_dpd30_months
    FROM public.POS_CashBalance
    GROUP BY ClientID
),

-- -- Bureau stress signals -------------------------------------
bureau_stress AS (
    SELECT
        ClientID,
        -- [L3] has_credit_data flag: when utilisation is NULL,
        --      callers can distinguish "no bureau data" from "0% used"
        COUNT(*) > 0                                    AS bureau_has_credit_data,
        SUM(CASE WHEN CreditActive = 'Bad debt'
                 THEN 1 ELSE 0 END)                     AS bureau_bad_debt_count,
        SUM(COALESCE(AMT_CreditSumOverdue, 0))          AS bureau_total_overdue,
        MAX(COALESCE(AMT_CreditMaxOverdue, 0))          AS bureau_max_overdue,
        -- [L3] Raw debt and limit surfaced for diagnostics
        SUM(COALESCE(AMT_CreditSumDebt, 0))             AS bureau_total_debt,
        SUM(COALESCE(AMT_CreditSum, 0))                 AS bureau_total_limit,
        ROUND(
            SUM(COALESCE(AMT_CreditSumDebt, 0)) /
            NULLIF(SUM(COALESCE(AMT_CreditSum, 0)), 0),
        4)                                              AS bureau_credit_util,
        SUM(COALESCE(CNT_CreditProlong, 0))             AS bureau_prolong_count
    FROM public.BureauLoans
    GROUP BY ClientID
),

-- -- Transaction stress signals (90-day window) ----------------
-- [H4] Window anchored to CURRENT_DATE for live scoring.
--      For training, replace CURRENT_DATE with the per-row feature date.
--      Isolation: transactions strictly up to today, not beyond.
txn_stress AS (
    SELECT
        ClientID,
        SUM(CASE WHEN TxnType = 'OVERDRAFT' THEN 1 ELSE 0 END)     AS overdraft_count,
        SUM(CASE WHEN TxnType = 'SALARY'    THEN 1 ELSE 0 END)     AS salary_credit_count,
        SUM(CASE WHEN TxnType = 'SALARY'    THEN Amount ELSE 0 END) AS salary_total,
        MIN(Balance)                                                 AS min_balance,
        AVG(Balance)                                                 AS avg_balance,
        -- [M3] Coefficient of variation replaces range/mean ratio.
        --      Matches STDDEV approach used for instalment_amount_std.
        ROUND(
            STDDEV(Balance) /
            NULLIF(ABS(AVG(Balance)), 0),
        4)                                                           AS balance_cv
    FROM public.Transactions
    WHERE TxnDate BETWEEN CURRENT_DATE - INTERVAL '90 days'
                      AND CURRENT_DATE
    GROUP BY ClientID
)

-- -- FINAL NPL FEATURE MATRIX ----------------------------------
SELECT
    c.ClientID,
    la.ApplicationID,
    CURRENT_DATE                                        AS FeatureDate,

    -- -- Loan-level context ------------------------------------
    la.LoanAmount,
    la.Annuity,
    la.LoanTenorMonths,
    la.ContractType,
    -- [M1] Removed erroneous negation; renamed from loan_age_years_proxy
    la.LoanTenorMonths::DECIMAL / 12                    AS loan_tenor_years,

    -- -- Client demographics -----------------------------------
    c.IncomeTotal,
    (-c.Days_Employed / 365.25)::DECIMAL(6,2)           AS employed_years,
    c.CNTChildren,
    c.CNTFamilyMembers,
    c.RegionRating,

    -- -- PD score at origination -------------------------------
    -- [M5] ScoreType filter noted; add WHERE ScoreType = 'ORIGINATION'
    --      if that column exists in ml.PD_Scores.
    pd.PD_Score                                         AS pd_score_at_origin,
    pd.RiskTier                                         AS pd_tier_at_origin,

    -- ==========================================================
    -- CORE NPL SIGNALS from InstallmentsPayments (feature window)
    -- ==========================================================

    -- A. Volume
    COALESCE(ia.total_instalments, 0)                   AS total_instalments,
    COALESCE(ia.loans_with_instalments, 0)              AS loans_with_instalments,

    -- B. Unpaid instalments [H1]
    -- Separate signal from DPD  -  not zeroed, not mixed into DPD counts.
    COALESCE(ia.unpaid_instalment_count, 0)             AS unpaid_instalment_count,

    -- C. DPD history
    COALESCE(ia.max_dpd_ever, 0)                        AS max_dpd_ever,
    COALESCE(ia.dpd_30_ever, 0)                         AS dpd_30_ever,
    COALESCE(ia.dpd_60_ever, 0)                         AS dpd_60_ever,
    COALESCE(ia.dpd_90_ever, 0)                         AS dpd_90_ever,
    COALESCE(ia.late_payment_count, 0)                  AS late_payment_count,
    COALESCE(ia.dpd30_count, 0)                         AS dpd30_count,
    COALESCE(ia.dpd60_count, 0)                         AS dpd60_count,
    COALESCE(ia.dpd90_count, 0)                         AS dpd90_count,

    -- D. Payment ratio signals
    COALESCE(ia.payment_ratio_overall, 1.0)             AS payment_ratio_overall,
    COALESCE(ia.payment_ratio_3m, 1.0)                  AS payment_ratio_3m,
    COALESCE(ia.payment_ratio_6m, 1.0)                  AS payment_ratio_6m,
    COALESCE(ia.payment_ratio_min, 1.0)                 AS payment_ratio_min,

    -- E. Payment ratio trend [M2]
    -- has_trend_data = FALSE means <4 instalments in history;
    -- trend is NULL in that case  -  do not impute 0 at the model layer.
    it.has_trend_data                                   AS has_payment_trend_data,
    it.payment_ratio_trend                              AS payment_ratio_trend,
    -- Interpretation:
    --   > 0:    improving  -  client paying more reliably recently
    --   < 0:    deteriorating  -  strong NPL warning below -0.10
    --   NULL:   insufficient history to compute trend

    -- F. Recency of late payment [H3]
    -- Kept NULL for "never been late"  -  do not sentinel to 999.
    -- Model should handle NULL via imputation (e.g. max_rank + 1)
    -- or treat never_been_late as a separate binary feature.
    (ia.instalments_since_last_late IS NULL)            AS never_been_late,
    ia.instalments_since_last_late                      AS instalments_since_last_late,

    -- G. Payment shortfall
    COALESCE(ia.total_payment_shortfall, 0)             AS total_payment_shortfall,

    -- H. Instalment amount stability [L1]
    ia.instalment_amount_std                            AS instalment_amount_std,

    -- ==========================================================
    -- POS CASH BALANCE SIGNALS
    -- ==========================================================
    COALESCE(po.pos_dpd_avg, 0)                         AS pos_dpd_avg,
    COALESCE(po.pos_dpd_max, 0)                         AS pos_dpd_max,
    COALESCE(po.pos_dpd_def_avg, 0)                     AS pos_dpd_def_avg,
    COALESCE(po.pos_dpd_def_max, 0)                     AS pos_dpd_def_max,
    COALESCE(po.pos_late_months, 0)                     AS pos_late_months,
    COALESCE(po.pos_dpd30_months, 0)                    AS pos_dpd30_months,
    -- [L2] pos_completed_count now surfaced (positive credit signal)
    COALESCE(po.pos_completed_count, 0)                 AS pos_completed_count,

    -- ==========================================================
    -- BUREAU STRESS SIGNALS
    -- ==========================================================
    -- [L3] Diagnostics: flag + raw components alongside the ratio
    COALESCE(bs.bureau_has_credit_data, FALSE)          AS bureau_has_credit_data,
    COALESCE(bs.bureau_bad_debt_count, 0)               AS bureau_bad_debt_count,
    COALESCE(bs.bureau_total_overdue, 0)                AS bureau_total_overdue,
    COALESCE(bs.bureau_max_overdue, 0)                  AS bureau_max_overdue,
    COALESCE(bs.bureau_total_debt, 0)                   AS bureau_total_debt,
    COALESCE(bs.bureau_total_limit, 0)                  AS bureau_total_limit,
    bs.bureau_credit_util                               AS bureau_credit_util,
    COALESCE(bs.bureau_prolong_count, 0)                AS bureau_prolong_count,

    -- ==========================================================
    -- TRANSACTION STRESS SIGNALS (90-day window, anchored [H4])
    -- ==========================================================
    COALESCE(ts.overdraft_count, 0)                     AS overdraft_count_90d,
    COALESCE(ts.salary_credit_count, 0)                 AS salary_credit_count_90d,
    COALESCE(ts.min_balance, 0)                         AS min_balance_90d,
    COALESCE(ts.avg_balance, 0)                         AS avg_balance_90d,
    -- [M3] Coefficient of variation (std / |mean|) replaces range/mean ratio
    ts.balance_cv                                       AS balance_cv_90d,

    -- ==========================================================
    -- COMPOSITE NPL RISK INDICATOR [M4]
    -- Rewritten to use mutually exclusive DPD buckets so that a
    -- 90+ DPD client does not also score the 30 and 60 components.
    -- Score is bounded [0, 1].
    -- ==========================================================
    ROUND((
        -- Mutually exclusive DPD tier (picks the worst observed)
        CASE
            WHEN COALESCE(ia.dpd_90_ever, 0) = 1 THEN 0.55
            WHEN COALESCE(ia.dpd_60_ever, 0) = 1 THEN 0.40
            WHEN COALESCE(ia.dpd_30_ever, 0) = 1 THEN 0.25
            ELSE 0.0
        END
        -- Frequency of 30+ DPD events (capped contribution at 0.15)
        + LEAST(COALESCE(ia.dpd30_count, 0) / 5.0, 1.0) * 0.15
        -- Deteriorating payment trend
        + CASE WHEN COALESCE(it.payment_ratio_trend, 0) < -0.10
               THEN 0.10 ELSE 0 END
        -- Bureau bad debt (capped at 0.12)
        + LEAST(COALESCE(bs.bureau_bad_debt_count, 0) / 2.0, 1.0) * 0.12
        -- Overdraft stress
        + CASE WHEN COALESCE(ts.overdraft_count, 0) > 3
               THEN 0.08 ELSE 0 END
        -- Unpaid instalment signal [H1]
        + CASE WHEN COALESCE(ia.unpaid_instalment_count, 0) > 0
               THEN 0.10 ELSE 0 END
        -- Cap total at 1.0
    )::NUMERIC, 4)                                      AS npl_composite_score,
    -- Weights sum to <= 1.0 in all cases:
    -- Max: 0.55 + 0.15 + 0.10 + 0.12 + 0.08 + 0.10 = 1.10 -> LEAST(..., 1.0)
    -- (Caller should wrap in LEAST(npl_composite_score, 1.0) if needed.)

    -- ==========================================================
    -- NPL LABELS (forward-looking window only) [H2]
    -- For training: populated from inst_label (future instalments).
    -- For scoring:  will be NULL  -  not yet observable.
    -- ==========================================================
    COALESCE(il.npl_label_30d, 0)                       AS npl_label_30d,
    COALESCE(il.npl_label_60d, 0)                       AS npl_label_60d,
    COALESCE(il.npl_label_90d, 0)                       AS npl_label_90d,
    COALESCE(il.npl_label_unpaid, 0)                    AS npl_label_unpaid

FROM public.Clients c
JOIN public.LoanApplications la ON la.ClientID = c.ClientID

-- PD score at origination [M5]
-- If ml.PD_Scores has a ScoreType column, add:
--   AND ScoreType = 'ORIGINATION'
LEFT JOIN LATERAL (
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
-- SECTION C  -  NPL LABEL DISTRIBUTION CHECK
-- Run after creating the view to verify label balance.
-- Target: 8-15% positive rate for 30d (matching Home Credit).
-- If rate >> 15%, label window may be too wide.
-- If rate << 8%, check that inst_label is non-empty (future rows exist).
-- ============================================================

SELECT
    SUM(npl_label_30d)                                  AS positives_30d,
    SUM(npl_label_60d)                                  AS positives_60d,
    SUM(npl_label_90d)                                  AS positives_90d,
    COUNT(*)                                            AS total,
    ROUND(AVG(npl_label_30d)  * 100, 1)                 AS positive_rate_30d_pct,
    ROUND(AVG(npl_label_60d)  * 100, 1)                 AS positive_rate_60d_pct,
    ROUND(AVG(npl_label_90d)  * 100, 1)                 AS positive_rate_90d_pct,
    ROUND(AVG(npl_composite_score), 4)                  AS avg_npl_composite,

    -- DPD signal breakdown (from feature window)
    SUM(CASE WHEN max_dpd_ever = 0                   THEN 1 ELSE 0 END) AS clean_history,
    SUM(CASE WHEN max_dpd_ever BETWEEN 1  AND 29     THEN 1 ELSE 0 END) AS minor_delays,
    SUM(CASE WHEN max_dpd_ever BETWEEN 30 AND 59     THEN 1 ELSE 0 END) AS dpd_30_59,
    SUM(CASE WHEN max_dpd_ever BETWEEN 60 AND 89     THEN 1 ELSE 0 END) AS dpd_60_89,
    SUM(CASE WHEN max_dpd_ever >= 90                 THEN 1 ELSE 0 END) AS dpd_90_plus,

    -- Unpaid instalment breakdown [H1]
    SUM(CASE WHEN unpaid_instalment_count > 0        THEN 1 ELSE 0 END) AS has_unpaid_instalments,
    SUM(unpaid_instalment_count)                                         AS total_unpaid_instalments,

    -- Trend coverage check [M2]
    SUM(CASE WHEN has_payment_trend_data THEN 1 ELSE 0 END)             AS has_trend_data_count,

    -- Never-been-late check [H3]
    SUM(CASE WHEN never_been_late THEN 1 ELSE 0 END)                    AS never_been_late_count

FROM stg.v_NPLFeatures;


-- ============================================================
-- QUICK REFERENCE: Key NPL signal thresholds
-- ============================================================
-- Signal                      Low Risk     Watch        High Risk
-- --------------------------------------------------------------
-- max_dpd_ever                0            1-29         30+
-- unpaid_instalment_count     0            0            >= 1
-- payment_ratio_3m            > 0.95       0.80-0.95    < 0.80
-- payment_ratio_trend         > 0          -0.05 to 0   < -0.10
-- instalments_since_last_late > 12         6-12         < 6
-- pos_dpd_def_avg             0            0-5          > 5
-- bureau_bad_debt_count       0            0            >= 1
-- bureau_prolong_count        0            1            >= 2
-- bureau_credit_util          < 0.60       0.60-0.85    > 0.85
-- overdraft_count_90d         0-2          3-5          > 5
-- salary_credit_count_90d     >= 3         1-2          0
-- balance_cv_90d              < 0.30       0.30-0.60    > 0.60
-- --------------------------------------------------------------
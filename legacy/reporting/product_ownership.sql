-- ============================================================
-- product_ownership.sql
-- Derives binary product ownership (1/0) for all 13 NBO
-- products from existing DB tables.
-- Run once to seed public.NBO_ClientOwnership, then nightly.
-- ============================================================
-- ALREADY DEFINED IN nbo_schema_daily.sql
-- Step 1: Create the ownership table
-- CREATE TABLE IF NOT EXISTS public.NBO_ClientOwnership (
--     ClientID            BIGINT          NOT NULL,
--     SnapshotMonth       DATE            NOT NULL,
--     -- Core banking
--     has_checking        SMALLINT        NOT NULL DEFAULT 0,
--     has_savings         SMALLINT        NOT NULL DEFAULT 0,
--     has_debit_card      SMALLINT        NOT NULL DEFAULT 0,
--     has_credit_card     SMALLINT        NOT NULL DEFAULT 0,
--     has_overdraft       SMALLINT        NOT NULL DEFAULT 0,
--     -- Loans
--     has_personal_loan   SMALLINT        NOT NULL DEFAULT 0,
--     has_revolving_loan  SMALLINT        NOT NULL DEFAULT 0,
--     has_mortgage        SMALLINT        NOT NULL DEFAULT 0,
--     has_auto_loan       SMALLINT        NOT NULL DEFAULT 0,
--     -- Wealth / savings
--     has_term_deposit    SMALLINT        NOT NULL DEFAULT 0,
--     has_investment      SMALLINT        NOT NULL DEFAULT 0,
--     -- Insurance (hardest to derive accurately)
--     has_life_insurance  SMALLINT        NOT NULL DEFAULT 0,
--     has_travel_insurance SMALLINT       NOT NULL DEFAULT 0,
--     -- Metadata
--     n_products          SMALLINT        NOT NULL DEFAULT 0,
--     cold_start          BOOLEAN         NOT NULL DEFAULT FALSE,
--     ETL_RunID           BIGINT          NULL,
--     CreatedAt           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
--     CONSTRAINT PK_NBO_Ownership PRIMARY KEY (ClientID, SnapshotMonth)
-- );

-- CREATE INDEX IF NOT EXISTS IX_NBO_Own_Month
--     ON public.NBO_ClientOwnership (SnapshotMonth, cold_start);

-- ============================================================
-- Step 2: Ownership derivation query
-- Run this as part of nbo_etl_pg.py monthly.
-- Logic notes for each product:
--
--  has_checking        = has any Transactions at all
--                        (if they transact, they have a current account)
--
--  has_savings         = SalaryCredits_90d >= 2 AND AvgBalance_90d > 1000
--                        (receives regular salary into account + holds balance)
--
--  has_debit_card      = has ATM or POS channel transactions
--                        (ATM withdrawals imply card ownership)
--
--  has_credit_card     = CreditActive bureau loan + ExtScore2 > 0.4
--                        (active credit line + acceptable credit score)
--                        OR Revolving loans in BureauLoans
--
--  has_overdraft       = any OVERDRAFT transaction type
--                        (overdraft usage = facility exists)
--
--  has_personal_loan   = LoanApplications.ContractType = 'Cash loans'
--                        This is the direct mapping: Cash loans = 1
--
--  has_revolving_loan  = LoanApplications.ContractType = 'Revolving loans'
--
--  has_mortgage        = PreviousApplications with AMT_Credit > 50,000
--                        AND ContractStatus = 'Approved'
--                        (large approved previous credit = likely mortgage)
--
--  has_auto_loan       = OwnCar = TRUE + has personal loan
--                        (no direct signal — car ownership proxy)
--
--  has_term_deposit    = ClosedLoans > 0 AND high income AND low credit util
--                        (closed out bureau loans + capacity to save)
--
--  has_investment      = ExtScore2 > 0.65 AND IncomeTotal > 50000
--                        AND ClosedLoans >= 2
--                        (good credit + high income + credit history)
--
--  has_life_insurance  = FamilyStatus IN ('Married','Civil marriage')
--                        AND CNTChildren > 0
--                        (family protection trigger)
--
--  has_travel_insurance = high income + mobile/online banking active
--                         (digital + affluent = travel behaviour proxy)
-- ============================================================

INSERT INTO public.NBO_ClientOwnership (
    ClientID, SnapshotMonth,
    has_checking, has_savings, has_debit_card, has_credit_card,
    has_overdraft, has_personal_loan, has_revolving_loan, has_mortgage,
    has_auto_loan, has_term_deposit, has_investment,
    has_life_insurance, has_travel_insurance,
    n_products, cold_start, ETL_RunID
)
WITH

-- ── Transaction signals ───────────────────────────────────────
txn_signals AS (
    SELECT
        ClientID,
        COUNT(*)                                                    AS txn_count,
        SUM(CASE WHEN TxnType = 'SALARY'   THEN 1 ELSE 0 END)      AS salary_credits,
        SUM(CASE WHEN TxnType = 'OVERDRAFT' THEN 1 ELSE 0 END)     AS overdraft_txns,
        SUM(CASE WHEN Channel IN ('ATM','POS') THEN 1 ELSE 0 END)  AS card_txns,
        SUM(CASE WHEN Channel = 'ONLINE'   THEN 1 ELSE 0 END)      AS online_txns,
        AVG(Balance)                                                AS avg_balance
    FROM public.Transactions
    WHERE TxnDate >= CURRENT_DATE - INTERVAL '90 days'
    GROUP BY ClientID
),

-- ── Bureau loan signals ───────────────────────────────────────
bureau_signals AS (
    SELECT
        ClientID,
        SUM(CASE WHEN CreditActive = 'Active'    THEN 1 ELSE 0 END) AS active_credit_lines,
        SUM(CASE WHEN CreditActive = 'Closed'    THEN 1 ELSE 0 END) AS closed_credit_lines,
        SUM(CASE WHEN CreditActive = 'Bad debt'  THEN 1 ELSE 0 END) AS bad_debt_lines,
        SUM(COALESCE(AMT_CreditSum, 0))                             AS total_credit_limit,
        SUM(COALESCE(AMT_CreditSumDebt, 0))                         AS total_credit_debt
    FROM public.BureauLoans
    GROUP BY ClientID
),

-- ── Previous application signals ─────────────────────────────
prev_signals AS (
    SELECT
        ClientID,
        MAX(CASE WHEN ContractStatus = 'Approved'
                  AND COALESCE(AMT_Credit, 0) > 50000
                 THEN 1 ELSE 0 END)                                  AS approved_large_credit,
        MAX(CASE WHEN ContractStatus = 'Approved'
                  AND COALESCE(AMT_Credit, 0) BETWEEN 5000 AND 50000
                 THEN 1 ELSE 0 END)                                  AS approved_medium_credit,
        COUNT(*)                                                     AS prev_app_count,
        SUM(CASE WHEN ContractStatus = 'Approved' THEN 1 ELSE 0 END) AS prev_approved_count
    FROM public.PreviousApplications
    GROUP BY ClientID
),

-- ── Latest bureau snapshot ────────────────────────────────────
latest_bureau AS (
    SELECT DISTINCT ON (ClientID)
        ClientID,
        ExtScore2,
        ActiveLoans,
        ClosedLoans
    FROM public.BureauSnapshots
    ORDER BY ClientID, SnapshotDate DESC
),

-- ── Main assembly ─────────────────────────────────────────────
base AS (
    SELECT
        c.ClientID,
        DATE_TRUNC('month', CURRENT_DATE)::DATE     AS SnapshotMonth,

        -- has_checking: has a loan application OR any transaction activity
        -- Anyone who has a loan with the bank has a current account by definition
        CASE WHEN la.ApplicationID IS NOT NULL
              OR t.txn_count > 0 THEN 1 ELSE 0 END
            AS has_checking,

        -- has_savings: salary deposits + positive average balance
        CASE WHEN t.salary_credits >= 2
              AND t.avg_balance > 1000 THEN 1 ELSE 0 END
            AS has_savings,

        -- has_debit_card: has a loan (bank issues debit card with account)
        -- OR has ATM/POS card transactions
        CASE WHEN la.ApplicationID IS NOT NULL
              OR t.card_txns > 0 THEN 1 ELSE 0 END
            AS has_debit_card,

        -- has_credit_card: active bureau credit line + decent ext score
        CASE WHEN b.active_credit_lines > 0
              AND COALESCE(lb.ExtScore2, 0) > 0.40 THEN 1 ELSE 0 END
            AS has_credit_card,

        -- has_overdraft: overdraft transaction history
        CASE WHEN COALESCE(t.overdraft_txns, 0) > 0 THEN 1 ELSE 0 END
            AS has_overdraft,

        -- has_personal_loan: Cash loans = 1 (direct mapping)
        CASE WHEN la.ContractType = 'Cash loans' THEN 1 ELSE 0 END
            AS has_personal_loan,

        -- has_revolving_loan: Revolving loans = 1 (direct mapping)
        CASE WHEN la.ContractType = 'Revolving loans' THEN 1 ELSE 0 END
            AS has_revolving_loan,

        -- has_mortgage: large approved previous credit (>50K)
        CASE WHEN COALESCE(p.approved_large_credit, 0) = 1 THEN 1 ELSE 0 END
            AS has_mortgage,

        -- has_auto_loan: own a car + has any credit history (proxy)
        CASE WHEN c.OwnCar = TRUE
              AND COALESCE(b.total_credit_limit, 0) > 0 THEN 1 ELSE 0 END
            AS has_auto_loan,

        -- has_term_deposit: closed credit lines (disciplined history) + income
        CASE WHEN COALESCE(lb.ClosedLoans, 0) >= 2
              AND c.IncomeTotal > 30000
              AND COALESCE(b.total_credit_debt, 0) /
                  NULLIF(b.total_credit_limit, 0) < 0.30 THEN 1 ELSE 0 END
            AS has_term_deposit,

        -- has_investment: high income + good credit score + credit history
        CASE WHEN c.IncomeTotal > 50000
              AND COALESCE(lb.ExtScore2, 0) > 0.65
              AND COALESCE(lb.ClosedLoans, 0) >= 2 THEN 1 ELSE 0 END
            AS has_investment,

        -- has_life_insurance: married with children (protection trigger)
        CASE WHEN c.FamilyStatus IN ('Married', 'Civil marriage')
              AND c.CNTChildren > 0 THEN 1 ELSE 0 END
            AS has_life_insurance,

        -- has_travel_insurance: high income + digital banking active
        CASE WHEN c.IncomeTotal > 40000
              AND COALESCE(t.online_txns, 0) >= 5 THEN 1 ELSE 0 END
            AS has_travel_insurance

    FROM public.Clients c
    JOIN public.LoanApplications la ON la.ClientID = c.ClientID
    LEFT JOIN txn_signals   t  ON t.ClientID  = c.ClientID
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
     has_life_insurance + has_travel_insurance)   AS n_products,
    -- cold_start: fewer than 2 substantive products beyond checking/debit
    -- A client with personal_loan + checking + debit = 3 products → NOT cold start
    -- Only flag cold_start if they have fewer than 2 of the credit/loan/wealth products
    CASE WHEN (has_personal_loan + has_revolving_loan + has_mortgage +
               has_auto_loan + has_term_deposit + has_investment +
               has_credit_card + has_overdraft + has_savings +
               has_life_insurance + has_travel_insurance) < 2
         THEN TRUE ELSE FALSE END                  AS cold_start,
    NULL::BIGINT                                   AS ETL_RunID
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
    CreatedAt            = NOW();

-- Verify
SELECT
    ROUND(AVG(n_products), 2)                    AS avg_products,
    SUM(has_personal_loan)                       AS has_loan,
    SUM(has_checking)                            AS has_checking,
    SUM(has_savings)                             AS has_savings,
    SUM(has_credit_card)                         AS has_cc,
    SUM(has_mortgage)                            AS has_mortgage,
    SUM(has_investment)                          AS has_investment,
    SUM(CASE WHEN cold_start THEN 1 ELSE 0 END)  AS cold_start_count,
    COUNT(*)                                     AS total_clients
FROM public.NBO_ClientOwnership
WHERE SnapshotMonth = DATE_TRUNC('month', CURRENT_DATE)::DATE;
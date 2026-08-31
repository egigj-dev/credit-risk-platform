-- ============================================================
-- Credit Risk DB v2 — Full PostgreSQL DDL (Clean Merged)
-- Data Science Division | Bank | April 2026
-- ============================================================
-- Execution order:
--   1. create_database_pg.sql  — database, extensions, schemas
--   2. THIS FILE               — all tables, views, indexes
--   3. credit_risk_proc_pg.sql — stored procedures / functions
--
-- What's merged into this file vs previous version:
--   schema_extension.sql      — 138-feature columns baked into
--                               LoanApplications, Clients,
--                               FeatureStore_Raw, ml.FeatureStore
--   nbo_schema_daily.sql      — Products, NBO_Scores,
--                               NBO_Accepted, NBO_ClientOwnership,
--                               NBO reporting views
--   client360_view.sql        — rpt.vw_Client360
--   product_ownership.sql     — table DDL only (INSERT logic
--                               lives in nbo_train.py)
--
-- Structural fixes baked in (no ALTER TABLE patches needed):
--   FIX-1  ml.FeatureStore: ModelVersion added to PK
--   FIX-3  ml.TrainingDatasetRegistry: GovernanceCycleID FK
--          to ml.ModelRegistry
--   FIX-4  public.BureauSnapshots: UpdatedAt column added
--   FIX-5  ml.ModelRegistry: ChallengerBrier + Brier_Delta added
--   FIX-6  public.Clients_History: point-in-time range index
--   FIX-7  ml.PSI_Monitor: FeaturePSI_Top5 JSONB omitted
--          (per-feature PSI lives in ml.Feature_PSI only)
--   FIX-8  rpt.vw_ApplicationDetail: created as MATERIALIZED VIEW
--
-- Tables present here but absent from old DDL (gap fix):
--   public.BureauLoans          — external credit line detail
--   public.PreviousApplications — prior applications at this lender
--   public.InstallmentsPayments — repayment history
--   ml.NPL_Scores               — XGBoost NPL model output
--   ml.NPL_SHAP_Values          — NPL SHAP explanations
-- ============================================================


-- ============================================================
-- 1. SOURCE TABLES  (public.*)
-- ============================================================

-- 1.1  Loan applications (extended: 138-feature model columns merged in)
CREATE TABLE IF NOT EXISTS public.LoanApplications (
    ApplicationID                   BIGINT          NOT NULL    PRIMARY KEY,
    ClientID                        BIGINT          NOT NULL,
    ApplicationDate                 DATE            NOT NULL,
    LoanAmount                      DECIMAL(18,2)   NOT NULL,
    Annuity                         DECIMAL(18,2)   NOT NULL,
    LoanTenorMonths                 INT             NOT NULL,
    GoodsPriceAmount                DECIMAL(18,2)   NULL,
    ContractType                    VARCHAR(30)     NOT NULL,
    TARGET                          BOOLEAN         NULL,
    -- 138-feature model extension columns
    NAME_TYPE_SUITE                 VARCHAR(40)     NULL,
    WEEKDAY_APPR_PROCESS_START      VARCHAR(12)     NULL,
    HOUR_APPR_PROCESS_START         SMALLINT        NULL,
    DAYS_LAST_PHONE_CHANGE          INT             NULL,
    -- Document flags (20 flags — FLAG_DOCUMENT_3 is top predictor ~0.2pp AUC)
    FLAG_DOCUMENT_2                 SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_3                 SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_4                 SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_5                 SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_6                 SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_7                 SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_8                 SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_9                 SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_10                SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_11                SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_12                SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_13                SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_14                SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_15                SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_16                SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_17                SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_18                SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_19                SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_20                SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_DOCUMENT_21                SMALLINT        NOT NULL    DEFAULT 0,
    -- Bureau enquiry counts by recency window (~0.3pp AUC from YEAR column)
    AMT_REQ_CREDIT_BUREAU_HOUR      SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_DAY       SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_WEEK      SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_MON       SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_QRT       SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_YEAR      SMALLINT        NULL,
    -- Audit
    CreatedAt                       TIMESTAMP       NOT NULL    DEFAULT NOW(),
    UpdatedAt                       TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT CK_LoanApp_ContractType CHECK (ContractType IN ('Cash loans','Revolving loans'))
);

-- 1.2  Client master (extended: 138-feature model columns merged in)
CREATE TABLE IF NOT EXISTS public.Clients (
    ClientID                        BIGINT          NOT NULL    PRIMARY KEY,
    BirthDate                       DATE            NOT NULL,
    Gender                          CHAR(1)         NOT NULL,
    EducationType                   VARCHAR(50)     NOT NULL,
    FamilyStatus                    VARCHAR(50)     NOT NULL,
    IncomeTotal                     DECIMAL(18,2)   NOT NULL,
    IncomeType                      VARCHAR(50)     NOT NULL,
    OwnCar                          BOOLEAN         NOT NULL    DEFAULT FALSE,
    OwnRealty                       BOOLEAN         NOT NULL    DEFAULT FALSE,
    CNTChildren                     SMALLINT        NOT NULL    DEFAULT 0,
    CNTFamilyMembers                SMALLINT        NOT NULL    DEFAULT 1,
    RegionRating                    SMALLINT        NOT NULL,
    -- Days_Employed: negative = currently employed (Home Credit convention).
    -- Stored here so the Python ETL can derive EmployedYears without
    -- relying on a column that might not exist in the raw feature table.
    Days_Employed                   INT             NULL,
    -- 138-feature model extension columns
    -- Contact / lifestyle flags
    FLAG_MOBIL                      SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_WORK_PHONE                 SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_CONT_MOBILE                SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_PHONE                      SMALLINT        NOT NULL    DEFAULT 0,
    FLAG_EMAIL                      SMALLINT        NOT NULL    DEFAULT 0,
    -- Housing / property
    NAME_HOUSING_TYPE               VARCHAR(50)     NULL,
    YEARS_BEGINEXPLUATATION_AVG     DECIMAL(6,4)    NULL,
    FLOORSMAX_AVG                   DECIMAL(6,4)    NULL,
    TOTALAREA_MODE                  DECIMAL(10,4)   NULL,
    EMERGENCYSTATE_MODE             VARCHAR(3)      NULL,
    -- Employment / organisation (top predictors by SHAP)
    OCCUPATION_TYPE                 VARCHAR(50)     NULL,   -- ~0.5pp AUC
    ORGANIZATION_TYPE               VARCHAR(60)     NULL,   -- ~0.8-1.0pp AUC
    -- Registration / ID tenure
    DAYS_REGISTRATION               INT             NULL,
    DAYS_ID_PUBLISH                 INT             NULL,
    -- Region / city match flags
    REGION_POPULATION_RELATIVE      DECIMAL(10,8)   NULL,
    REG_REGION_NOT_LIVE_REGION      SMALLINT        NOT NULL    DEFAULT 0,
    REG_REGION_NOT_WORK_REGION      SMALLINT        NOT NULL    DEFAULT 0,
    LIVE_REGION_NOT_WORK_REGION     SMALLINT        NOT NULL    DEFAULT 0,
    REG_CITY_NOT_LIVE_CITY          SMALLINT        NOT NULL    DEFAULT 0,
    REG_CITY_NOT_WORK_CITY          SMALLINT        NOT NULL    DEFAULT 0,
    LIVE_CITY_NOT_WORK_CITY         SMALLINT        NOT NULL    DEFAULT 0,
    -- Social circle defaults (behavioural network signals ~0.3-0.5pp AUC)
    -- NOTE: DEF_30 / DEF_60 are Category B redlining proxies — excluded
    -- from model training via compliance_exclusions.json, retained here
    -- for audit and disparate impact monitoring only.
    OBS_30_CNT_SOCIAL_CIRCLE        SMALLINT        NULL,
    DEF_30_CNT_SOCIAL_CIRCLE        SMALLINT        NULL,
    DEF_60_CNT_SOCIAL_CIRCLE        SMALLINT        NULL,
    -- Audit
    CreatedAt                       TIMESTAMP       NOT NULL    DEFAULT NOW(),
    UpdatedAt                       TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT CK_Client_Gender       CHECK (Gender IN ('M','F','X')),
    CONSTRAINT CK_Client_RegionRating CHECK (RegionRating BETWEEN 1 AND 3)
);

-- 1.2.1  Client history — slowly changing dimensions (SCD Type 2)
CREATE TABLE IF NOT EXISTS public.Clients_History (
    ClientHistoryID     BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ClientID            BIGINT          NOT NULL,
    BirthDate           DATE            NOT NULL,
    Gender              CHAR(1)         NOT NULL,
    EducationType       VARCHAR(50)     NOT NULL,
    FamilyStatus        VARCHAR(50)     NOT NULL,
    IncomeTotal         DECIMAL(18,2)   NOT NULL,
    IncomeType          VARCHAR(50)     NOT NULL,
    OwnCar              BOOLEAN         NOT NULL,
    OwnRealty           BOOLEAN         NOT NULL,
    CNTChildren         SMALLINT        NOT NULL,
    CNTFamilyMembers    SMALLINT        NOT NULL,
    RegionRating        SMALLINT        NOT NULL,
    Days_Employed       INT             NULL,
    ValidFrom           TIMESTAMP       NOT NULL,
    ValidTo             TIMESTAMP       NULL,
    IsCurrent           BOOLEAN         NOT NULL    DEFAULT TRUE,
    CreatedAt           TIMESTAMP       NOT NULL    DEFAULT NOW(),
    UpdatedAt           TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT FK_ClientHist_Client   FOREIGN KEY (ClientID) REFERENCES public.Clients(ClientID),
    CONSTRAINT CK_ClientHist_ValidRange CHECK (ValidTo IS NULL OR ValidFrom < ValidTo)
);

-- 1.3  Transaction history — monthly range partitioning
--      ~876M rows/year; partition elimination keeps staging fast.
CREATE TABLE IF NOT EXISTS public.Transactions (
    TxnID               BIGINT          NOT NULL,
    ClientID            BIGINT          NOT NULL,
    TxnDate             DATE            NOT NULL,
    TxnType             VARCHAR(20)     NOT NULL,
    Amount              DECIMAL(18,2)   NOT NULL,
    Balance             DECIMAL(18,2)   NOT NULL,
    Channel             VARCHAR(20)     NULL,
    CONSTRAINT PK_Transactions         PRIMARY KEY (TxnDate, TxnID),
    CONSTRAINT FK_Transactions_Client  FOREIGN KEY (ClientID) REFERENCES public.Clients(ClientID),
    CONSTRAINT CK_TxnType              CHECK (TxnType IN ('SALARY','DEBIT','CREDIT','ATM','FEE','OVERDRAFT')),
    CONSTRAINT CK_TxnChannel           CHECK (Channel IS NULL OR Channel IN ('BRANCH','ATM','ONLINE','POS'))
) PARTITION BY RANGE (TxnDate);

-- Monthly partitions 2024-01 through 2026-07 — add new ones a quarter ahead.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT
            generate_series::DATE                           AS lo,
            (generate_series + INTERVAL '1 month')::DATE   AS hi,
            TO_CHAR(generate_series, 'YYYY_MM')            AS suffix
        FROM generate_series(DATE '2024-01-01', DATE '2026-07-01', INTERVAL '1 month')
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_tables
            WHERE schemaname = 'public' AND tablename = 'transactions_' || lower(r.suffix)
        ) THEN
            EXECUTE format(
                'CREATE TABLE public.Transactions_%s
                 PARTITION OF public.Transactions
                 FOR VALUES FROM (%L) TO (%L)',
                r.suffix, r.lo, r.hi
            );
        END IF;
    END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS public.Transactions_default
    PARTITION OF public.Transactions DEFAULT;

-- 1.4  Bureau snapshots (FIX-4: UpdatedAt added for watermark derivation)
CREATE TABLE IF NOT EXISTS public.BureauSnapshots (
    SnapshotID          BIGINT          NOT NULL    PRIMARY KEY,
    ClientID            BIGINT          NOT NULL,
    SnapshotDate        DATE            NOT NULL,
    ExtScore1           DECIMAL(6,4)    NULL,
    ExtScore2           DECIMAL(6,4)    NULL,
    ExtScore3           DECIMAL(6,4)    NULL,
    ActiveLoans         SMALLINT        NOT NULL    DEFAULT 0,
    ClosedLoans         SMALLINT        NOT NULL    DEFAULT 0,
    EnquiriesYear       SMALLINT        NOT NULL    DEFAULT 0,
    CreatedAt           TIMESTAMP       NOT NULL    DEFAULT NOW(),
    UpdatedAt           TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),   -- FIX-4
    CONSTRAINT FK_Bureau_Client     FOREIGN KEY (ClientID) REFERENCES public.Clients(ClientID),
    CONSTRAINT UQ_Bureau_ClientDate UNIQUE (ClientID, SnapshotDate),
    CONSTRAINT CK_ExtScore1         CHECK (ExtScore1 IS NULL OR ExtScore1 BETWEEN 0 AND 1),
    CONSTRAINT CK_ExtScore2         CHECK (ExtScore2 IS NULL OR ExtScore2 BETWEEN 0 AND 1),
    CONSTRAINT CK_ExtScore3         CHECK (ExtScore3 IS NULL OR ExtScore3 BETWEEN 0 AND 1)
);

-- 1.5  Bureau loan detail — external credit line records per client
--      Source: bureau.csv from Home Credit dataset.
--      Used by: bureau_agg CTE in usp_BuildFeatureStoreRaw,
--               bureau_stress CTE in stg.v_NPLFeatures,
--               bureau_signals CTE in product_ownership derivation.
CREATE TABLE IF NOT EXISTS public.BureauLoans (
    BureauLoanID        BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ClientID            BIGINT          NOT NULL,
    CreditActive        VARCHAR(12)     NOT NULL,   -- 'Active', 'Closed', 'Bad debt', 'Sold'
    CreditCurrency      VARCHAR(10)     NULL,
    DaysCredit          INT             NULL,        -- days before application; negative
    DaysCreditEndDate   INT             NULL,        -- days to credit end date
    DaysEndDateFact     INT             NULL,
    AMT_CreditSum       DECIMAL(18,2)   NULL,        -- total credit limit
    AMT_CreditSumDebt   DECIMAL(18,2)   NULL,        -- current debt outstanding
    AMT_CreditSumLimit  DECIMAL(18,2)   NULL,
    AMT_CreditSumOverdue DECIMAL(18,2)  NULL,
    AMT_CreditMaxOverdue DECIMAL(18,2)  NULL,
    CNT_CreditProlong   SMALLINT        NULL,        -- number of credit prolongations
    CreditType          VARCHAR(50)     NULL,
    CreatedAt           TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),
    CONSTRAINT FK_BureauLoans_Client FOREIGN KEY (ClientID) REFERENCES public.Clients(ClientID),
    CONSTRAINT CK_BureauLoans_Active CHECK (CreditActive IN ('Active','Closed','Bad debt','Sold'))
);

-- 1.6  Previous applications — prior loan applications at this lender
--      Source: previous_application.csv from Home Credit dataset.
--      Used by: prev_agg CTE in usp_BuildFeatureStoreRaw,
--               prev_signals CTE in product_ownership derivation.
CREATE TABLE IF NOT EXISTS public.PreviousApplications (
    PrevAppID           BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ClientID            BIGINT          NOT NULL,
    ContractType        VARCHAR(30)     NULL,        -- 'Cash loans', 'Consumer loans', 'Revolving loans'
    ContractStatus      VARCHAR(20)     NULL,        -- 'Approved', 'Refused', 'Canceled', 'Unused offer'
    AMT_Credit          DECIMAL(18,2)   NULL,
    AMT_Annuity         DECIMAL(18,2)   NULL,
    AMT_DownPayment     DECIMAL(18,2)   NULL,
    AMT_GoodsPrice      DECIMAL(18,2)   NULL,
    DaysDecision        INT             NULL,        -- days before current application; negative
    ChannelType         VARCHAR(30)     NULL,
    CreatedAt           TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),
    CONSTRAINT FK_PrevApp_Client FOREIGN KEY (ClientID) REFERENCES public.Clients(ClientID)
);

-- 1.7  Installments payments — repayment history per previous application
--      Source: installments_payments.csv from Home Credit dataset.
--      Used by: stg.v_InstallmentDPD, inst_agg CTE in usp_BuildFeatureStoreRaw.
--      NOTE: surrogate PK (not composite on PrevAppID+DaysInstalment) to avoid
--            duplicate key violations where multiple payments share the same date.
CREATE TABLE IF NOT EXISTS public.InstallmentsPayments (
    InstPayID           BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ClientID            BIGINT          NOT NULL,
    PrevAppID           BIGINT          NOT NULL,
    DaysInstalment      INT             NULL,        -- scheduled instalment date (negative = past)
    DaysEntryPayment    INT             NULL,        -- actual payment date; NULL = not yet paid
    AMT_Instalment      DECIMAL(18,2)   NULL,        -- scheduled amount
    AMT_Payment         DECIMAL(18,2)   NULL,        -- actual amount paid
    CreatedAt           TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),
    CONSTRAINT FK_InstPay_Client  FOREIGN KEY (ClientID)  REFERENCES public.Clients(ClientID),
    CONSTRAINT FK_InstPay_PrevApp FOREIGN KEY (PrevAppID) REFERENCES public.PreviousApplications(PrevAppID)
);

-- 1.8  POS Cash Balance — monthly POS/cash loan snapshots
--      Source: POS_CASH_balance.csv. Seeded by seed_pos_cash_pg.py.
--      Used by: pos_agg CTE in usp_BuildFeatureStoreRaw and stg.v_NPLFeatures.
CREATE TABLE IF NOT EXISTS public.POS_CashBalance (
    PosID                   BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ClientID                BIGINT          NOT NULL,
    PrevAppID               BIGINT          NOT NULL,
    MonthsBalance           INT             NOT NULL,   -- negative = months before application
    CNT_Instalment          DECIMAL(10,2)   NULL,
    CNT_Instalment_Future   DECIMAL(10,2)   NULL,
    NAME_Contract_Status    VARCHAR(20)     NULL,
    SK_DPD                  SMALLINT        NOT NULL    DEFAULT 0,
    SK_DPD_DEF              SMALLINT        NOT NULL    DEFAULT 0,
    CreatedAt               TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),
    CONSTRAINT FK_POS_Client  FOREIGN KEY (ClientID)  REFERENCES public.Clients(ClientID),
    CONSTRAINT FK_POS_PrevApp FOREIGN KEY (PrevAppID) REFERENCES public.PreviousApplications(PrevAppID)
);

-- 1.9  Products catalogue — 13 banking products for NBO scoring
--      FK target for ml.NBO_Scores; must exist before NBO tables.
CREATE TABLE IF NOT EXISTS public.Products (
    ProductID       INT             NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ProductCode     VARCHAR(30)     NOT NULL    UNIQUE,
    ProductName     VARCHAR(100)    NOT NULL,
    Category        VARCHAR(20)     NOT NULL,
    -- MaxPD_Score: if client PD exceeds this threshold, suppress offer.
    -- NULL = no PD gate (e.g. CORE products like checking accounts).
    MaxPD_Score     DECIMAL(6,4)    NULL,
    CooldownDays    SMALLINT        NOT NULL    DEFAULT 7,
    IsActive        BOOLEAN         NOT NULL    DEFAULT TRUE,
    ValidFrom       DATE            NOT NULL    DEFAULT CURRENT_DATE,
    ValidTo         DATE            NULL,
    CONSTRAINT CK_Product_Category CHECK (Category IN ('CORE','CREDIT','LOAN','WEALTH','INSURANCE'))
);

-- Seed the 13 products
INSERT INTO public.Products (ProductCode, ProductName, Category, MaxPD_Score, CooldownDays) VALUES
    ('checking_account',    'Current Account',    'CORE',      NULL,  3),
    ('savings_account',     'Savings Account',    'CORE',      NULL,  7),
    ('debit_card',          'Debit Card',         'CORE',      NULL,  3),
    ('credit_card',         'Credit Card',        'CREDIT',    0.391, 7),
    ('overdraft_facility',  'Overdraft Facility', 'CREDIT',    0.611, 14),
    ('personal_loan',       'Personal Loan',      'LOAN',      0.391, 30),
    ('revolving_loan',      'Revolving Credit',   'LOAN',      0.611, 30),
    ('mortgage',            'Mortgage',           'LOAN',      0.391, 30),
    ('auto_loan',           'Auto Loan',          'LOAN',      0.611, 30),
    ('term_deposit',        'Term Deposit',       'WEALTH',    NULL,  14),
    ('investment_fund',     'Investment Fund',    'WEALTH',    NULL,  14),
    ('life_insurance',      'Life Insurance',     'INSURANCE', NULL,  30),
    ('travel_insurance',    'Travel Insurance',   'INSURANCE', NULL,  7)
ON CONFLICT (ProductCode) DO NOTHING;

-- 1.10  NBO client product ownership — derived monthly
--       Logic: see nbo_train.py (product_ownership derivation).
--       Columns are binary flags (1/0) per product.
CREATE TABLE IF NOT EXISTS public.NBO_ClientOwnership (
    ClientID                BIGINT      NOT NULL,
    SnapshotMonth           DATE        NOT NULL,
    has_checking            SMALLINT    NOT NULL    DEFAULT 0,
    has_savings             SMALLINT    NOT NULL    DEFAULT 0,
    has_debit_card          SMALLINT    NOT NULL    DEFAULT 0,
    has_credit_card         SMALLINT    NOT NULL    DEFAULT 0,
    has_overdraft           SMALLINT    NOT NULL    DEFAULT 0,
    has_personal_loan       SMALLINT    NOT NULL    DEFAULT 0,
    has_revolving_loan      SMALLINT    NOT NULL    DEFAULT 0,
    has_mortgage            SMALLINT    NOT NULL    DEFAULT 0,
    has_auto_loan           SMALLINT    NOT NULL    DEFAULT 0,
    has_term_deposit        SMALLINT    NOT NULL    DEFAULT 0,
    has_investment          SMALLINT    NOT NULL    DEFAULT 0,
    has_life_insurance      SMALLINT    NOT NULL    DEFAULT 0,
    has_travel_insurance    SMALLINT    NOT NULL    DEFAULT 0,
    n_products              SMALLINT    NOT NULL    DEFAULT 0,
    -- cold_start: TRUE if client holds fewer than 2 substantive products
    -- (beyond core checking/debit). Used as fallback method in NBO scoring.
    cold_start              BOOLEAN     NOT NULL    DEFAULT FALSE,
    ETL_RunID               BIGINT      NULL,
    CreatedAt               TIMESTAMPTZ NOT NULL    DEFAULT NOW(),
    CONSTRAINT PK_NBO_Ownership PRIMARY KEY (ClientID, SnapshotMonth)
);

-- 1.11  NBO accepted offers — conversion tracking
CREATE TABLE IF NOT EXISTS public.NBO_Accepted (
    AcceptanceID        BIGINT      NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ClientID            BIGINT      NOT NULL,
    ProductID           INT         NOT NULL,
    NBO_ScoreID         BIGINT      NULL,
    OfferDate           DATE        NOT NULL,
    AcceptedDate        DATE        NULL,
    AcceptedFlag        BOOLEAN     NOT NULL    DEFAULT FALSE,
    ChannelCode         VARCHAR(20) NULL,
    ApplicationID_New   BIGINT      NULL,
    CreatedAt           TIMESTAMPTZ NOT NULL    DEFAULT NOW(),
    CONSTRAINT FK_NBO_Acc_Client  FOREIGN KEY (ClientID)  REFERENCES public.Clients(ClientID),
    CONSTRAINT FK_NBO_Acc_Product FOREIGN KEY (ProductID) REFERENCES public.Products(ProductID)
);


-- ============================================================
-- 2. STAGING TABLES  (stg.*)
-- ============================================================

-- 2.1  ETL run log — every pipeline step writes a row here
CREATE TABLE IF NOT EXISTS stg.ETL_RunLog (
    RunID               BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    BatchID             UUID            NOT NULL    DEFAULT gen_random_uuid(),
    RunDate             DATE            NOT NULL    DEFAULT CURRENT_DATE,
    StepName            VARCHAR(50)     NOT NULL,
    Status              VARCHAR(20)     NOT NULL,
    RowsProcessed       INT             NULL,
    DurationSec         INT             NULL,
    ErrorMessage        TEXT            NULL,
    StartedAt           TIMESTAMP       NOT NULL    DEFAULT NOW(),
    FinishedAt          TIMESTAMP       NULL,
    CONSTRAINT CK_ETL_Status CHECK (Status IN ('running','success','failed','skipped'))
);

-- 2.2  Validation error log — data-quality firewall
--      Populated by stg.usp_ValidateStagedData.
--      CRITICAL rows halt the pipeline before scoring.
CREATE TABLE IF NOT EXISTS stg.ValidationErrors (
    ErrorID             BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    RunID               BIGINT          NOT NULL,
    RunDate             DATE            NOT NULL,
    SourceTable         VARCHAR(60)     NOT NULL,
    CheckName           VARCHAR(60)     NOT NULL,
    Severity            VARCHAR(10)     NOT NULL    DEFAULT 'CRITICAL',
    FailedRowKey        VARCHAR(50)     NULL,
    FailedValue         VARCHAR(200)    NULL,
    ExpectedValue       VARCHAR(200)    NULL,
    CreatedAt           TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT FK_ValErr_RunLog FOREIGN KEY (RunID) REFERENCES stg.ETL_RunLog(RunID),
    CONSTRAINT CK_ValErr_Sev    CHECK (Severity IN ('CRITICAL','WARNING'))
);

-- 2.3  ETL watermark control — last high-water mark per source table
CREATE TABLE IF NOT EXISTS stg.ETL_Control (
    ControlID               INT         NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    SourceTable             VARCHAR(60) NOT NULL    UNIQUE,
    LastWatermark           TIMESTAMP   NOT NULL    DEFAULT '1970-01-01 00:00:00',
    LastWatermarkSource     VARCHAR(30) NULL,
    LastRunID               BIGINT      NULL,
    UpdatedAt               TIMESTAMP   NOT NULL    DEFAULT NOW(),
    CONSTRAINT FK_ETLCtrl_RunLog FOREIGN KEY (LastRunID) REFERENCES stg.ETL_RunLog(RunID)
);

-- Seed watermark rows for all source tables
INSERT INTO stg.ETL_Control (SourceTable, LastWatermarkSource) VALUES
    ('public.LoanApplications', 'UpdatedAt'),
    ('public.Clients',          'UpdatedAt'),
    ('public.Transactions',     'TxnDate (end-of-day)'),
    ('public.BureauSnapshots',  'UpdatedAt'),           -- FIX-4
    ('public.POS_CashBalance',  'CreatedAt')
ON CONFLICT (SourceTable) DO NOTHING;

-- 2.4  Delta staging tables — incremental ingestion layer.
--      PK is composite (entity_id, ETL_RunID) so every run's changed rows
--      are preserved independently — enables replay and per-run lineage.
--      DeltaAction distinguishes INSERT vs UPDATE for audit dashboards.

CREATE TABLE IF NOT EXISTS stg.Applications_Delta (
    ApplicationID       BIGINT          NOT NULL,
    ClientID            BIGINT          NOT NULL,
    ApplicationDate     DATE            NOT NULL,
    LoanAmount          DECIMAL(18,2)   NOT NULL,
    Annuity             DECIMAL(18,2)   NOT NULL,
    LoanTenorMonths     INT             NOT NULL,
    GoodsPriceAmount    DECIMAL(18,2)   NULL,
    ContractType        VARCHAR(30)     NOT NULL,
    TARGET              BOOLEAN         NULL,
    RowHash             CHAR(64)        NULL,
    DeltaAction         VARCHAR(10)     NOT NULL    DEFAULT 'INSERT',
    ETL_RunID           BIGINT          NOT NULL,
    ETL_LoadedAt        TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT PK_AppDelta        PRIMARY KEY (ApplicationID, ETL_RunID),
    CONSTRAINT FK_AppDelta_RunLog FOREIGN KEY (ETL_RunID) REFERENCES stg.ETL_RunLog(RunID),
    CONSTRAINT CK_AppDelta_Action CHECK (DeltaAction IN ('INSERT','UPDATE'))
);

CREATE TABLE IF NOT EXISTS stg.Clients_Delta (
    ClientID            BIGINT          NOT NULL,
    BirthDate           DATE            NOT NULL,
    Gender              CHAR(1)         NOT NULL,
    EducationType       VARCHAR(50)     NOT NULL,
    FamilyStatus        VARCHAR(50)     NOT NULL,
    IncomeTotal         DECIMAL(18,2)   NOT NULL,
    IncomeType          VARCHAR(50)     NOT NULL,
    OwnCar              BOOLEAN         NOT NULL,
    OwnRealty           BOOLEAN         NOT NULL,
    CNTChildren         SMALLINT        NOT NULL,
    CNTFamilyMembers    SMALLINT        NOT NULL,
    RegionRating        SMALLINT        NOT NULL,
    Days_Employed       INT             NULL,
    RowHash             CHAR(64)        NULL,
    DeltaAction         VARCHAR(10)     NOT NULL    DEFAULT 'INSERT',
    ETL_RunID           BIGINT          NOT NULL,
    ETL_LoadedAt        TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT PK_ClientDelta        PRIMARY KEY (ClientID, ETL_RunID),
    CONSTRAINT FK_ClientDelta_RunLog FOREIGN KEY (ETL_RunID) REFERENCES stg.ETL_RunLog(RunID),
    CONSTRAINT CK_ClientDelta_Action CHECK (DeltaAction IN ('INSERT','UPDATE'))
);

-- Transactions are immutable — no RowHash or DeltaAction needed.
CREATE TABLE IF NOT EXISTS stg.Transactions_Delta (
    TxnID               BIGINT          NOT NULL,
    ClientID            BIGINT          NOT NULL,
    TxnDate             DATE            NOT NULL,
    TxnType             VARCHAR(20)     NOT NULL,
    Amount              DECIMAL(18,2)   NOT NULL,
    Balance             DECIMAL(18,2)   NOT NULL,
    Channel             VARCHAR(20)     NULL,
    ETL_RunID           BIGINT          NOT NULL,
    ETL_LoadedAt        TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT PK_TxnDelta        PRIMARY KEY (TxnDate, TxnID, ETL_RunID),
    CONSTRAINT FK_TxnDelta_RunLog FOREIGN KEY (ETL_RunID) REFERENCES stg.ETL_RunLog(RunID)
);

CREATE TABLE IF NOT EXISTS stg.Bureau_Delta (
    SnapshotID          BIGINT          NOT NULL,
    ClientID            BIGINT          NOT NULL,
    SnapshotDate        DATE            NOT NULL,
    ExtScore1           DECIMAL(6,4)    NULL,
    ExtScore2           DECIMAL(6,4)    NULL,
    ExtScore3           DECIMAL(6,4)    NULL,
    ActiveLoans         SMALLINT        NOT NULL,
    ClosedLoans         SMALLINT        NOT NULL,
    EnquiriesYear       SMALLINT        NOT NULL,
    ETL_RunID           BIGINT          NOT NULL,
    ETL_LoadedAt        TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT PK_BurDelta        PRIMARY KEY (SnapshotID, ETL_RunID),
    CONSTRAINT FK_BurDelta_RunLog FOREIGN KEY (ETL_RunID) REFERENCES stg.ETL_RunLog(RunID)
);

CREATE TABLE IF NOT EXISTS stg.POS_Delta (
    PosID               BIGINT          NOT NULL,
    ETL_RunID           BIGINT          NOT NULL,
    ClientID            BIGINT          NOT NULL,
    PrevAppID           BIGINT          NOT NULL,
    MonthsBalance       INT             NOT NULL,
    SK_DPD              SMALLINT        NOT NULL,
    SK_DPD_DEF          SMALLINT        NOT NULL,
    NAME_Contract_Status VARCHAR(20)    NULL,
    ETL_LoadedAt        TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),
    CONSTRAINT PK_POSDelta        PRIMARY KEY (PosID, ETL_RunID),
    CONSTRAINT FK_POSDelta_RunLog FOREIGN KEY (ETL_RunID) REFERENCES stg.ETL_RunLog(RunID)
);

-- 2.5  Intermediate feature store — SQL-computed aggregates.
--      Python ETL reads this and computes derived ratio features on top.
--      All 138-feature model passthrough columns included.
CREATE TABLE IF NOT EXISTS stg.FeatureStore_Raw (
    ClientID                        BIGINT          NOT NULL,
    AsOfDate                        DATE            NOT NULL,
    -- ── Transaction aggregates (90-day window) ──────────────────────
    AvgBalance_90d                  DECIMAL(18,2)   NULL,
    MinBalance_90d                  DECIMAL(18,2)   NULL,
    MaxBalance_90d                  DECIMAL(18,2)   NULL,
    SalaryCredits_90d               SMALLINT        NULL,
    OverdraftDays_90d               SMALLINT        NULL,
    TxnCount_90d                    INT             NULL,
    AvgTxnAmount_90d                DECIMAL(18,2)   NULL,
    CashWithdraws_90d               SMALLINT        NULL,
    -- ── Client master ───────────────────────────────────────────────
    BirthDate                       DATE            NULL,
    Gender                          CHAR(1)         NULL,
    EducationType                   VARCHAR(50)     NULL,
    FamilyStatus                    VARCHAR(50)     NULL,
    IncomeTotal                     DECIMAL(18,2)   NULL,
    IncomeType                      VARCHAR(50)     NULL,
    OwnCar                          BOOLEAN         NULL,
    OwnRealty                       BOOLEAN         NULL,
    CNTChildren                     SMALLINT        NULL,
    CNTFamilyMembers                SMALLINT        NULL,
    RegionRating                    SMALLINT        NULL,
    Days_Employed                   INT             NULL,
    NAME_HOUSING_TYPE               VARCHAR(50)     NULL,
    REGION_POPULATION_RELATIVE      DECIMAL(10,8)   NULL,
    DAYS_REGISTRATION               INT             NULL,
    DAYS_ID_PUBLISH                 INT             NULL,
    FLAG_MOBIL                      SMALLINT        NULL,
    FLAG_WORK_PHONE                 SMALLINT        NULL,
    FLAG_CONT_MOBILE                SMALLINT        NULL,
    FLAG_PHONE                      SMALLINT        NULL,
    FLAG_EMAIL                      SMALLINT        NULL,
    OCCUPATION_TYPE                 VARCHAR(50)     NULL,
    ORGANIZATION_TYPE               VARCHAR(60)     NULL,
    REG_REGION_NOT_LIVE_REGION      SMALLINT        NULL,
    REG_REGION_NOT_WORK_REGION      SMALLINT        NULL,
    LIVE_REGION_NOT_WORK_REGION     SMALLINT        NULL,
    REG_CITY_NOT_LIVE_CITY          SMALLINT        NULL,
    REG_CITY_NOT_WORK_CITY          SMALLINT        NULL,
    LIVE_CITY_NOT_WORK_CITY         SMALLINT        NULL,
    YEARS_BEGINEXPLUATATION_AVG     DECIMAL(6,4)    NULL,
    FLOORSMAX_AVG                   DECIMAL(6,4)    NULL,
    TOTALAREA_MODE                  DECIMAL(10,4)   NULL,
    EMERGENCYSTATE_MODE             VARCHAR(3)      NULL,
    OBS_30_CNT_SOCIAL_CIRCLE        SMALLINT        NULL,
    DEF_30_CNT_SOCIAL_CIRCLE        SMALLINT        NULL,
    DEF_60_CNT_SOCIAL_CIRCLE        SMALLINT        NULL,
    -- ── Latest application ──────────────────────────────────────────
    ApplicationID                   BIGINT          NULL,
    ApplicationDate                 DATE            NULL,
    LoanAmount                      DECIMAL(18,2)   NULL,
    Annuity                         DECIMAL(18,2)   NULL,
    LoanTenorMonths                 INT             NULL,
    GoodsPriceAmount                DECIMAL(18,2)   NULL,
    ContractType                    VARCHAR(30)     NULL,
    NAME_TYPE_SUITE                 VARCHAR(40)     NULL,
    WEEKDAY_APPR_PROCESS_START      VARCHAR(12)     NULL,
    HOUR_APPR_PROCESS_START         SMALLINT        NULL,
    DAYS_LAST_PHONE_CHANGE          INT             NULL,
    FLAG_DOCUMENT_2                 SMALLINT        NULL,
    FLAG_DOCUMENT_3                 SMALLINT        NULL,
    FLAG_DOCUMENT_4                 SMALLINT        NULL,
    FLAG_DOCUMENT_5                 SMALLINT        NULL,
    FLAG_DOCUMENT_6                 SMALLINT        NULL,
    FLAG_DOCUMENT_7                 SMALLINT        NULL,
    FLAG_DOCUMENT_8                 SMALLINT        NULL,
    FLAG_DOCUMENT_9                 SMALLINT        NULL,
    FLAG_DOCUMENT_10                SMALLINT        NULL,
    FLAG_DOCUMENT_11                SMALLINT        NULL,
    FLAG_DOCUMENT_12                SMALLINT        NULL,
    FLAG_DOCUMENT_13                SMALLINT        NULL,
    FLAG_DOCUMENT_14                SMALLINT        NULL,
    FLAG_DOCUMENT_15                SMALLINT        NULL,
    FLAG_DOCUMENT_16                SMALLINT        NULL,
    FLAG_DOCUMENT_17                SMALLINT        NULL,
    FLAG_DOCUMENT_18                SMALLINT        NULL,
    FLAG_DOCUMENT_19                SMALLINT        NULL,
    FLAG_DOCUMENT_20                SMALLINT        NULL,
    FLAG_DOCUMENT_21                SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_HOUR      SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_DAY       SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_WEEK      SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_MON       SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_QRT       SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_YEAR      SMALLINT        NULL,
    -- ── Latest bureau snapshot ──────────────────────────────────────
    ExtScore1                       DECIMAL(6,4)    NULL,
    ExtScore2                       DECIMAL(6,4)    NULL,
    ExtScore3                       DECIMAL(6,4)    NULL,
    ActiveLoans                     SMALLINT        NULL,
    ClosedLoans                     SMALLINT        NULL,
    EnquiriesYear                   SMALLINT        NULL,
    -- ── Bureau loan aggregations (from public.BureauLoans) ──────────
    bureau_loan_count               INT             NULL,
    bureau_active_count             INT             NULL,
    bureau_closed_count             INT             NULL,
    bureau_bad_debt_count           INT             NULL,
    bureau_avg_days_credit          DECIMAL(10,2)   NULL,
    bureau_avg_days_enddate         DECIMAL(10,2)   NULL,
    bureau_total_debt               DECIMAL(18,2)   NULL,
    bureau_total_limit              DECIMAL(18,2)   NULL,
    bureau_total_overdue            DECIMAL(18,2)   NULL,
    bureau_max_overdue              DECIMAL(18,2)   NULL,
    bureau_prolong_count            INT             NULL,
    bureau_last_credit              INT             NULL,
    bureau_first_credit             INT             NULL,
    bureau_credit_span              INT             NULL,
    bureau_days_credit_std          DECIMAL(10,4)   NULL,
    bureau_debt_std                 DECIMAL(18,4)   NULL,
    -- ── Previous application aggregations ───────────────────────────
    prev_app_count                  INT             NULL,
    prev_approved_count             INT             NULL,
    prev_refused_count              INT             NULL,
    prev_avg_credit                 DECIMAL(18,2)   NULL,
    prev_avg_annuity                DECIMAL(18,2)   NULL,
    prev_avg_down_payment           DECIMAL(18,2)   NULL,
    prev_avg_goods_price            DECIMAL(18,2)   NULL,
    prev_max_days_decision          INT             NULL,
    prev_avg_days_decision          DECIMAL(10,2)   NULL,
    prev_consumer_count             INT             NULL,
    prev_cash_count                 INT             NULL,
    prev_revolving_count            INT             NULL,
    prev_first_app                  INT             NULL,
    prev_app_span                   INT             NULL,
    prev_days_std                   DECIMAL(10,4)   NULL,
    prev_credit_std                 DECIMAL(18,4)   NULL,
    -- ── Installment aggregations ─────────────────────────────────────
    inst_count                      INT             NULL,
    inst_avg_payment                DECIMAL(18,2)   NULL,
    inst_max_payment                DECIMAL(18,2)   NULL,
    inst_min_payment                DECIMAL(18,2)   NULL,
    inst_avg_days_delay             DECIMAL(10,4)   NULL,
    inst_max_days_delay             DECIMAL(10,4)   NULL,
    -- ── POS Cash aggregations ────────────────────────────────────────
    pos_count                       INT             NULL,
    pos_months_avg                  DECIMAL(10,4)   NULL,
    pos_dpd_avg                     DECIMAL(10,4)   NULL,
    pos_dpd_max                     SMALLINT        NULL,
    pos_dpd_def_avg                 DECIMAL(10,4)   NULL,
    pos_completed                   INT             NULL,
    -- ── Audit ────────────────────────────────────────────────────────
    ETL_RunID                       BIGINT          NOT NULL,
    CreatedAt                       TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT PK_FeatureStoreRaw     PRIMARY KEY (ClientID, AsOfDate),
    CONSTRAINT FK_FeatStoreRaw_RunLog FOREIGN KEY (ETL_RunID) REFERENCES stg.ETL_RunLog(RunID)
);


-- ============================================================
-- 3. MODEL DIMENSION TABLE  (ml.Models)
-- ============================================================
-- Central registry for all model version strings.
-- Every table that stores a ModelVersion FK references this table,
-- preventing orphaned or mistyped version keys.
-- Insert a row here before any scoring, PSI, or encoder writes.
CREATE TABLE IF NOT EXISTS ml.Models (
    ModelVersion        VARCHAR(30)     NOT NULL    PRIMARY KEY,
    MLflowRunID         VARCHAR(40)     NOT NULL,
    RegisteredAt        TIMESTAMP       NOT NULL    DEFAULT NOW(),
    Notes               VARCHAR(500)    NULL
);

-- Seed known model versions
INSERT INTO ml.Models (ModelVersion, MLflowRunID, Notes) VALUES
    ('v2-lgbm',  'local', 'LightGBM PD champion — 27 pipeline-aligned features, AUC 0.785'),
    ('npl-v1',   'local', 'XGBoost NPL early warning — TARGET label, out-of-time split, AUC 0.771'),
    ('nbo-v1',   'local', 'NearestNeighbors NBO recommender — 13 products, Hit@5 0.982')
ON CONFLICT (ModelVersion) DO NOTHING;


-- ============================================================
-- 4. FEATURE STORE  (ml.*)
-- ============================================================

-- 4.1  Final feature store — Python-computed derived features.
--      PK is (ClientID, AsOfDate, ModelVersion) — FIX-1:
--      ModelVersion added so different model generations can
--      coexist without overwriting each other's feature vectors.
CREATE TABLE IF NOT EXISTS ml.FeatureStore (
    ClientID                        BIGINT          NOT NULL,
    AsOfDate                        DATE            NOT NULL,
    ModelVersion                    VARCHAR(30)     NOT NULL,   -- FIX-1
    -- ── Derived ratio features (Python-computed) ─────────────────────
    AgeYears                        DECIMAL(5,2)    NULL,
    EmployedYears                   DECIMAL(6,2)    NULL,
    DebtToIncome                    DECIMAL(8,4)    NULL,
    CreditToIncome                  DECIMAL(8,4)    NULL,
    AnnuityToIncome                 DECIMAL(8,4)    NULL,
    CreditToGoods                   DECIMAL(8,4)    NULL,
    ExtSourceMean                   DECIMAL(6,4)    NULL,
    BureauLoansTotal                SMALLINT        NULL,
    IncomePerChild                  DECIMAL(18,2)   NULL,
    -- ── Transaction aggregates ───────────────────────────────────────
    AvgBalance_90d                  DECIMAL(18,2)   NULL,
    MinBalance_90d                  DECIMAL(18,2)   NULL,
    SalaryCredits_90d               SMALLINT        NULL,
    OverdraftDays_90d               SMALLINT        NULL,
    TxnCount_90d                    INT             NULL,
    CashWithdraws_90d               SMALLINT        NULL,
    -- ── Raw bureau ───────────────────────────────────────────────────
    ExtScore1                       DECIMAL(6,4)    NULL,
    ExtScore2                       DECIMAL(6,4)    NULL,
    ExtScore3                       DECIMAL(6,4)    NULL,
    ActiveLoans                     SMALLINT        NULL,
    ClosedLoans                     SMALLINT        NULL,
    EnquiriesYear                   SMALLINT        NULL,
    -- ── Raw client flags ─────────────────────────────────────────────
    OwnCar                          BOOLEAN         NULL,
    OwnRealty                       BOOLEAN         NULL,
    CNTChildren                     SMALLINT        NULL,
    CNTFamilyMembers                SMALLINT        NULL,
    RegionRating                    SMALLINT        NULL,
    -- ── Encoded categoricals ─────────────────────────────────────────
    Gender_Enc                      SMALLINT        NULL,
    EducationType_Enc               SMALLINT        NULL,
    FamilyStatus_Enc                SMALLINT        NULL,
    IncomeType_Enc                  SMALLINT        NULL,
    ContractType_Enc                SMALLINT        NULL,
    -- ── Loan terms ───────────────────────────────────────────────────
    LoanAmount                      DECIMAL(18,2)   NULL,
    Annuity                         DECIMAL(18,2)   NULL,
    LoanTenorMonths                 INT             NULL,
    -- ── 138-feature extension columns ────────────────────────────────
    -- Raw application features
    NAME_CONTRACT_TYPE              SMALLINT        NULL,
    FLAG_OWN_CAR                    SMALLINT        NULL,
    FLAG_OWN_REALTY                 SMALLINT        NULL,
    AMT_INCOME_TOTAL                DECIMAL(18,2)   NULL,
    AMT_CREDIT                      DECIMAL(18,2)   NULL,
    AMT_ANNUITY                     DECIMAL(18,2)   NULL,
    NAME_TYPE_SUITE                 SMALLINT        NULL,
    NAME_INCOME_TYPE                SMALLINT        NULL,
    NAME_EDUCATION_TYPE             SMALLINT        NULL,
    NAME_HOUSING_TYPE               SMALLINT        NULL,
    REGION_POPULATION_RELATIVE      DECIMAL(10,8)   NULL,
    DAYS_BIRTH                      INT             NULL,
    DAYS_EMPLOYED                   INT             NULL,
    DAYS_REGISTRATION               INT             NULL,
    DAYS_ID_PUBLISH                 INT             NULL,
    FLAG_MOBIL                      SMALLINT        NULL,
    FLAG_WORK_PHONE                 SMALLINT        NULL,
    FLAG_CONT_MOBILE                SMALLINT        NULL,
    FLAG_PHONE                      SMALLINT        NULL,
    FLAG_EMAIL                      SMALLINT        NULL,
    OCCUPATION_TYPE                 SMALLINT        NULL,
    CNT_FAM_MEMBERS                 SMALLINT        NULL,
    REGION_RATING_CLIENT            SMALLINT        NULL,
    WEEKDAY_APPR_PROCESS_START      SMALLINT        NULL,
    HOUR_APPR_PROCESS_START         SMALLINT        NULL,
    REG_REGION_NOT_LIVE_REGION      SMALLINT        NULL,
    REG_REGION_NOT_WORK_REGION      SMALLINT        NULL,
    LIVE_REGION_NOT_WORK_REGION     SMALLINT        NULL,
    REG_CITY_NOT_LIVE_CITY          SMALLINT        NULL,
    REG_CITY_NOT_WORK_CITY          SMALLINT        NULL,
    LIVE_CITY_NOT_WORK_CITY         SMALLINT        NULL,
    ORGANIZATION_TYPE               SMALLINT        NULL,
    EXT_SOURCE_2                    DECIMAL(6,4)    NULL,
    EXT_SOURCE_3                    DECIMAL(6,4)    NULL,
    YEARS_BEGINEXPLUATATION_AVG     DECIMAL(6,4)    NULL,
    FLOORSMAX_AVG                   DECIMAL(6,4)    NULL,
    TOTALAREA_MODE                  DECIMAL(10,4)   NULL,
    EMERGENCYSTATE_MODE             SMALLINT        NULL,
    OBS_30_CNT_SOCIAL_CIRCLE        SMALLINT        NULL,
    DEF_30_CNT_SOCIAL_CIRCLE        SMALLINT        NULL,
    DEF_60_CNT_SOCIAL_CIRCLE        SMALLINT        NULL,
    DAYS_LAST_PHONE_CHANGE          INT             NULL,
    FLAG_DOCUMENT_2                 SMALLINT        NULL,
    FLAG_DOCUMENT_3                 SMALLINT        NULL,
    FLAG_DOCUMENT_4                 SMALLINT        NULL,
    FLAG_DOCUMENT_5                 SMALLINT        NULL,
    FLAG_DOCUMENT_6                 SMALLINT        NULL,
    FLAG_DOCUMENT_7                 SMALLINT        NULL,
    FLAG_DOCUMENT_8                 SMALLINT        NULL,
    FLAG_DOCUMENT_9                 SMALLINT        NULL,
    FLAG_DOCUMENT_10                SMALLINT        NULL,
    FLAG_DOCUMENT_11                SMALLINT        NULL,
    FLAG_DOCUMENT_12                SMALLINT        NULL,
    FLAG_DOCUMENT_13                SMALLINT        NULL,
    FLAG_DOCUMENT_14                SMALLINT        NULL,
    FLAG_DOCUMENT_15                SMALLINT        NULL,
    FLAG_DOCUMENT_16                SMALLINT        NULL,
    FLAG_DOCUMENT_17                SMALLINT        NULL,
    FLAG_DOCUMENT_18                SMALLINT        NULL,
    FLAG_DOCUMENT_19                SMALLINT        NULL,
    FLAG_DOCUMENT_20                SMALLINT        NULL,
    FLAG_DOCUMENT_21                SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_HOUR      SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_DAY       SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_WEEK      SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_MON       SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_QRT       SMALLINT        NULL,
    AMT_REQ_CREDIT_BUREAU_YEAR      SMALLINT        NULL,
    -- Derived ratios
    CREDIT_INCOME_RATIO             DECIMAL(8,4)    NULL,
    ANNUITY_INCOME_RATIO            DECIMAL(8,4)    NULL,
    CREDIT_GOODS_RATIO              DECIMAL(8,4)    NULL,
    ANNUITY_CREDIT_RATIO            DECIMAL(8,4)    NULL,
    EMPLOYMENT_YEARS                DECIMAL(6,2)    NULL,
    LOAN_TENOR_MONTHS               DECIMAL(8,2)    NULL,
    EXT_SOURCE_MEAN                 DECIMAL(6,4)    NULL,
    EXT_SOURCE_MIN                  DECIMAL(6,4)    NULL,
    EXT_SOURCE_PRODUCT              DECIMAL(8,6)    NULL,
    TOTAL_DOC_FLAGS                 SMALLINT        NULL,
    TOTAL_ENQUIRIES                 SMALLINT        NULL,
    -- Bureau aggregations
    bureau_loan_count               INT             NULL,
    bureau_active_count             INT             NULL,
    bureau_closed_count             INT             NULL,
    bureau_bad_debt_count           INT             NULL,
    bureau_avg_days_credit          DECIMAL(10,2)   NULL,
    bureau_avg_days_enddate         DECIMAL(10,2)   NULL,
    bureau_total_debt               DECIMAL(18,2)   NULL,
    bureau_total_limit              DECIMAL(18,2)   NULL,
    bureau_total_overdue            DECIMAL(18,2)   NULL,
    bureau_max_overdue              DECIMAL(18,2)   NULL,
    bureau_prolong_count            INT             NULL,
    bureau_debt_limit_ratio         DECIMAL(8,4)    NULL,
    bureau_active_ratio_x           DECIMAL(6,4)    NULL,
    bureau_overdue_ratio            DECIMAL(8,4)    NULL,
    bureau_last_credit              INT             NULL,
    bureau_first_credit             INT             NULL,
    bureau_credit_span              INT             NULL,
    bureau_days_credit_std          DECIMAL(10,4)   NULL,
    bureau_debt_std                 DECIMAL(18,4)   NULL,
    bureau_active_mean              DECIMAL(6,4)    NULL,
    bureau_stability                DECIMAL(8,4)    NULL,
    -- Previous application aggregations
    prev_app_count                  INT             NULL,
    prev_approved_count             INT             NULL,
    prev_refused_count              INT             NULL,
    prev_avg_credit                 DECIMAL(18,2)   NULL,
    prev_avg_annuity                DECIMAL(18,2)   NULL,
    prev_avg_down_payment           DECIMAL(18,2)   NULL,
    prev_avg_goods_price            DECIMAL(18,2)   NULL,
    prev_max_days_decision          INT             NULL,
    prev_avg_days_decision          DECIMAL(10,2)   NULL,
    prev_consumer_count             INT             NULL,
    prev_cash_count                 INT             NULL,
    prev_revolving_count            INT             NULL,
    prev_approval_rate              DECIMAL(6,4)    NULL,
    prev_refusal_rate               DECIMAL(6,4)    NULL,
    prev_first_app                  INT             NULL,
    prev_app_span                   INT             NULL,
    prev_days_std                   DECIMAL(10,4)   NULL,
    prev_credit_std                 DECIMAL(18,4)   NULL,
    prev_app_frequency              DECIMAL(8,4)    NULL,
    -- Installment aggregations
    inst_count                      INT             NULL,
    inst_avg_payment                DECIMAL(18,2)   NULL,
    inst_max_payment                DECIMAL(18,2)   NULL,
    inst_min_payment                DECIMAL(18,2)   NULL,
    inst_avg_days_delay             DECIMAL(10,4)   NULL,
    inst_max_days_delay             DECIMAL(10,4)   NULL,
    inst_payment_ratio              DECIMAL(8,4)    NULL,
    inst_payment_gap                DECIMAL(18,4)   NULL,
    inst_delay_risk                 DECIMAL(10,4)   NULL,
    -- POS Cash aggregations
    pos_count                       INT             NULL,
    pos_months_avg                  DECIMAL(10,4)   NULL,
    pos_dpd_avg                     DECIMAL(10,4)   NULL,
    pos_dpd_max                     SMALLINT        NULL,
    pos_dpd_def_avg                 DECIMAL(10,4)   NULL,
    pos_completed                   INT             NULL,
    pos_active_ratio                DECIMAL(6,4)    NULL,
    -- ── Audit ────────────────────────────────────────────────────────
    ETL_RunID                       BIGINT          NOT NULL,
    CreatedAt                       TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT PK_FeatureStore  PRIMARY KEY (ClientID, AsOfDate, ModelVersion),  -- FIX-1
    CONSTRAINT FK_FS_RunLog     FOREIGN KEY (ETL_RunID)     REFERENCES stg.ETL_RunLog(RunID),
    CONSTRAINT FK_FS_Model      FOREIGN KEY (ModelVersion)  REFERENCES ml.Models(ModelVersion)
);

-- 4.2  Model Inputs Snapshot — exact feature vector per scored application.
--      Answers "Why did THIS customer get this score?" Partitioned by ScoreDate.
CREATE TABLE IF NOT EXISTS ml.ModelInputs_Snapshots (
    SnapshotID      BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY,
    ApplicationID   BIGINT          NOT NULL,
    ClientID        BIGINT          NOT NULL,
    ScoreDate       DATE            NOT NULL,
    ModelVersion    VARCHAR(30)     NOT NULL,
    FeatureVersion  VARCHAR(30)     NOT NULL,
    FeatureVector   JSONB           NOT NULL,
    ETL_RunID       BIGINT          NOT NULL,
    CreatedAt       TIMESTAMP       NOT NULL    DEFAULT NOW(),
    PRIMARY KEY (SnapshotID, ScoreDate),
    CONSTRAINT UQ_ModelInputs_AppDate UNIQUE (ApplicationID, ScoreDate, ModelVersion),
    CONSTRAINT FK_ModelInputs_App     FOREIGN KEY (ApplicationID) REFERENCES public.LoanApplications(ApplicationID),
    CONSTRAINT FK_ModelInputs_Client  FOREIGN KEY (ClientID)      REFERENCES public.Clients(ClientID),
    CONSTRAINT FK_ModelInputs_RunLog  FOREIGN KEY (ETL_RunID)     REFERENCES stg.ETL_RunLog(RunID),
    CONSTRAINT FK_ModelInputs_Model   FOREIGN KEY (ModelVersion)  REFERENCES ml.Models(ModelVersion)
) PARTITION BY RANGE (ScoreDate);

DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT
            generate_series::DATE                           AS lo,
            (generate_series + INTERVAL '1 month')::DATE   AS hi,
            TO_CHAR(generate_series, 'YYYY_MM')            AS suffix
        FROM generate_series(DATE '2024-01-01', DATE '2026-07-01', INTERVAL '1 month')
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_tables
            WHERE schemaname = 'ml' AND tablename = 'modelinputs_snapshots_' || lower(r.suffix)
        ) THEN
            EXECUTE format(
                'CREATE TABLE ml.ModelInputs_Snapshots_%s
                 PARTITION OF ml.ModelInputs_Snapshots
                 FOR VALUES FROM (%L) TO (%L)',
                r.suffix, r.lo, r.hi
            );
        END IF;
    END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS ml.ModelInputs_Snapshots_default
    PARTITION OF ml.ModelInputs_Snapshots DEFAULT;

-- 4.3  Training Dataset Registry
--      Answers "What data was used to train model vN?"
--      Required under EBA GL/2021/01 model reproducibility obligations.
CREATE TABLE IF NOT EXISTS ml.TrainingDatasetRegistry (
    TrainingID              BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ModelVersion            VARCHAR(30)     NOT NULL,
    TrainingRunID           VARCHAR(40)     NOT NULL,
    -- SHA-256 hex digest of the exact SQL used to pull training data.
    -- Auditors can verify by re-hashing the query in the training notebook.
    DataQueryHash           TEXT            NOT NULL,
    TrainingWindowStart     DATE            NOT NULL,
    TrainingWindowEnd       DATE            NOT NULL,
    RowCount                INT             NOT NULL,
    TargetMean              DECIMAL(8,4)    NOT NULL,
    PositiveRate            DECIMAL(8,4)    NOT NULL,
    SamplingStrategy        VARCHAR(100)    NULL,
    -- FIX-3: links training record to the governance cycle that approved it.
    -- NULL is allowed for records written in the same transaction as the cycle.
    GovernanceCycleID       INT             NULL,
    Notes                   TEXT            NULL,
    CreatedAt               TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT UQ_TrainingDataset    UNIQUE (ModelVersion, TrainingRunID),
    CONSTRAINT FK_Training_Model     FOREIGN KEY (ModelVersion)      REFERENCES ml.Models(ModelVersion)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT FK_Training_ModelReg  FOREIGN KEY (GovernanceCycleID) REFERENCES ml.ModelRegistry(CycleID)
        DEFERRABLE INITIALLY DEFERRED
);


-- ============================================================
-- 5. OUTPUT TABLES  (ml.*)
-- ============================================================

-- 5.1  PD Scores — nightly output of the LightGBM PD model
--      RiskTier values: 'LOW', 'MEDIUM', 'HIGH' (percentile-based thresholds)
CREATE TABLE IF NOT EXISTS ml.PD_Scores (
    ScoreID             BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ApplicationID       BIGINT          NOT NULL,
    ClientID            BIGINT          NOT NULL,
    ScoreDate           DATE            NOT NULL    DEFAULT CURRENT_DATE,
    PD_Score            DECIMAL(9,7)    NOT NULL,
    RiskTier            VARCHAR(10)     NOT NULL,
    ScoreDecile         SMALLINT        NOT NULL,
    ModelVersion        VARCHAR(30)     NOT NULL,
    MLflowRunID         VARCHAR(40)     NOT NULL,
    ETL_RunID           BIGINT          NOT NULL,
    CreatedAt           TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT CK_PDScore     CHECK (PD_Score BETWEEN 0 AND 1),
    CONSTRAINT CK_RiskTier    CHECK (RiskTier IN ('LOW','MEDIUM','HIGH')),
    CONSTRAINT CK_ScoreDecile CHECK (ScoreDecile BETWEEN 1 AND 10),
    CONSTRAINT UQ_PD_AppDate  UNIQUE (ApplicationID, ScoreDate),
    CONSTRAINT FK_PD_App      FOREIGN KEY (ApplicationID) REFERENCES public.LoanApplications(ApplicationID),
    CONSTRAINT FK_PD_Client   FOREIGN KEY (ClientID)      REFERENCES public.Clients(ClientID),
    CONSTRAINT FK_PD_RunLog   FOREIGN KEY (ETL_RunID)     REFERENCES stg.ETL_RunLog(RunID),
    CONSTRAINT FK_PD_Model    FOREIGN KEY (ModelVersion)  REFERENCES ml.Models(ModelVersion)
);

-- 5.2  SHAP Values — top-5 features per scored application
--      ON DELETE CASCADE: deleting a PD_Score removes its SHAP rows automatically.
--      Note: ml.SHAP_Values has no ClientID column — join via ApplicationID + ScoreDate.
CREATE TABLE IF NOT EXISTS ml.SHAP_Values (
    SHAPRecordID        BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ApplicationID       BIGINT          NOT NULL,
    ScoreDate           DATE            NOT NULL,
    FeatureName         VARCHAR(50)     NOT NULL,
    FeatureValue        DECIMAL(18,6)   NULL,
    SHAPValue           DECIMAL(12,6)   NOT NULL,
    AbsSHAPValue        DECIMAL(12,6)   NOT NULL,
    SHAPRank            SMALLINT        NOT NULL,
    Direction           CHAR(1)         NOT NULL,
    ETL_RunID           BIGINT          NOT NULL,
    CreatedAt           TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT CK_SHAPRank CHECK (SHAPRank BETWEEN 1 AND 5),
    CONSTRAINT CK_SHAPDir  CHECK (Direction IN ('+','-')),
    CONSTRAINT UQ_SHAP     UNIQUE (ApplicationID, ScoreDate, FeatureName, SHAPRank),
    CONSTRAINT FK_SHAP_PD  FOREIGN KEY (ApplicationID, ScoreDate)
                           REFERENCES ml.PD_Scores(ApplicationID, ScoreDate)
                           ON DELETE CASCADE,
    CONSTRAINT FK_SHAP_RunLog FOREIGN KEY (ETL_RunID) REFERENCES stg.ETL_RunLog(RunID)
);

-- 5.3  PSI Monitor — nightly drift detection
--      FIX-7: FeaturePSI_Top5 JSONB removed; per-feature PSI lives in ml.Feature_PSI.
--      Thresholds: PSI > 0.10 → MONITOR; PSI > 0.25 → RETRAIN.
CREATE TABLE IF NOT EXISTS ml.PSI_Monitor (
    MonitorID           BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    MonitorDate         DATE            NOT NULL,
    PSI_Score           DECIMAL(8,4)    NOT NULL,
    PSI_Status          VARCHAR(10)     NOT NULL,
    ModelVersion        VARCHAR(30)     NOT NULL,
    AlertSent           BOOLEAN         NOT NULL    DEFAULT FALSE,
    AlertSentAt         TIMESTAMP       NULL,
    ETL_RunID           BIGINT          NOT NULL,
    CreatedAt           TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT CK_PSI_Status CHECK (PSI_Status IN ('OK','MONITOR','RETRAIN')),
    CONSTRAINT UQ_PSI         UNIQUE (MonitorDate, ModelVersion),
    CONSTRAINT FK_PSI_RunLog  FOREIGN KEY (ETL_RunID)    REFERENCES stg.ETL_RunLog(RunID),
    CONSTRAINT FK_PSI_Model   FOREIGN KEY (ModelVersion) REFERENCES ml.Models(ModelVersion)
);

-- 5.4  Feature PSI — per-feature drift tracking
CREATE TABLE IF NOT EXISTS ml.Feature_PSI (
    FeaturePSIID        BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    FeatureName         VARCHAR(50)     NOT NULL,
    MonitorDate         DATE            NOT NULL,
    PSI_Score           DECIMAL(8,4)    NOT NULL,
    PSI_Status          VARCHAR(10)     NOT NULL,
    ModelVersion        VARCHAR(30)     NOT NULL,
    ETL_RunID           BIGINT          NOT NULL,
    CreatedAt           TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT CK_FeaturePSI_Status CHECK (PSI_Status IN ('OK','MONITOR','RETRAIN')),
    CONSTRAINT UQ_Feature_PSI       UNIQUE (FeatureName, MonitorDate, ModelVersion),
    CONSTRAINT FK_FeatPSI_Model     FOREIGN KEY (ModelVersion) REFERENCES ml.Models(ModelVersion),
    CONSTRAINT FK_FeatPSI_RunLog    FOREIGN KEY (ETL_RunID)    REFERENCES stg.ETL_RunLog(RunID)
);

-- 5.5  Model Registry — champion/challenger governance history
--      FIX-5: ChallengerBrier and Brier_Delta added.
--      Answers "What was the active model on a given date?"
CREATE TABLE IF NOT EXISTS ml.ModelRegistry (
    CycleID             INT             NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ETL_RunID           BIGINT          NOT NULL,
    CycleDate           DATE            NOT NULL,
    CycleType           VARCHAR(20)     NOT NULL,
    ChampionVersion     VARCHAR(30)     NOT NULL,
    ChampionAUC         DECIMAL(6,4)    NOT NULL,
    ChampionGini        DECIMAL(6,4)    NOT NULL,
    ChampionKS          DECIMAL(6,4)    NOT NULL,
    ChampionBrier       DECIMAL(6,4)    NOT NULL,
    ChallengerAUC       DECIMAL(6,4)    NOT NULL,
    ChallengerGini      DECIMAL(6,4)    NOT NULL,
    ChallengerKS        DECIMAL(6,4)    NOT NULL,
    ChallengerBrier     DECIMAL(6,4)    NULL,       -- FIX-5
    -- Stored deltas — avoids arithmetic in Power BI
    AUC_Delta           DECIMAL(6,4)    GENERATED ALWAYS AS (ChallengerAUC  - ChampionAUC)  STORED,
    Gini_Delta          DECIMAL(6,4)    GENERATED ALWAYS AS (ChallengerGini - ChampionGini) STORED,
    KS_Delta            DECIMAL(6,4)    GENERATED ALWAYS AS (ChallengerKS   - ChampionKS)   STORED,
    -- FIX-5: Brier_Delta stored; negative = challenger better calibrated.
    Brier_Delta         DECIMAL(6,4)    NULL,
    Promoted            BOOLEAN         NOT NULL    DEFAULT FALSE,
    NewChampionVersion  VARCHAR(30)     NULL,
    NewChampionRunID    VARCHAR(40)     NULL,
    TrainingWindowStart DATE            NULL,
    TrainingWindowEnd   DATE            NULL,
    OptunaBestAUC       DECIMAL(6,4)    NULL,
    OptunaTrials        SMALLINT        NULL,
    Notes               VARCHAR(500)    NULL,
    CreatedAt           TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT CK_CycleType         CHECK (CycleType IN ('QUARTERLY','PSI_TRIGGERED','MANUAL')),
    CONSTRAINT FK_ModelReg_RunLog   FOREIGN KEY (ETL_RunID)          REFERENCES stg.ETL_RunLog(RunID),
    CONSTRAINT FK_ModelReg_Champion FOREIGN KEY (ChampionVersion)    REFERENCES ml.Models(ModelVersion),
    CONSTRAINT FK_ModelReg_New      FOREIGN KEY (NewChampionVersion) REFERENCES ml.Models(ModelVersion)
);

-- 5.6  Encoder map — categorical label → integer mapping, versioned
CREATE TABLE IF NOT EXISTS ml.EncoderMap (
    EncoderID           INT             NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    FeatureName         VARCHAR(50)     NOT NULL,
    OriginalValue       VARCHAR(100)    NOT NULL,
    EncodedValue        SMALLINT        NOT NULL,
    ModelVersion        VARCHAR(30)     NOT NULL,
    CreatedAt           TIMESTAMP       NOT NULL    DEFAULT NOW(),
    CONSTRAINT UQ_EncoderMap_Label UNIQUE (FeatureName, OriginalValue, ModelVersion),
    CONSTRAINT UQ_EncoderMap_Value UNIQUE (FeatureName, EncodedValue,  ModelVersion),
    CONSTRAINT FK_Encoder_Model    FOREIGN KEY (ModelVersion) REFERENCES ml.Models(ModelVersion)
);

-- 5.7  NPL Scores — monthly output of the XGBoost NPL early-warning model
--      RiskTier values: 'NORMAL' / 'WATCH' / 'CONCERN' / 'CRITICAL'
--      Thresholds: <0.15 NORMAL, <0.30 WATCH, <0.50 CONCERN, ≥0.50 CRITICAL
CREATE TABLE IF NOT EXISTS ml.NPL_Scores (
    NPLScoreID          BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ApplicationID       BIGINT          NOT NULL,
    ClientID            BIGINT          NOT NULL,
    ScoreDate           DATE            NOT NULL    DEFAULT CURRENT_DATE,
    NPL_Score           DECIMAL(9,7)    NOT NULL,
    NPL_Risk_Tier       VARCHAR(10)     NOT NULL,
    PD_Score_Current    DECIMAL(9,7)    NULL,
    ModelVersion        VARCHAR(30)     NOT NULL,
    ETL_RunID           BIGINT          NOT NULL,
    CreatedAt           TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),
    CONSTRAINT UQ_NPL_AppDate UNIQUE (ApplicationID, ScoreDate, ModelVersion),
    CONSTRAINT CK_NPL_Tier    CHECK (NPL_Risk_Tier IN ('NORMAL','WATCH','CONCERN','CRITICAL')),
    CONSTRAINT FK_NPL_App     FOREIGN KEY (ApplicationID) REFERENCES public.LoanApplications(ApplicationID),
    CONSTRAINT FK_NPL_Model   FOREIGN KEY (ModelVersion)  REFERENCES ml.Models(ModelVersion),
    CONSTRAINT FK_NPL_RunLog  FOREIGN KEY (ETL_RunID)     REFERENCES stg.ETL_RunLog(RunID)
);

-- 5.8  NPL SHAP Values — top-5 features per NPL-scored application
--      Note: unlike ml.SHAP_Values, this table DOES have a ClientID column.
CREATE TABLE IF NOT EXISTS ml.NPL_SHAP_Values (
    NPLSHAPRecordID     BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ApplicationID       BIGINT          NOT NULL,
    ClientID            BIGINT          NOT NULL,
    ScoreDate           DATE            NOT NULL,
    FeatureName         VARCHAR(60)     NOT NULL,
    FeatureValue        DECIMAL(18,6)   NULL,
    SHAPValue           DECIMAL(12,6)   NOT NULL,
    AbsSHAPValue        DECIMAL(12,6)   NOT NULL,
    SHAPRank            SMALLINT        NOT NULL,
    Direction           CHAR(1)         NOT NULL,
    Horizon             SMALLINT        NOT NULL    DEFAULT 90,
    ETL_RunID           BIGINT          NOT NULL,
    CreatedAt           TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),
    CONSTRAINT CK_NPL_SHAP_Rank  CHECK (SHAPRank BETWEEN 1 AND 5),
    CONSTRAINT CK_NPL_SHAP_Dir   CHECK (Direction IN ('+','-')),
    CONSTRAINT FK_NPL_SHAP_RunLog FOREIGN KEY (ETL_RunID) REFERENCES stg.ETL_RunLog(RunID)
);

-- 5.9  NBO Scores — daily output of the NearestNeighbors recommender
--      Top-5 offers per eligible client per day.
--      SuppressReason: why the offer was blocked (PD gate, cooldown, etc.)
CREATE TABLE IF NOT EXISTS ml.NBO_Scores (
    NBO_ScoreID         BIGINT          NOT NULL    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ClientID            BIGINT          NOT NULL,
    SnapshotDate        DATE            NOT NULL,
    ProductID           INT             NOT NULL,
    Rank                SMALLINT        NOT NULL,
    PropensityScore     DECIMAL(8,4)    NOT NULL,
    EligibleFlag        BOOLEAN         NOT NULL    DEFAULT TRUE,
    SuppressReason      VARCHAR(50)     NULL,
    PD_Score_Used       DECIMAL(9,7)    NULL,
    PD_RiskTier_Used    VARCHAR(10)     NULL,
    Method              VARCHAR(20)     NOT NULL    DEFAULT 'CF',
    ModelVersion        VARCHAR(30)     NOT NULL,
    ETL_RunID           BIGINT          NOT NULL,
    CreatedAt           TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),
    CONSTRAINT UQ_NBO_Score    UNIQUE (ClientID, SnapshotDate, ProductID, ModelVersion),
    CONSTRAINT CK_NBO_Rank     CHECK (Rank BETWEEN 1 AND 5),
    CONSTRAINT CK_NBO_Method   CHECK (Method IN ('CF','COLD','RULE')),
    CONSTRAINT CK_NBO_Suppress CHECK (
        SuppressReason IS NULL OR SuppressReason IN
        ('PD_HIGH','ALREADY_OWNS','COOLDOWN','INELIGIBLE','NPL_CONCERN')
    ),
    CONSTRAINT FK_NBO_Client   FOREIGN KEY (ClientID)     REFERENCES public.Clients(ClientID),
    CONSTRAINT FK_NBO_Product  FOREIGN KEY (ProductID)    REFERENCES public.Products(ProductID),
    CONSTRAINT FK_NBO_Model    FOREIGN KEY (ModelVersion) REFERENCES ml.Models(ModelVersion),
    CONSTRAINT FK_NBO_RunLog   FOREIGN KEY (ETL_RunID)    REFERENCES stg.ETL_RunLog(RunID)
);


-- ============================================================
-- 6. REPORTING VIEWS  (rpt.*)
-- ============================================================

-- 6.1  Portfolio risk summary
CREATE OR REPLACE VIEW rpt.vw_PortfolioRiskSummary AS
SELECT
    s.ScoreDate,
    s.RiskTier,
    s.ScoreDecile,
    s.ModelVersion,
    COUNT(*)            AS LoanCount,
    AVG(s.PD_Score)     AS AvgPD_Score,
    MIN(s.PD_Score)     AS MinPD_Score,
    MAX(s.PD_Score)     AS MaxPD_Score,
    SUM(a.LoanAmount)   AS TotalExposure,
    AVG(a.LoanAmount)   AS AvgLoanAmount,
    AVG(a.Annuity)      AS AvgAnnuity
FROM       ml.PD_Scores            s
INNER JOIN public.LoanApplications a ON s.ApplicationID = a.ApplicationID
GROUP BY   s.ScoreDate, s.RiskTier, s.ScoreDecile, s.ModelVersion;

-- 6.2  Application detail — FIX-8: materialized view for Power BI performance
--      Refreshed nightly by rpt.usp_RefreshReportingCache.
CREATE MATERIALIZED VIEW IF NOT EXISTS rpt.vw_ApplicationDetail AS
SELECT
    s.ApplicationID,
    s.ClientID,
    s.ScoreDate,
    s.PD_Score,
    s.RiskTier,
    s.ScoreDecile,
    s.ModelVersion,
    s.MLflowRunID,
    a.LoanAmount,
    a.Annuity,
    a.LoanTenorMonths,
    a.ContractType,
    a.ApplicationDate,
    c.IncomeTotal,
    c.EducationType,
    c.FamilyStatus,
    c.RegionRating,
    MAX(CASE WHEN sh.SHAPRank = 1 THEN sh.FeatureName END) AS Reason1_Feature,
    MAX(CASE WHEN sh.SHAPRank = 1 THEN sh.SHAPValue   END) AS Reason1_SHAP,
    MAX(CASE WHEN sh.SHAPRank = 1 THEN sh.Direction   END) AS Reason1_Dir,
    MAX(CASE WHEN sh.SHAPRank = 2 THEN sh.FeatureName END) AS Reason2_Feature,
    MAX(CASE WHEN sh.SHAPRank = 2 THEN sh.SHAPValue   END) AS Reason2_SHAP,
    MAX(CASE WHEN sh.SHAPRank = 2 THEN sh.Direction   END) AS Reason2_Dir,
    MAX(CASE WHEN sh.SHAPRank = 3 THEN sh.FeatureName END) AS Reason3_Feature,
    MAX(CASE WHEN sh.SHAPRank = 3 THEN sh.SHAPValue   END) AS Reason3_SHAP,
    MAX(CASE WHEN sh.SHAPRank = 3 THEN sh.Direction   END) AS Reason3_Dir,
    MAX(CASE WHEN sh.SHAPRank = 4 THEN sh.FeatureName END) AS Reason4_Feature,
    MAX(CASE WHEN sh.SHAPRank = 4 THEN sh.SHAPValue   END) AS Reason4_SHAP,
    MAX(CASE WHEN sh.SHAPRank = 4 THEN sh.Direction   END) AS Reason4_Dir,
    MAX(CASE WHEN sh.SHAPRank = 5 THEN sh.FeatureName END) AS Reason5_Feature,
    MAX(CASE WHEN sh.SHAPRank = 5 THEN sh.SHAPValue   END) AS Reason5_SHAP,
    MAX(CASE WHEN sh.SHAPRank = 5 THEN sh.Direction   END) AS Reason5_Dir
FROM       ml.PD_Scores            s
INNER JOIN public.LoanApplications a  ON s.ApplicationID = a.ApplicationID
INNER JOIN public.Clients          c  ON s.ClientID      = c.ClientID
LEFT  JOIN ml.SHAP_Values         sh  ON s.ApplicationID = sh.ApplicationID
                                     AND s.ScoreDate     = sh.ScoreDate
GROUP BY
    s.ApplicationID, s.ClientID, s.ScoreDate, s.PD_Score, s.RiskTier,
    s.ScoreDecile, s.ModelVersion, s.MLflowRunID,
    a.LoanAmount, a.Annuity, a.LoanTenorMonths, a.ContractType, a.ApplicationDate,
    c.IncomeTotal, c.EducationType, c.FamilyStatus, c.RegionRating
WITH NO DATA;

-- 6.3  Model health — registry joined to latest PSI
CREATE OR REPLACE VIEW rpt.vw_ModelHealth AS
SELECT
    r.CycleDate,
    r.CycleType,
    r.ChampionVersion,
    r.ChampionAUC,
    r.ChampionGini,
    r.ChampionKS,
    r.ChampionBrier,
    r.ChallengerAUC,
    r.ChallengerGini,
    r.ChallengerKS,
    r.ChallengerBrier,
    r.AUC_Delta,
    r.Gini_Delta,
    r.KS_Delta,
    r.Brier_Delta,
    r.Promoted,
    r.NewChampionVersion,
    r.OptunaBestAUC,
    r.OptunaTrials,
    p.PSI_Score,
    p.PSI_Status,
    r.TrainingWindowStart,
    r.TrainingWindowEnd,
    r.Notes
FROM       ml.ModelRegistry r
LEFT JOIN  ml.PSI_Monitor   p ON p.MonitorDate  = r.CycleDate
                              AND p.ModelVersion = r.ChampionVersion;

-- 6.4  PSI trend with static threshold lines for Power BI reference marks
CREATE OR REPLACE VIEW rpt.vw_PSITrend AS
SELECT
    MonitorDate,
    PSI_Score,
    PSI_Status,
    ModelVersion,
    AlertSent,
    AlertSentAt,
    0.10 AS PSI_MonitorThreshold,
    0.25 AS PSI_RetrainThreshold
FROM ml.PSI_Monitor;

-- 6.5  Score distribution by decile
CREATE OR REPLACE VIEW rpt.vw_ScoreDistribution AS
SELECT
    ScoreDate,
    ModelVersion,
    ScoreDecile,
    COUNT(*)        AS LoanCount,
    AVG(PD_Score)   AS AvgPD,
    MIN(PD_Score)   AS MinPD,
    MAX(PD_Score)   AS MaxPD
FROM ml.PD_Scores
GROUP BY ScoreDate, ModelVersion, ScoreDecile;

-- 6.6  Global SHAP feature importance
CREATE OR REPLACE VIEW rpt.vw_GlobalSHAPImportance AS
SELECT
    ScoreDate,
    FeatureName,
    COUNT(*)                                            AS AppCount,
    AVG(AbsSHAPValue)                                   AS MeanAbsSHAP,
    AVG(SHAPValue)                                      AS MeanSHAP,
    SUM(CASE WHEN Direction = '+' THEN 1 ELSE 0 END)   AS CountPositive,
    SUM(CASE WHEN Direction = '-' THEN 1 ELSE 0 END)   AS CountNegative,
    RANK() OVER (
        PARTITION BY ScoreDate
        ORDER BY AVG(AbsSHAPValue) DESC
    )                                                   AS ImportanceRank
FROM ml.SHAP_Values
GROUP BY ScoreDate, FeatureName;

-- 6.7  Delta change audit — rows changed per run per table
CREATE OR REPLACE VIEW rpt.vw_DeltaChangeAudit AS
SELECT r.RunDate, r.RunID, 'Applications' AS SourceTable, d.DeltaAction, COUNT(*) AS RowCount
FROM stg.ETL_RunLog r
INNER JOIN stg.Applications_Delta d ON d.ETL_RunID = r.RunID
GROUP BY r.RunDate, r.RunID, d.DeltaAction
UNION ALL
SELECT r.RunDate, r.RunID, 'Clients', d.DeltaAction, COUNT(*)
FROM stg.ETL_RunLog r
INNER JOIN stg.Clients_Delta d ON d.ETL_RunID = r.RunID
GROUP BY r.RunDate, r.RunID, d.DeltaAction
ORDER BY RunDate DESC, RunID DESC;

-- 6.8  Client 360 — single row per client combining PD, NPL, and NBO
--      Used by Power BI Page 4 and the planned Streamlit lookup page.
--      NBO eligibility gate: HIGH PD or CONCERN/CRITICAL NPL → suppress offers.
CREATE OR REPLACE VIEW rpt.vw_Client360 AS
SELECT
    -- ── Client identity ───────────────────────────────────────────────
    c.clientid,
    c.gender,
    c.educationtype,
    c.familystatus,
    c.incometype,
    c.incometotal,
    c.owncar,
    c.ownrealty,
    c.cntchildren,
    c.regionrating,
    DATE_PART('year', AGE(c.birthdate))::INT            AS age_years,
    ROUND(ABS(COALESCE(c.days_employed, 0)) / 365.25, 1) AS employed_years,
    -- ── Latest loan application ──────────────────────────────────────
    la.applicationid,
    la.loanamount,
    la.annuity,
    la.loantenormonths,
    la.contracttype,
    la.applicationdate,
    la.target                                           AS historical_default,
    -- ── PD score (latest) ────────────────────────────────────────────
    pd.scoredate                                        AS pd_score_date,
    pd.pd_score,
    pd.risktier                                         AS pd_risk_tier,
    pd.scoredecile                                      AS pd_score_decile,
    pd.modelversion                                     AS pd_model_version,
    -- ── NPL score (latest) ───────────────────────────────────────────
    npl.scoredate                                       AS npl_score_date,
    npl.npl_score,
    npl.npl_risk_tier,
    npl.pd_score_current                                AS npl_pd_score_used,
    npl.modelversion                                    AS npl_model_version,
    -- ── NBO top offer (rank=1, eligible only) ────────────────────────
    nbo.snapshotdate                                    AS nbo_score_date,
    pr.productcode                                      AS top_offer_code,
    pr.productname                                      AS top_offer_name,
    pr.category                                         AS top_offer_category,
    nbo.propensityscore                                 AS top_offer_propensity,
    nbo.method                                          AS nbo_method,
    -- ── Joint risk classification ─────────────────────────────────────
    -- Combines PD and NPL tiers into a single portfolio segment label.
    CASE
        WHEN pd.risktier = 'HIGH'
            OR npl.npl_risk_tier = 'CRITICAL'           THEN 'CRITICAL_RISK'
        WHEN pd.risktier = 'HIGH'
            OR npl.npl_risk_tier = 'CONCERN'            THEN 'HIGH_RISK'
        WHEN pd.risktier = 'MEDIUM'
            OR npl.npl_risk_tier = 'WATCH'              THEN 'MEDIUM_RISK'
        WHEN pd.risktier = 'LOW'
            AND COALESCE(npl.npl_risk_tier, 'NORMAL') = 'NORMAL'
                                                        THEN 'LOW_RISK'
        ELSE 'UNSCORED'
    END                                                 AS joint_risk_class,
    -- ── NBO eligibility gate ──────────────────────────────────────────
    CASE
        WHEN pd.risktier = 'HIGH'                       THEN FALSE
        WHEN npl.npl_risk_tier IN ('CONCERN','CRITICAL') THEN FALSE
        ELSE TRUE
    END                                                 AS nbo_eligible,
    -- ── Product ownership summary ─────────────────────────────────────
    own.n_products,
    own.has_personal_loan,
    own.has_savings,
    own.has_credit_card,
    own.has_mortgage,
    own.has_investment,
    own.cold_start                                      AS nbo_cold_start

FROM public.Clients c

LEFT JOIN LATERAL (
    SELECT * FROM public.LoanApplications
    WHERE clientid = c.clientid
    ORDER BY applicationdate DESC LIMIT 1
) la ON TRUE

LEFT JOIN LATERAL (
    SELECT pd_score, risktier, scoredecile, scoredate, modelversion
    FROM ml.PD_Scores
    WHERE clientid = c.clientid
    ORDER BY scoredate DESC LIMIT 1
) pd ON TRUE

LEFT JOIN LATERAL (
    SELECT npl_score, npl_risk_tier, scoredate, pd_score_current, modelversion
    FROM ml.NPL_Scores
    WHERE clientid = c.clientid
    ORDER BY scoredate DESC LIMIT 1
) npl ON TRUE

LEFT JOIN LATERAL (
    SELECT nb.productid, nb.propensityscore, nb.snapshotdate, nb.method
    FROM ml.NBO_Scores nb
    WHERE nb.clientid     = c.clientid
      AND nb.rank         = 1
      AND nb.eligibleflag = TRUE
    ORDER BY nb.snapshotdate DESC LIMIT 1
) nbo ON TRUE

LEFT JOIN public.Products pr ON pr.productid = nbo.productid

LEFT JOIN LATERAL (
    SELECT n_products, has_personal_loan, has_savings,
           has_credit_card, has_mortgage, has_investment, cold_start
    FROM public.NBO_ClientOwnership
    WHERE clientid = c.clientid
    ORDER BY snapshotmonth DESC LIMIT 1
) own ON TRUE;

-- 6.9  NBO cooldown — clients+products currently within cooldown window
CREATE OR REPLACE VIEW rpt.vw_NBO_Cooldown AS
SELECT DISTINCT
    ns.ClientID,
    ns.ProductID,
    p.CooldownDays,
    MAX(ns.SnapshotDate)                    AS LastOfferedDate,
    CURRENT_DATE - MAX(ns.SnapshotDate)     AS DaysSinceOffered
FROM ml.NBO_Scores ns
JOIN public.Products p ON p.ProductID = ns.ProductID
WHERE ns.EligibleFlag = TRUE
  AND ns.SnapshotDate >= CURRENT_DATE - INTERVAL '60 days'
GROUP BY ns.ClientID, ns.ProductID, p.CooldownDays
HAVING CURRENT_DATE - MAX(ns.SnapshotDate) < p.CooldownDays;

-- 6.10  NBO recommendations — Power BI / Streamlit daily feed
CREATE OR REPLACE VIEW rpt.vw_NBORecommendations AS
SELECT
    ns.SnapshotDate,
    ns.ClientID,
    ns.Rank,
    p.ProductCode,
    p.ProductName,
    p.Category,
    ns.PropensityScore,
    ns.Method,
    ns.EligibleFlag,
    ns.SuppressReason,
    ns.PD_Score_Used,
    ns.PD_RiskTier_Used,
    c.IncomeTotal,
    c.IncomeType,
    c.FamilyStatus,
    c.RegionRating,
    na.AcceptedFlag,
    na.AcceptedDate
FROM ml.NBO_Scores ns
JOIN public.Products   p  ON p.ProductID = ns.ProductID
JOIN public.Clients    c  ON c.ClientID  = ns.ClientID
LEFT JOIN public.NBO_Accepted na
    ON na.ClientID  = ns.ClientID
   AND na.ProductID = ns.ProductID
   AND na.OfferDate = ns.SnapshotDate
WHERE ns.EligibleFlag = TRUE;

-- 6.11  NBO conversion rate by product + day
CREATE OR REPLACE VIEW rpt.vw_NBOConversionRate AS
SELECT
    ns.SnapshotDate,
    p.ProductCode,
    p.ProductName,
    p.Category,
    ns.Method,
    COUNT(*)                                                        AS offers_generated,
    SUM(CASE WHEN na.AcceptedFlag THEN 1 ELSE 0 END)               AS offers_accepted,
    ROUND(SUM(CASE WHEN na.AcceptedFlag THEN 1 ELSE 0 END)::DECIMAL
          / NULLIF(COUNT(*), 0), 4)                                 AS conversion_rate,
    ROUND(AVG(ns.PropensityScore), 4)                               AS avg_propensity
FROM ml.NBO_Scores ns
JOIN public.Products p ON p.ProductID = ns.ProductID
LEFT JOIN public.NBO_Accepted na
    ON na.ClientID  = ns.ClientID
   AND na.ProductID = ns.ProductID
   AND na.OfferDate = ns.SnapshotDate
WHERE ns.EligibleFlag = TRUE
GROUP BY ns.SnapshotDate, p.ProductCode, p.ProductName, p.Category, ns.Method;


-- ============================================================
-- 7. INDEXES
-- ============================================================

-- Source tables
CREATE INDEX IF NOT EXISTS IX_LoanApp_ClientID
    ON public.LoanApplications (ClientID);
CREATE INDEX IF NOT EXISTS IX_BureauSnap_ClientDate
    ON public.BureauSnapshots (ClientID, SnapshotDate DESC);
CREATE INDEX IF NOT EXISTS IX_Bureau_UpdatedAt              -- FIX-4
    ON public.BureauSnapshots (UpdatedAt);
CREATE INDEX IF NOT EXISTS IX_BureauLoans_ClientID
    ON public.BureauLoans (ClientID);
CREATE INDEX IF NOT EXISTS IX_PrevApp_ClientID
    ON public.PreviousApplications (ClientID);
CREATE INDEX IF NOT EXISTS IX_InstPay_ClientPrevApp
    ON public.InstallmentsPayments (ClientID, PrevAppID);
CREATE INDEX IF NOT EXISTS IX_POS_ClientID
    ON public.POS_CashBalance (ClientID);
CREATE INDEX IF NOT EXISTS IX_POS_PrevApp
    ON public.POS_CashBalance (PrevAppID);

-- SCD Type 2: active client lookup
CREATE INDEX IF NOT EXISTS IX_ClientHist_Active
    ON public.Clients_History (ClientID) WHERE IsCurrent = TRUE;
CREATE INDEX IF NOT EXISTS IX_ClientHist_Range              -- FIX-6
    ON public.Clients_History (ClientID, ValidFrom, ValidTo);

-- Transactions — parent placeholder + partition indexes
CREATE INDEX IF NOT EXISTS IX_Txn_ClientDate
    ON ONLY public.Transactions (ClientID, TxnDate);

DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT c.relname AS child
        FROM   pg_inherits   i
        JOIN   pg_class      c ON c.oid = i.inhrelid
        JOIN   pg_class      p ON p.oid = i.inhparent
        JOIN   pg_namespace  n ON n.oid = p.relnamespace
        WHERE  n.nspname = 'public'
        AND    p.relname = 'transactions'
        AND    c.relname != 'transactions_default'
    LOOP
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON public.%I (ClientID, TxnDate)',
            'IX_' || r.child || '_ClientDate', r.child
        );
    END LOOP;
END $$;

-- NBO
CREATE INDEX IF NOT EXISTS IX_NBO_Own_Month
    ON public.NBO_ClientOwnership (SnapshotMonth, cold_start);
CREATE INDEX IF NOT EXISTS IX_NBO_Acc_ClientDate
    ON public.NBO_Accepted (ClientID, OfferDate DESC);
CREATE INDEX IF NOT EXISTS IX_NBO_Acc_Product
    ON public.NBO_Accepted (ProductID, AcceptedFlag);

-- Staging
CREATE INDEX IF NOT EXISTS IX_AppDelta_ClientRun
    ON stg.Applications_Delta (ClientID, ETL_RunID);
CREATE INDEX IF NOT EXISTS IX_AppDelta_RunAction
    ON stg.Applications_Delta (ETL_RunID, DeltaAction);
CREATE INDEX IF NOT EXISTS IX_ClientDelta_RunID
    ON stg.Clients_Delta (ETL_RunID);
CREATE INDEX IF NOT EXISTS IX_TxnDelta_ClientDate
    ON stg.Transactions_Delta (ClientID, TxnDate);
CREATE INDEX IF NOT EXISTS IX_BurDelta_ClientDate
    ON stg.Bureau_Delta (ClientID, SnapshotDate);
CREATE INDEX IF NOT EXISTS IX_ETLLog_RunDate
    ON stg.ETL_RunLog (RunDate, StepName);
CREATE INDEX IF NOT EXISTS IX_ValErr_RunID
    ON stg.ValidationErrors (RunID);
CREATE INDEX IF NOT EXISTS IX_ETLCtrl_SourceTable
    ON stg.ETL_Control (SourceTable);

-- Feature store
CREATE INDEX IF NOT EXISTS IX_FS_ClientDateModel
    ON ml.FeatureStore (ClientID, AsOfDate DESC, ModelVersion);  -- FIX-1 aligned
CREATE INDEX IF NOT EXISTS IX_FS_RunID
    ON ml.FeatureStore (ETL_RunID);

-- PD Scores
CREATE INDEX IF NOT EXISTS IX_Scores_ClientDate
    ON ml.PD_Scores (ClientID, ScoreDate);
CREATE INDEX IF NOT EXISTS IX_Scores_DateTier
    ON ml.PD_Scores (ScoreDate, RiskTier);
CREATE INDEX IF NOT EXISTS IX_Scores_ModelVerDate
    ON ml.PD_Scores (ModelVersion, ScoreDate);
CREATE INDEX IF NOT EXISTS IX_Scores_High
    ON ml.PD_Scores (ScoreDate, ApplicationID) WHERE RiskTier = 'HIGH';

-- SHAP (PD)
CREATE INDEX IF NOT EXISTS IX_SHAP_AppDate
    ON ml.SHAP_Values (ApplicationID, ScoreDate);
CREATE INDEX IF NOT EXISTS IX_SHAP_DateFeature
    ON ml.SHAP_Values (ScoreDate, FeatureName);
CREATE INDEX IF NOT EXISTS IX_SHAP_FeatureName
    ON ml.SHAP_Values (FeatureName, ScoreDate);

-- NPL
CREATE INDEX IF NOT EXISTS IX_NPL_ClientDate
    ON ml.NPL_Scores (ClientID, ScoreDate DESC);
CREATE INDEX IF NOT EXISTS IX_NPL_Tier
    ON ml.NPL_Scores (ScoreDate, NPL_Risk_Tier) WHERE NPL_Risk_Tier != 'NORMAL';
CREATE INDEX IF NOT EXISTS IX_NPL_SHAP_AppDate
    ON ml.NPL_SHAP_Values (ApplicationID, ScoreDate);

-- NBO
CREATE INDEX IF NOT EXISTS IX_NBO_ClientDate
    ON ml.NBO_Scores (ClientID, SnapshotDate DESC);
CREATE INDEX IF NOT EXISTS IX_NBO_DateRank
    ON ml.NBO_Scores (SnapshotDate, Rank) WHERE EligibleFlag = TRUE;

-- Monitoring
CREATE INDEX IF NOT EXISTS IX_PSI_MonitorDate
    ON ml.PSI_Monitor (MonitorDate);
CREATE INDEX IF NOT EXISTS IX_FeatPSI_Date
    ON ml.Feature_PSI (MonitorDate, FeatureName);

-- Materialized view indexes (FIX-8)
CREATE UNIQUE INDEX IF NOT EXISTS IX_MV_AppDetail_AppDate
    ON rpt.vw_ApplicationDetail (ApplicationID, ScoreDate);
CREATE INDEX IF NOT EXISTS IX_MV_AppDetail_ScoreDate
    ON rpt.vw_ApplicationDetail (ScoreDate, RiskTier);


-- ============================================================
-- 8. PG_CRON JOB SHELL
--    Requires pg_cron extension + shared_preload_libraries = 'pg_cron'
--    and a server restart. Uncomment after configuring.
-- ============================================================

-- SELECT cron.schedule(
--     'credit_risk_etl_nightly',
--     '0 2 * * *',
--     $$
--     SELECT stg.usp_StageSourceData(NULL);
--     SELECT stg.usp_ValidateStagedData(NULL);
--     SELECT ml.usp_BuildFeatureStoreRaw(NULL);
--     -- Step 4 (Python): called by OS cron / Airflow
--     --   python credit_risk_etl_pg.py
--     SELECT rpt.usp_RefreshReportingCache(NULL);
--     $$
-- );


-- ============================================================
-- SANITY CHECK — run after deployment
-- ============================================================
SELECT schemaname AS SchemaName, tablename AS TableName
FROM   pg_tables
WHERE  schemaname IN ('public','stg','ml')
ORDER  BY schemaname, tablename;

SELECT schemaname AS SchemaName, viewname AS ViewName
FROM   pg_views
WHERE  schemaname = 'rpt'
ORDER  BY viewname;

SELECT schemaname, matviewname AS MaterializedViewName
FROM   pg_matviews
WHERE  schemaname = 'rpt'
ORDER  BY matviewname;

-- Expected counts
SELECT 'Products seeded: ' || COUNT(*) FROM public.Products;
SELECT 'Models seeded:   ' || COUNT(*) FROM ml.Models;

-- ============================================================
-- END OF DDL
-- ============================================================

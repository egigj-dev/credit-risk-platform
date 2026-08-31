-- ============================================================
-- workflow_schema.sql
-- Documents and creates the full pipeline execution workflow.
-- Three sections:
--   A. Workflow metadata table
--   B. Seed workflow steps for all three pipelines
--   C. Execution order view (for ops monitoring)
-- ============================================================

-- ── A. Workflow steps table ───────────────────────────────────
CREATE TABLE IF NOT EXISTS stg.WorkflowSteps (
    StepID          SERIAL          PRIMARY KEY,
    PipelineName    VARCHAR(20)     NOT NULL,
    StepName        VARCHAR(50)     NOT NULL UNIQUE,
    StepOrder       SMALLINT        NOT NULL,
    ScriptName      VARCHAR(100)    NOT NULL,
    ScheduledTime   TIME            NOT NULL,
    FrequencyType   VARCHAR(10)     NOT NULL DEFAULT 'DAILY',
    DependsOn       VARCHAR(50)[]   NULL,
    ExpectedMaxSec  INT             NULL,
    Description     TEXT            NULL,
    IsActive        BOOLEAN         NOT NULL DEFAULT TRUE,
    CONSTRAINT CK_WF_Freq CHECK (FrequencyType IN ('DAILY','MONTHLY','QUARTERLY','MANUAL'))
);

-- ── B. Seed workflow steps ────────────────────────────────────
INSERT INTO stg.WorkflowSteps
    (PipelineName, StepName, StepOrder, ScriptName,
     ScheduledTime, FrequencyType, DependsOn, ExpectedMaxSec, Description)
VALUES

-- ── Step 1: Source staging (nightly) ─────────────────────────
('STAGING', 'source_staging', 1,
 'psql -f credit_risk_proc_pg.sql → stg.usp_StageSourceData()',
 '02:00', 'DAILY', NULL, 600,
 'Delta-loads new/changed rows from all source tables into stg.*_Delta tables. '
 'Watermark-controlled — only processes rows changed since last run.'),

-- ── Step 2: Data validation ───────────────────────────────────
('STAGING', 'data_validation', 2,
 'psql → stg.usp_ValidateStagedData()',
 '02:10', 'DAILY', ARRAY['source_staging'], 120,
 'Runs 6 data quality checks on staged data. Logs errors to '
 'stg.ValidationErrors. Halts pipeline on CRITICAL failures.'),

-- ── Step 3: Feature store rebuild ────────────────────────────
('PD', 'feature_store_rebuild', 3,
 'psql → ml.usp_BuildFeatureStoreRaw_v2()',
 '02:15', 'DAILY', ARRAY['data_validation'], 600,
 'Aggregates bureau, installment, POS, and transaction data '
 'into stg.FeatureStore_Raw. 307,511 rows × 138+ columns.'),

-- ── Step 4: PD scoring ───────────────────────────────────────
('PD', 'pd_scoring', 4,
 'python credit_risk_etl_pg.py --run-date {today}',
 '02:30', 'DAILY', ARRAY['feature_store_rebuild'], 1800,
 'Loads stg.FeatureStore_Raw → derives 138 features → scores with '
 'CatBoost v2-cat → writes ml.PD_Scores (307,511 rows) + '
 'ml.SHAP_Values (1.5M rows). Logs PSI to ml.PSI_Monitor.'),

-- ── Step 5: NPL scoring (monthly) ────────────────────────────
('NPL', 'npl_scoring', 5,
 'python npl_train.py --mode score',
 '04:00', 'MONTHLY', ARRAY['pd_scoring'], 1200,
 'Queries stg.v_NPLFeatures → scores with XGBoost npl-v1 → '
 'writes ml.NPL_Scores (307,511 rows) + ml.NPL_SHAP_Values. '
 'Tier thresholds: NORMAL<0.15, WATCH<0.30, CONCERN<0.50, CRITICAL>=0.50.'),

-- ── Step 6: Product ownership refresh (monthly) ──────────────
('NBO', 'ownership_refresh', 6,
 'psql -f product_ownership.sql',
 '04:15', 'MONTHLY', ARRAY['pd_scoring'], 180,
 'Derives binary product ownership flags for all 13 products '
 'from LoanApplications, Transactions, BureauLoans, Clients. '
 'Updates public.NBO_ClientOwnership (307,511 rows).'),

-- ── Step 7: NBO scoring (daily) ──────────────────────────────
('NBO', 'nbo_scoring', 7,
 'python nbo_train.py --mode daily',
 '04:30', 'DAILY', ARRAY['pd_scoring'], 900,
 'Loads HNSW-CF artifacts → builds 42-dim feature vectors → '
 'applies PD gate (excludes HIGH risk) → applies cooldown filter → '
 'writes ml.NBO_Scores (top-5 per eligible client, ~100K rows/day).'),

-- ── Step 8: Reporting cache refresh ──────────────────────────
('REPORTING', 'reporting_refresh', 8,
 'psql → rpt.usp_RefreshReportingCache()',
 '05:00', 'DAILY', ARRAY['pd_scoring', 'nbo_scoring'], 300,
 'Refreshes rpt.vw_applicationdetail MATERIALIZED VIEW. '
 'Makes all Power BI and Streamlit data consistent with latest scores.'),

-- ── Step 9: Index rebuild (monthly) ──────────────────────────
('NBO', 'hnsw_index_rebuild', 9,
 'python nbo_train.py --mode train',
 '01:00', 'MONTHLY', ARRAY['ownership_refresh'], 1800,
 'Retrains NBO HNSW-CF index on updated client base. '
 'Recomputes RFM quartiles, IUF weights, KMeans cold-start clusters. '
 'Saves new artifacts to nbo_artifacts/. Run before nbo_scoring.'),

-- ── Step 10: NPL model retrain (quarterly) ───────────────────
('NPL', 'npl_model_retrain', 10,
 'python npl_train.py --mode train',
 '01:00', 'QUARTERLY', NULL, 3600,
 'Full XGBoost retrain on latest TARGET labels + NPL features. '
 'Out-of-time validation. Saves new model to npl_artifacts/. '
 'Trigger: AUC drop > 0.02 on monthly PSI check OR quarterly schedule.')

ON CONFLICT (StepName) DO UPDATE SET
    Description   = EXCLUDED.Description,
    ExpectedMaxSec = EXCLUDED.ExpectedMaxSec;

-- ── C. Execution order view ───────────────────────────────────
CREATE OR REPLACE VIEW stg.vw_WorkflowStatus AS
SELECT
    ws.StepOrder,
    ws.PipelineName,
    ws.StepName,
    ws.ScheduledTime,
    ws.FrequencyType,
    ws.DependsOn,
    ws.ExpectedMaxSec,

    -- Latest run outcome from ETL_RunLog
    rl.RunDate          AS last_run_date,
    rl.Status           AS last_status,
    rl.RowsProcessed    AS last_rows,
    rl.DurationSec      AS last_duration_sec,
    rl.ErrorMessage     AS last_error,

    -- Health signal
    CASE
        WHEN rl.Status IS NULL                      THEN 'NEVER_RUN'
        WHEN rl.Status = 'success'
         AND rl.RunDate >= CURRENT_DATE - 1         THEN 'OK'
        WHEN rl.Status = 'success'
         AND ws.FrequencyType = 'MONTHLY'
         AND rl.RunDate >= DATE_TRUNC('month', CURRENT_DATE) THEN 'OK'
        WHEN rl.Status = 'success'                  THEN 'STALE'
        WHEN rl.Status = 'running'                  THEN 'RUNNING'
        ELSE                                             'FAILED'
    END                                             AS health,

    -- SLA breach flag
    CASE
        WHEN rl.DurationSec > ws.ExpectedMaxSec     THEN TRUE
        ELSE FALSE
    END                                             AS sla_breached

FROM stg.WorkflowSteps ws
LEFT JOIN LATERAL (
    SELECT RunDate, Status, RowsProcessed,
           DurationSec, ErrorMessage
    FROM stg.ETL_RunLog
    WHERE StepName = ws.StepName
    ORDER BY RunID DESC
    LIMIT 1
) rl ON TRUE
WHERE ws.IsActive = TRUE
ORDER BY ws.StepOrder;

-- ── Verify ───────────────────────────────────────────────────
SELECT StepOrder, PipelineName, StepName,
       ScheduledTime, FrequencyType, health
FROM stg.vw_WorkflowStatus;

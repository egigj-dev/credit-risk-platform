-- ============================================================
-- Credit Risk PD Model — Power BI Setup
-- Data Science Division | Bank | April 2026
-- Version: 2.0 — updated for credit_risk_db_v2, v2-cat model
-- ============================================================
-- PART A — Connection setup
-- PART B — DAX measures
-- PART C — Dashboard page specifications
-- PART D — Power Query M transforms
-- ============================================================


/* ============================================================
   PART A — CONNECTION SETUP
   ============================================================

   Step 1: Open Power BI Desktop
     Home → Get data → More → Database → PostgreSQL database

   Step 2: Connect (single Import connection — no mixed mode)
   ┌──────────────────────────────────────────────────────────┐
   │ Server:    localhost:5432                                 │
   │ Database:  credit_risk_db_v2                             │
   │ Mode:      Import                                        │
   │ Auth:      Database → postgres / [your password]         │
   └──────────────────────────────────────────────────────────┘

   NOTE: vw_ApplicationDetail is a MATERIALIZED VIEW — it is
   precomputed nightly and is best loaded as Import, not
   DirectQuery. DirectQuery on a 307k-row materialized view
   with 5 pivoted SHAP columns would be unnecessarily slow.

   Step 3: In the Navigator, select these views:
   ┌──────────────────────────────────────────────────────────┐
   │ rpt vw_portfolioriskSummary  → rename: fact_PDScores     │
   │ rpt vw_scoredistribution     → rename: fact_ScoreDist    │
   │ rpt vw_globalshapimportance  → rename: fact_GlobalSHAP   │
   │ rpt vw_applicationdetail     → rename: dim_AppDetail     │
   │ rpt vw_modelhealth           → rename: mon_ModelHealth   │
   │ rpt vw_psitrend              → rename: mon_PSITrend      │
   │ rpt vw_deltachangeaudit      → rename: mon_DeltaAudit    │
   └──────────────────────────────────────────────────────────┘

   NOTE: Power BI's PostgreSQL connector lower-cases all
   unquoted column aliases. The DDL uses aliases like
   "AS LoanCount" which PostgreSQL stores as "loancount".
   When you drag fields in Power BI they will appear as:
     loancount, avgpd_score, totalexposure, risktier,
     pd_score, scoredate, modelversion, etc.
   The DAX measures below use the exact lowercase names
   Power BI will show.

   Step 4: Rename tables in the Fields pane (right-click →
   Rename) to the names shown above for readability.

   Step 5: Create dim_Date as a calculated table.
   Modelling → New Table → paste DAX from PART B below.

   Step 6: Set relationships (Home → Manage relationships):
     fact_PDScores[clientid]      → dim_Date[Date]       (N:1)
     fact_ScoreDist[scoredate]    → dim_Date[Date]       (N:1)
     fact_GlobalSHAP[scoredate]   → dim_Date[Date]       (N:1)
     mon_PSITrend[monitordate]    → dim_Date[Date]       (N:1)
     mon_ModelHealth[cycledate]   → dim_Date[Date]       (N:1)
     fact_PDScores[applicationid] → dim_AppDetail[applicationid] (N:1)

   Step 7: Schedule refresh (Power BI Service only if needed)
     Daily at 06:00 — after nightly ETL completes (~04:30)
   ============================================================ */


/* ============================================================
   PART B — DAX MEASURES
   Home → Enter data → create empty table called _Measures
   Select _Measures → New Measure → paste each block below
   ============================================================ */


-- ── CALCULATED TABLE: dim_Date ────────────────────────────────
-- Modelling → New Table

dim_Date =
ADDCOLUMNS(
    CALENDAR( DATE(2020,1,1), DATE(2030,12,31) ),
    "Year",      YEAR([Date]),
    "Quarter",   "Q" & QUARTER([Date]),
    "Month",     MONTH([Date]),
    "MonthName", FORMAT([Date], "MMM YYYY"),
    "MonthSort", YEAR([Date]) * 100 + MONTH([Date]),
    "IsWeekend", IF(WEEKDAY([Date], 2) >= 6, TRUE, FALSE)
)


-- ════════════════════════════════════════════════════════════
-- MEASURE GROUP 1 — Portfolio overview
-- Source table: fact_PDScores (rpt.vw_PortfolioRiskSummary)
-- Column reference note: PostgreSQL returns lowercase column
-- names. Power BI will show: loancount, avgpd_score,
-- totalexposure, risktier, scoredate, scoredecile
-- ════════════════════════════════════════════════════════════

Total Applications =
SUM(fact_PDScores[loancount])


Avg PD Score =
DIVIDE(
    SUMX(fact_PDScores, fact_PDScores[avgpd_score] * fact_PDScores[loancount]),
    [Total Applications],
    0
)


Mean PD % =
FORMAT([Avg PD Score], "0.00%")


Total Exposure =
SUM(fact_PDScores[totalexposure])


High Risk Count =
CALCULATE(
    SUM(fact_PDScores[loancount]),
    fact_PDScores[risktier] = "HIGH"
)


Medium Risk Count =
CALCULATE(
    SUM(fact_PDScores[loancount]),
    fact_PDScores[risktier] = "MEDIUM"
)


Low Risk Count =
CALCULATE(
    SUM(fact_PDScores[loancount]),
    fact_PDScores[risktier] = "LOW"
)


High Risk % =
DIVIDE([High Risk Count], [Total Applications], 0)


High Risk % Label =
FORMAT([High Risk %], "0.0%")


-- Estimated provision: PD × Exposure × LGD
-- LGD = 42% — update to match internal credit policy
Estimated Provision =
VAR LGD = 0.42
RETURN
SUMX(
    fact_PDScores,
    fact_PDScores[avgpd_score]
        * fact_PDScores[totalexposure]
        * LGD
)


Expected Defaults =
SUMX(
    fact_PDScores,
    fact_PDScores[avgpd_score] * fact_PDScores[loancount]
)


-- ════════════════════════════════════════════════════════════
-- MEASURE GROUP 2 — Trend & period comparison
-- ════════════════════════════════════════════════════════════

Avg PD Score LM =
CALCULATE(
    [Avg PD Score],
    DATEADD(dim_Date[Date], -1, MONTH)
)


PD Score MoM Change =
[Avg PD Score] - [Avg PD Score LM]


PD MoM Arrow =
VAR chg = [PD Score MoM Change]
RETURN
    SWITCH(
        TRUE(),
        chg >  0.005, "▲ " & FORMAT(chg, "+0.000"),
        chg < -0.005, "▼ " & FORMAT(chg, "0.000"),
        "─ Stable"
    )


PD Score 30d Avg =
CALCULATE(
    [Avg PD Score],
    DATESINPERIOD(dim_Date[Date], LASTDATE(dim_Date[Date]), -30, DAY)
)


High Risk Count LM =
CALCULATE(
    [High Risk Count],
    DATEADD(dim_Date[Date], -1, MONTH)
)


High Risk Count Change =
[High Risk Count] - [High Risk Count LM]


-- ════════════════════════════════════════════════════════════
-- MEASURE GROUP 3 — PSI monitoring
-- Source table: mon_PSITrend (rpt.vw_PSITrend)
-- Columns: monitordate, psi_score, psi_status, modelversion,
--          alertsent, alertsentat,
--          psi_monitorthreshold (0.10), psi_retrainthreshold (0.25)
-- ════════════════════════════════════════════════════════════

Latest PSI Score =
CALCULATE(
    MAX(mon_PSITrend[psi_score]),
    LASTDATE(mon_PSITrend[monitordate])
)


Latest PSI Status =
CALCULATE(
    LASTNONBLANK(mon_PSITrend[psi_status], 1),
    LASTDATE(mon_PSITrend[monitordate])
)


PSI Status Label =
VAR s = [Latest PSI Status]
RETURN
    SWITCH(
        s,
        "OK",      "✓  Stable",
        "MONITOR", "⚠  Monitor",
        "RETRAIN", "✗  Retrain needed",
        "—"
    )


PSI Traffic Light =
VAR s = [Latest PSI Status]
RETURN
    SWITCH(
        s,
        "OK",      "#27500A",
        "MONITOR", "#633806",
        "RETRAIN", "#791F1F",
        "#888780"
    )


PSI Breach Count =
CALCULATE(
    COUNTROWS(mon_PSITrend),
    mon_PSITrend[psi_status] IN {"MONITOR", "RETRAIN"}
)


-- ════════════════════════════════════════════════════════════
-- MEASURE GROUP 4 — Model health (MLOps)
-- Source table: mon_ModelHealth (rpt.vw_ModelHealth)
-- Columns: cycledate, cycletype, championversion,
--          championauc, championgini, championks, championbrier,
--          challengerauc, challengergini, challengerks,
--          auc_delta, gini_delta, ks_delta,
--          promoted, newchampionversion,
--          psi_score, psi_status,
--          trainingwindowstart, trainingwindowend, notes
-- NOTE: mon_ModelHealth is empty until the first quarterly
--       governance cycle is run and ml.ModelRegistry is
--       populated. The cards below will show BLANK until then.
-- ════════════════════════════════════════════════════════════

Latest Champion AUC =
CALCULATE(
    MAX(mon_ModelHealth[championauc]),
    LASTDATE(mon_ModelHealth[cycledate])
)


Latest Gini =
CALCULATE(
    MAX(mon_ModelHealth[championgini]),
    LASTDATE(mon_ModelHealth[cycledate])
)


Latest KS =
CALCULATE(
    MAX(mon_ModelHealth[championks]),
    LASTDATE(mon_ModelHealth[cycledate])
)


AUC vs Basel Minimum =
-- Basel II IRB minimum: Gini >= 0.30 (AUC >= 0.65)
-- Your model: Gini = 0.5701
VAR gini      = [Latest Gini]
VAR BaselMin  = 0.30
RETURN
    IF(
        ISBLANK(gini),
        "No governance cycle logged yet",
        IF(
            gini >= BaselMin,
            "✓  " & FORMAT(gini, "0.000") & "  (min " & FORMAT(BaselMin, "0.00") & ")",
            "✗  BELOW MINIMUM: " & FORMAT(gini, "0.000")
        )
    )


Total Champion Promotions =
CALCULATE(
    COUNTROWS(mon_ModelHealth),
    mon_ModelHealth[promoted] = TRUE
)


Last Promotion Date =
CALCULATE(
    MAX(mon_ModelHealth[cycledate]),
    mon_ModelHealth[promoted] = TRUE
)


Days Since Last Retrain =
VAR last_date = [Last Promotion Date]
RETURN
    IF(
        ISBLANK(last_date),
        BLANK(),
        DATEDIFF(last_date, TODAY(), DAY)
    )


-- ════════════════════════════════════════════════════════════
-- MEASURE GROUP 5 — Application lookup
-- Source table: dim_AppDetail (rpt.vw_ApplicationDetail)
-- Columns: applicationid, clientid, scoredate, pd_score,
--          risktier, scoredecile, modelversion, mlflowrunid,
--          loanamount, annuity, loantenormonths,
--          contracttype, applicationdate,
--          incometotal, educationtype, familystatus, regionrating,
--          reason1_feature, reason1_shap, reason1_dir, (×5)
-- ════════════════════════════════════════════════════════════

Selected PD Score =
IF(
    HASONEVALUE(dim_AppDetail[applicationid]),
    MAX(dim_AppDetail[pd_score]),
    BLANK()
)


Selected Risk Tier =
IF(
    HASONEVALUE(dim_AppDetail[applicationid]),
    MAX(dim_AppDetail[risktier]),
    BLANK()
)


Selected Score Label =
VAR score = [Selected PD Score]
VAR tier  = [Selected Risk Tier]
RETURN
    IF(
        NOT ISBLANK(score),
        FORMAT(score, "0.00%") & "  —  " & tier,
        "Enter an Application ID above"
    )


Decision Recommendation =
VAR score = [Selected PD Score]
RETURN
    SWITCH(
        TRUE(),
        ISBLANK(score),  "—",
        score < 0.05,    "✓  Approve — Low Risk",
        score < 0.08,    "✓  Approve — Acceptable",
        score < 0.15,    "⚠  Review — Borderline",
        score < 0.25,    "⚠  Escalate — Elevated Risk",
        "✗  Decline — High Risk"
    )


Score Card Colour =
VAR score = [Selected PD Score]
RETURN
    SWITCH(
        TRUE(),
        ISBLANK(score), "#9CA3AF",
        score < 0.08,   "#27500A",
        score < 0.20,   "#633806",
        "#791F1F"
    )


-- ════════════════════════════════════════════════════════════
-- MEASURE GROUP 6 — SHAP feature importance
-- Source table: fact_GlobalSHAP (rpt.vw_GlobalSHAPImportance)
-- Columns: scoredate, featurename, appcount,
--          meanabsshap, meanshap, countpositive,
--          countnegative, importancerank
-- ════════════════════════════════════════════════════════════

Top Feature =
CALCULATE(
    FIRSTNONBLANK(fact_GlobalSHAP[featurename], 1),
    TOPN(1, fact_GlobalSHAP, fact_GlobalSHAP[meanabsshap], DESC)
)


Top Feature SHAP =
CALCULATE(
    MAX(fact_GlobalSHAP[meanabsshap]),
    TOPN(1, fact_GlobalSHAP, fact_GlobalSHAP[meanabsshap], DESC)
)


Feature Impact Direction =
IF(
    MAX(fact_GlobalSHAP[meanshap]) > 0,
    "↑ Increases default risk",
    "↓ Decreases default risk"
)


/* ============================================================
   PART C — DASHBOARD PAGE SPECIFICATIONS
   ============================================================


   ── PAGE 1: PORTFOLIO RISK OVERVIEW ──────────────────────────
   ┌──────────────────────────────────────────────────────────┐
   │ ROW 1 — four KPI cards (equal width)                     │
   │   Card 1: [Total Applications]   label "Applications"    │
   │   Card 2: [Mean PD %]            label "Mean PD"         │
   │   Card 3: [High Risk % Label]    label "High Risk"       │
   │           conditional bg: [PSI Traffic Light]            │
   │   Card 4: [PSI Status Label]     label "Model Drift"     │
   │           conditional bg: [PSI Traffic Light]            │
   │                                                          │
   │ ROW 2 — charts                                           │
   │   LEFT: Clustered column chart                           │
   │     X-axis: fact_ScoreDist[scoredecile]  (sort 1→10)     │
   │     Y-axis: fact_ScoreDist[loancount]                    │
   │     Legend: fact_ScoreDist[scoredecile]                  │
   │     Colour: deciles 8-10 = red, 5-7 = amber, 1-4 = green │
   │     Title: "Score decile distribution"                   │
   │                                                          │
   │   RIGHT: Donut chart                                     │
   │     Legend: fact_PDScores[risktier]                      │
   │     Values: [Total Exposure]                             │
   │     Colours: LOW=#27500A, MEDIUM=#633806, HIGH=#791F1F   │
   │     Title: "Exposure by risk tier"                       │
   │                                                          │
   │ ROW 3 — trend charts                                     │
   │   LEFT: Line chart                                       │
   │     X-axis: dim_Date[MonthName] sorted by [MonthSort]    │
   │     Y-axis: [Avg PD Score], [PD Score 30d Avg]           │
   │     Title: "Mean PD trend"                               │
   │                                                          │
   │   RIGHT: Line chart                                      │
   │     X-axis: mon_PSITrend[monitordate]                    │
   │     Y-axis: mon_PSITrend[psi_score]                      │
   │     Analytics pane → Constant line 0.10 (amber label)    │
   │     Analytics pane → Constant line 0.25 (red label)      │
   │     OR add mon_PSITrend[psi_monitorthreshold] and        │
   │           mon_PSITrend[psi_retrainthreshold] as lines     │
   │     Title: "Population Stability Index"                  │
   │                                                          │
   │ ROW 4 — Stacked bar (full width)                         │
   │     X-axis: fact_PDScores[scoredate]                     │
   │     Y-axis: fact_PDScores[totalexposure]                 │
   │     Legend: fact_PDScores[risktier]                      │
   │     Colours: LOW/MEDIUM/HIGH as above                    │
   │     Title: "Portfolio exposure by risk tier over time"   │
   └──────────────────────────────────────────────────────────┘


   ── PAGE 2: APPLICATION LOOKUP ───────────────────────────────
   ┌──────────────────────────────────────────────────────────┐
   │ SEARCH — Slicer (single value, dropdown style)           │
   │     Field: dim_AppDetail[applicationid]                  │
   │                                                          │
   │ SCORE CARD (top left, large)                             │
   │     Value: [Selected PD Score]  Format as %              │
   │     Sub-label: [Selected Risk Tier]                      │
   │     Conditional background: [Score Card Colour]          │
   │     Below: [Decision Recommendation] as a card           │
   │                                                          │
   │ SHAP WATERFALL (top right)                               │
   │     Source: dim_ShapUnpivot (Power Query M — see PART D) │
   │     Visual: Horizontal bar chart                         │
   │     Category: dim_ShapUnpivot[Feature]                   │
   │     Values: dim_ShapUnpivot[SHAP]                        │
   │     Colour: conditional — positive=red, negative=green   │
   │     Title: "Top 5 score drivers"                         │
   │     Sort: by absolute SHAP descending                    │
   │                                                          │
   │ CONTEXT CARDS (bottom row — 4 cards)                     │
   │     Loan Amount:  MAX(dim_AppDetail[loanamount])         │
   │     Annuity:      MAX(dim_AppDetail[annuity])            │
   │     Income:       MAX(dim_AppDetail[incometotal])        │
   │     Contract:     MAX(dim_AppDetail[contracttype])       │
   │                                                          │
   │ DEMOGRAPHICS CARDS (bottom row — 4 cards)                │
   │     Education:    MAX(dim_AppDetail[educationtype])      │
   │     Family:       MAX(dim_AppDetail[familystatus])       │
   │     Region:       MAX(dim_AppDetail[regionrating])       │
   │     Score Date:   MAX(dim_AppDetail[scoredate])          │
   └──────────────────────────────────────────────────────────┘


   ── PAGE 3: MODEL HEALTH ─────────────────────────────────────
   NOTE: This page will show BLANK until ml.ModelRegistry is
   populated via a governance cycle. The SHAP chart (bottom
   left) uses fact_GlobalSHAP which IS populated.
   ┌──────────────────────────────────────────────────────────┐
   │ ROW 1 — four metric cards                                │
   │     Card 1: [Latest Champion AUC]  label "AUC"          │
   │     Card 2: [Latest Gini]          label "Gini"         │
   │     Card 3: [Latest KS]            label "KS Stat"      │
   │     Card 4: [AUC vs Basel Minimum]                       │
   │                                                          │
   │ ROW 2 — model performance history                        │
   │   LEFT: Line chart                                       │
   │     X-axis: mon_ModelHealth[cycledate]                   │
   │     Lines: [championauc], [challengerauc]                │
   │     Analytics → Constant line at 0.70 (Basel min AUC)   │
   │     Title: "Champion vs challenger AUC"                  │
   │                                                          │
   │   RIGHT: Table                                           │
   │     Columns: cycledate, championversion,                 │
   │              championauc, challengerauc,                 │
   │              auc_delta, promoted, cycletype              │
   │     Conditional format promoted=TRUE: green row          │
   │     Title: "Champion/challenger log"                     │
   │                                                          │
   │ ROW 3 — feature importance + drift history               │
   │   LEFT: Horizontal bar chart                             │
   │     Category: fact_GlobalSHAP[featurename]               │
   │     Value: fact_GlobalSHAP[meanabsshap]                  │
   │     Filter: fact_GlobalSHAP[importancerank] <= 15        │
   │     Sort: meanabsshap descending                         │
   │     Title: "Global feature importance (SHAP)"            │
   │                                                          │
   │   RIGHT: Column + line combo                             │
   │     Columns: mon_PSITrend[psi_score]                     │
   │     Conditional colour: psi_status                       │
   │       OK=#27500A, MONITOR=#633806, RETRAIN=#791F1F       │
   │     Constant lines: 0.10 amber, 0.25 red                 │
   │     Title: "PSI history"                                 │
   └──────────────────────────────────────────────────────────┘


/* ============================================================
   PART D — POWER QUERY M TRANSFORMS
   ============================================================
   These go in Power Query Editor.
   Home → Transform Data → open Power Query Editor.


   ── TRANSFORM 1: Unpivot SHAP reasons for waterfall chart ────
   Purpose: converts the 5 pivoted Reason columns in
   dim_AppDetail into one row per reason per application.
   This is required for the Page 2 SHAP waterfall chart.

   In Power Query Editor:
     1. Right-click dim_AppDetail → Reference
        (creates a new query based on dim_AppDetail)
     2. Rename the new query to: dim_ShapUnpivot
     3. Home → Advanced Editor → paste:

let
    Source = dim_AppDetail,
    KeepCols = Table.SelectColumns(Source, {
        "applicationid", "scoredate", "pd_score", "risktier"
    }),
    WithReasons = Table.AddColumn(
        Table.SelectColumns(Source, {
            "applicationid",
            "reason1_feature", "reason1_shap", "reason1_dir",
            "reason2_feature", "reason2_shap", "reason2_dir",
            "reason3_feature", "reason3_shap", "reason3_dir",
            "reason4_feature", "reason4_shap", "reason4_dir",
            "reason5_feature", "reason5_shap", "reason5_dir"
        }),
        "Reasons", each {
            [Rank=1, Feature=[reason1_feature], SHAP=[reason1_shap], Dir=[reason1_dir]],
            [Rank=2, Feature=[reason2_feature], SHAP=[reason2_shap], Dir=[reason2_dir]],
            [Rank=3, Feature=[reason3_feature], SHAP=[reason3_shap], Dir=[reason3_dir]],
            [Rank=4, Feature=[reason4_feature], SHAP=[reason4_shap], Dir=[reason4_dir]],
            [Rank=5, Feature=[reason5_feature], SHAP=[reason5_shap], Dir=[reason5_dir]]
        }
    ),
    Expanded = Table.ExpandListColumn(WithReasons, "Reasons"),
    Final = Table.ExpandRecordColumn(
        Expanded, "Reasons", {"Rank","Feature","SHAP","Dir"}
    ),
    Joined = Table.Join(
        Final, "applicationid",
        KeepCols, "applicationid",
        JoinKind.Inner
    )
in
    Joined


   ── TRANSFORM 2: Add AbsSHAP column to dim_ShapUnpivot ───────
   In dim_ShapUnpivot query, after the above steps add:

    AddAbsSHAP = Table.AddColumn(Final, "AbsSHAP",
        each Number.Abs([SHAP]), type number)

   Use AbsSHAP to sort the waterfall bar chart.


   ── TRANSFORM 3: Score tier colour column ────────────────────
   Add a conditional column to dim_AppDetail for the score
   gauge background:
   Add Column → Conditional Column:
     Name: TierColour
     IF risktier = "HIGH"   THEN #791F1F
     IF risktier = "MEDIUM" THEN #633806
     ELSE                        #27500A


   ── TRANSFORM 4: Reduce fact_GlobalSHAP to top 15 ───────────
   In fact_GlobalSHAP query, add a filter step:
     Filter → importancerank is less than or equal to 15
   This keeps the SHAP bar chart clean.
   ============================================================ */

# ============================================================
# api.py
# FastAPI demo — Credit Risk CRM integration endpoints
# Exposes PD, NPL, NBO outputs from the DB for CRM consumption.
#
# Install:
#   pip install fastapi uvicorn psycopg2-binary
#
# Run:
#   uvicorn api:app --host 0.0.0.0 --port 8000 --reload
#
# Docs (auto-generated):
#   http://localhost:8000/docs      ← Swagger UI
#   http://localhost:8000/redoc     ← ReDoc
# ============================================================

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ── DB connection ─────────────────────────────────────────────

PG = dict(
    host     = os.getenv("PG_HOST",   "localhost"),
    port     = int(os.getenv("PG_PORT", "5432")),
    dbname   = os.getenv("PG_DB",     "credit_risk_db_v2"),
    user     = os.getenv("PG_USER",   "postgres"),
    password = os.getenv("PG_PASS",   ""),
)


def get_conn():
    return psycopg2.connect(**PG, cursor_factory=psycopg2.extras.RealDictCursor)


def query_one(sql: str, params: tuple = ()) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


def query_many(sql: str, params: tuple = ()) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def execute(sql: str, params: tuple = ()) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


# ── Lifespan ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        query_one("SELECT 1")
    except Exception as e:
        raise RuntimeError(f"DB connection failed: {e}")
    yield


# ── App ───────────────────────────────────────────────────────

app = FastAPI(
    title       = "Credit Risk CRM API",
    description = (
        "Exposes PD scoring, NPL early warning, and NBO recommendations "
        "from the credit_risk_db_v2 pipeline for CRM integration.\n\n"
        "**Models in production:**\n"
        "- PD: CatBoost v2-cat (131 features)\n"
        "- NPL: XGBoost npl-v1 (AUC=0.771, 43 features)\n"
        "- NBO: sklearn-CF nbo-v1 (Hit@5=0.982, 13 products)"
    ),
    version  = "1.0.0",
    lifespan = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = [origin.strip() for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:8000"
    ).split(",") if origin.strip()],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)


# ============================================================
# RESPONSE MODELS
# ============================================================

class PDScore(BaseModel):
    application_id : int
    client_id      : int
    score_date     : date
    pd_score       : float = Field(..., ge=0, le=1, description="Probability of default [0,1]")
    risk_tier      : str   = Field(..., description="LOW / MEDIUM / HIGH / CRITICAL")
    score_decile   : int   = Field(..., ge=1, le=10)
    model_version  : str


class SHAPReason(BaseModel):
    rank          : int
    feature_name  : str
    feature_value : Optional[float] = None
    shap_value    : float
    direction     : Optional[str]   = None   # "+" increases risk, "-" decreases


class ApplicationDetail(BaseModel):
    application_id    : int
    client_id         : int
    score_date        : date
    pd_score          : float
    risk_tier         : str
    score_decile      : int
    loan_amount       : Optional[float] = None
    annuity           : Optional[float] = None
    loan_tenor_months : Optional[int]   = None
    contract_type     : Optional[str]   = None
    income_total      : Optional[float] = None
    education_type    : Optional[str]   = None
    family_status     : Optional[str]   = None
    region_rating     : Optional[int]   = None
    top_reasons       : list[SHAPReason]
    decision          : str
    model_version     : str


class NPLScore(BaseModel):
    application_id   : int
    client_id        : int
    score_date       : date
    npl_score        : float = Field(..., ge=0, le=1)
    npl_risk_tier    : str   = Field(..., description="NORMAL / WATCH / CONCERN / CRITICAL")
    pd_score_current : Optional[float] = None
    model_version    : str


class NBOOffer(BaseModel):
    rank             : int
    product_code     : str
    product_name     : str
    category         : str
    propensity_score : float
    method           : str   # CF / COLD / RULE
    eligible         : bool
    suppress_reason  : Optional[str] = None


class ClientProfile(BaseModel):
    client_id            : int
    age_years            : Optional[int]   = None
    gender               : Optional[str]   = None
    income_total         : Optional[float] = None
    income_type          : Optional[str]   = None
    family_status        : Optional[str]   = None
    own_car              : Optional[bool]  = None
    own_realty           : Optional[bool]  = None
    pd_score             : Optional[float] = None
    pd_risk_tier         : Optional[str]   = None
    pd_score_date        : Optional[date]  = None
    npl_score            : Optional[float] = None
    npl_risk_tier        : Optional[str]   = None
    npl_score_date       : Optional[date]  = None
    top_offer_name       : Optional[str]   = None
    top_offer_category   : Optional[str]   = None
    top_offer_propensity : Optional[float] = None
    nbo_score_date       : Optional[date]  = None
    joint_risk_class     : Optional[str]   = None
    nbo_eligible         : Optional[bool]  = None
    n_products           : Optional[int]   = None


class NBOAcceptPayload(BaseModel):
    client_id          : int
    product_code       : str
    offer_date         : date
    accepted           : bool
    channel_code       : Optional[str] = None
    new_application_id : Optional[int] = None


class PortfolioSummary(BaseModel):
    score_date         : date
    total_applications : int
    mean_pd_score      : float
    low_count          : int
    medium_count       : int
    high_count         : int
    total_exposure     : float
    model_version      : str


class PipelineStatus(BaseModel):
    step_name      : str
    status         : str
    run_date       : Optional[date]     = None
    rows_processed : Optional[int]      = None
    duration_sec   : Optional[int]      = None
    started_at     : Optional[datetime] = None
    finished_at    : Optional[datetime] = None
    error          : Optional[str]      = None


# ============================================================
# HELPER
# ============================================================

def _decision(pd_score: float) -> str:
    if pd_score < 0.05: return "Approve — Low Risk"
    if pd_score < 0.08: return "Approve — Acceptable"
    if pd_score < 0.15: return "Review — Borderline"
    if pd_score < 0.25: return "Escalate — Elevated Risk"
    return "Decline — High Risk"


# ============================================================
# ENDPOINTS — PD SCORING
# ============================================================

@app.get(
    "/api/v1/scoring/application/{application_id}",
    response_model = ApplicationDetail,
    tags           = ["PD Scoring"],
    summary        = "Get PD score and SHAP reasons for one application",
)
def get_application_score(application_id: int):
    """
    Returns the latest PD score, risk tier, loan details, and top SHAP
    reasons (4 risk-increasing + 4 protective) for a single application.
    Used by loan officers in the CRM application detail view.
    """
    row = query_one("""
        SELECT
            applicationid, clientid, scoredate, pd_score, risktier,
            scoredecile, loanamount, annuity, loantenormonths,
            contracttype, incometotal, educationtype,
            familystatus, regionrating, modelversion
        FROM rpt.vw_applicationdetail
        WHERE applicationid = %s
    """, (application_id,))

    if not row:
        raise HTTPException(
            404,
            f"Application {application_id} not found or not yet scored."
        )

    # Top 4 risk-increasing + top 4 protective SHAP values
    reasons = query_many("""
        (
            SELECT shaprank AS rank, featurename AS feature_name,
                featurevalue AS feature_value, shapvalue AS shap_value,
                direction
            FROM ml.SHAP_Values
            WHERE applicationid = %s AND scoredate = %s AND shapvalue > 0
            ORDER BY shapvalue DESC
            LIMIT 4
        )
        UNION ALL
        (
            SELECT shaprank AS rank, featurename AS feature_name,
                featurevalue AS feature_value, shapvalue AS shap_value,
                direction
            FROM ml.SHAP_Values
            WHERE applicationid = %s AND scoredate = %s AND shapvalue < 0
            ORDER BY shapvalue ASC
            LIMIT 4
        )
        ORDER BY shap_value DESC
    """, (application_id, row["scoredate"], application_id, row["scoredate"]))

    return ApplicationDetail(
        application_id    = row["applicationid"],
        client_id         = row["clientid"],
        score_date        = row["scoredate"],
        pd_score          = float(row["pd_score"]),
        risk_tier         = row["risktier"],
        score_decile      = int(row["scoredecile"]),
        loan_amount       = float(row["loanamount"])   if row["loanamount"]   else None,
        annuity           = float(row["annuity"])       if row["annuity"]       else None,
        loan_tenor_months = row["loantenormonths"],
        contract_type     = row["contracttype"],
        income_total      = float(row["incometotal"])  if row["incometotal"]  else None,
        education_type    = row["educationtype"],
        family_status     = row["familystatus"],
        region_rating     = row["regionrating"],
        top_reasons       = [SHAPReason(**r) for r in reasons],
        decision          = _decision(float(row["pd_score"])),
        model_version     = row["modelversion"],
    )


@app.get(
    "/api/v1/scoring/client/{client_id}",
    response_model = PDScore,
    tags           = ["PD Scoring"],
    summary        = "Get latest PD score for a client",
)
def get_client_pd_score(client_id: int):
    """Returns the most recent PD score for a client across all applications."""
    row = query_one("""
        SELECT applicationid, clientid, scoredate, pd_score,
               risktier, scoredecile, modelversion
        FROM ml.PD_Scores
        WHERE clientid = %s
        ORDER BY scoredate DESC
        LIMIT 1
    """, (client_id,))

    if not row:
        raise HTTPException(404, f"No PD score found for client {client_id}.")

    return PDScore(
        application_id = row["applicationid"],
        client_id      = row["clientid"],
        score_date     = row["scoredate"],
        pd_score       = float(row["pd_score"]),
        risk_tier      = row["risktier"],
        score_decile   = int(row["scoredecile"]),
        model_version  = row["modelversion"],
    )


@app.get(
    "/api/v1/scoring/portfolio/summary",
    response_model = PortfolioSummary,
    tags           = ["PD Scoring"],
    summary        = "Portfolio risk summary for latest score date",
)
def get_portfolio_summary():
    """Aggregated portfolio metrics — total applications, tier counts, exposure."""
    row = query_one("""
        SELECT
            scoredate                              AS score_date,
            SUM(loancount)                         AS total_applications,
            ROUND(SUM(avgpd_score * loancount)
                  / NULLIF(SUM(loancount), 0), 7)  AS mean_pd_score,
            SUM(CASE WHEN risktier='LOW'    THEN loancount ELSE 0 END) AS low_count,
            SUM(CASE WHEN risktier='MEDIUM' THEN loancount ELSE 0 END) AS medium_count,
            SUM(CASE WHEN risktier='HIGH'   THEN loancount ELSE 0 END) AS high_count,
            SUM(totalexposure)                     AS total_exposure,
            MAX(modelversion)                      AS model_version
        FROM rpt.vw_portfoliorisksummary
        WHERE scoredate = (SELECT MAX(scoredate) FROM rpt.vw_portfoliorisksummary)
        GROUP BY scoredate
    """)

    if not row:
        raise HTTPException(404, "No portfolio scores found.")

    return PortfolioSummary(
        score_date         = row["score_date"],
        total_applications = int(row["total_applications"]),
        mean_pd_score      = float(row["mean_pd_score"]),
        low_count          = int(row["low_count"]),
        medium_count       = int(row["medium_count"]),
        high_count         = int(row["high_count"]),
        total_exposure     = float(row["total_exposure"]),
        model_version      = row["model_version"],
    )


# ============================================================
# ENDPOINTS — NPL EARLY WARNING
# ============================================================

@app.get(
    "/api/v1/npl/client/{client_id}",
    response_model = NPLScore,
    tags           = ["NPL Early Warning"],
    summary        = "Get latest NPL score for a client",
)
def get_client_npl_score(client_id: int):
    """Returns the most recent NPL risk score and tier for a client."""
    row = query_one("""
        SELECT applicationid, clientid, scoredate,
               npl_score, npl_risk_tier, pd_score_current, modelversion
        FROM ml.NPL_Scores
        WHERE clientid = %s
        ORDER BY scoredate DESC
        LIMIT 1
    """, (client_id,))

    if not row:
        raise HTTPException(404, f"No NPL score found for client {client_id}.")

    return NPLScore(
        application_id   = row["applicationid"],
        client_id        = row["clientid"],
        score_date       = row["scoredate"],
        npl_score        = float(row["npl_score"]),
        npl_risk_tier    = row["npl_risk_tier"],
        pd_score_current = float(row["pd_score_current"]) if row["pd_score_current"] else None,
        model_version    = row["modelversion"],
    )


@app.get(
    "/api/v1/npl/tier-summary",
    tags    = ["NPL Early Warning"],
    summary = "Count of clients per NPL risk tier for latest score date",
)
def get_npl_tier_summary():
    """Returns {tier: count} for all four NPL tiers. Used by the dashboard KPI cards."""
    rows = query_many("""
        SELECT npl_risk_tier, COUNT(*) AS count
        FROM ml.NPL_Scores
        WHERE scoredate = (SELECT MAX(scoredate) FROM ml.NPL_Scores)
        GROUP BY npl_risk_tier
    """)
    return {r["npl_risk_tier"]: int(r["count"]) for r in rows}


@app.get(
    "/api/v1/npl/watchlist",
    response_model = list[NPLScore],
    tags           = ["NPL Early Warning"],
    summary        = "Clients with elevated NPL risk",
)
def get_npl_watchlist(
    tier  : str  = Query("WATCH", description="WATCH / CONCERN / CRITICAL"),
    exact : bool = Query(False,   description="True = only this tier; False = this tier and above"),
    limit : int  = Query(100, ge=1, le=1000),
    offset: int  = Query(0, ge=0),
):
    """
    Returns clients at or above the given NPL risk tier, sorted by score descending.
    Pass exact=true to filter to a single tier (e.g. for per-tier watchlist views).
    Used by the collections team to prioritise outreach.
    """
    valid_tiers = {"WATCH", "CONCERN", "CRITICAL"}
    if tier not in valid_tiers:
        raise HTTPException(400, f"tier must be one of {valid_tiers}")

    if exact:
        tier_filter = [tier]
    else:
        tier_filter = {
            "WATCH":    ["WATCH", "CONCERN", "CRITICAL"],
            "CONCERN":  ["CONCERN", "CRITICAL"],
            "CRITICAL": ["CRITICAL"],
        }[tier]

    rows = query_many("""
        SELECT applicationid, clientid, scoredate,
               npl_score, npl_risk_tier, pd_score_current, modelversion
        FROM ml.NPL_Scores
        WHERE scoredate      = (SELECT MAX(scoredate) FROM ml.NPL_Scores)
          AND npl_risk_tier  = ANY(%s)
        ORDER BY npl_score DESC
        LIMIT %s OFFSET %s
    """, (tier_filter, limit, offset))

    return [NPLScore(
        application_id   = r["applicationid"],
        client_id        = r["clientid"],
        score_date       = r["scoredate"],
        npl_score        = float(r["npl_score"]),
        npl_risk_tier    = r["npl_risk_tier"],
        pd_score_current = float(r["pd_score_current"]) if r["pd_score_current"] else None,
        model_version    = r["modelversion"],
    ) for r in rows]


# ============================================================
# ENDPOINTS — NBO RECOMMENDATIONS
# ============================================================

@app.get(
    "/api/v1/nbo/client/{client_id}",
    response_model = list[NBOOffer],
    tags           = ["NBO Recommendations"],
    summary        = "Get today's product recommendations for a client",
)
def get_client_nbo(client_id: int):
    """
    Returns the client's top NBO offers for the latest snapshot date.
    Offers are ordered by rank (1 = highest propensity).
    The CRM shows these to the relationship manager during client calls.
    """
    rows = query_many("""
        SELECT
            nb.rank,
            p.productcode      AS product_code,
            p.productname      AS product_name,
            p.category,
            nb.propensityscore AS propensity_score,
            nb.method,
            nb.eligibleflag    AS eligible,
            nb.suppressreason  AS suppress_reason
        FROM ml.NBO_Scores nb
        JOIN public.Products p ON p.productid = nb.productid
        WHERE nb.clientid    = %s
          AND nb.snapshotdate = (
              SELECT MAX(snapshotdate) FROM ml.NBO_Scores WHERE clientid = %s
          )
        ORDER BY nb.rank
    """, (client_id, client_id))

    if not rows:
        raise HTTPException(
            404,
            f"No NBO recommendations found for client {client_id}. "
            "Run nbo_train.py --mode daily to generate recommendations."
        )

    return [NBOOffer(**r) for r in rows]


@app.post(
    "/api/v1/nbo/accept",
    status_code = 201,
    tags        = ["NBO Recommendations"],
    summary     = "Record client acceptance or rejection of an NBO offer",
)
def record_nbo_acceptance(payload: NBOAcceptPayload):
    """
    CRM writes back whether the client accepted an NBO offer.
    Closes the feedback loop for model retraining.
    accepted=true  → client took the product.
    accepted=false → offer shown but client declined.
    """
    prod = query_one(
        "SELECT productid FROM public.Products WHERE productcode = %s",
        (payload.product_code,)
    )
    if not prod:
        raise HTTPException(404, f"Product code '{payload.product_code}' not found.")

    nbo_score = query_one("""
        SELECT nbo_scoreid
        FROM ml.NBO_Scores
        WHERE clientid     = %s
          AND productid    = %s
          AND snapshotdate = %s
        LIMIT 1
    """, (payload.client_id, prod["productid"], payload.offer_date))

    execute("""
        INSERT INTO public.NBO_Accepted
            (clientid, productid, nbo_scoreid, offerdate,
             accepteddate, acceptedflag, channelcode, applicationid_new)
        VALUES (%s, %s, %s, %s,
                CASE WHEN %s THEN CURRENT_DATE ELSE NULL END,
                %s, %s, %s)
        ON CONFLICT DO NOTHING
    """, (
        payload.client_id,
        prod["productid"],
        nbo_score["nbo_scoreid"] if nbo_score else None,
        payload.offer_date,
        payload.accepted,
        payload.accepted,
        payload.channel_code,
        payload.new_application_id,
    ))

    return {
        "status":     "recorded",
        "client_id":  payload.client_id,
        "product":    payload.product_code,
        "accepted":   payload.accepted,
        "offer_date": str(payload.offer_date),
    }


# ============================================================
# ENDPOINTS — CLIENT 360
# ============================================================

@app.get(
    "/api/v1/client/{client_id}",
    response_model = ClientProfile,
    tags           = ["Client 360"],
    summary        = "Full client risk profile — PD + NPL + NBO in one call",
)
def get_client_profile(client_id: int):
    """
    Single endpoint for the CRM client detail page.
    Returns demographics, latest PD score, NPL tier,
    top NBO offer, and joint risk classification.
    """
    row = query_one("""
        SELECT * FROM rpt.vw_Client360 WHERE clientid = %s
    """, (client_id,))

    if not row:
        raise HTTPException(404, f"Client {client_id} not found.")

    return ClientProfile(
        client_id            = row["clientid"],
        age_years            = row["age_years"],
        gender               = row["gender"],
        income_total         = float(row["incometotal"])         if row["incometotal"]         else None,
        income_type          = row["incometype"],
        family_status        = row["familystatus"],
        own_car              = row["owncar"],
        own_realty           = row["ownrealty"],
        pd_score             = float(row["pd_score"])            if row["pd_score"]            else None,
        pd_risk_tier         = row["pd_risk_tier"],
        pd_score_date        = row["pd_score_date"],
        npl_score            = float(row["npl_score"])           if row["npl_score"]           else None,
        npl_risk_tier        = row["npl_risk_tier"],
        npl_score_date       = row["npl_score_date"],
        top_offer_name       = row["top_offer_name"],
        top_offer_category   = row["top_offer_category"],
        top_offer_propensity = float(row["top_offer_propensity"]) if row["top_offer_propensity"] else None,
        nbo_score_date       = row["nbo_score_date"],
        joint_risk_class     = row["joint_risk_class"],
        nbo_eligible         = row["nbo_eligible"],
        n_products           = row["n_products"],
    )


@app.get(
    "/api/v1/client/{client_id}/risk-summary",
    tags    = ["Client 360"],
    summary = "Compact risk summary for CRM sidebar widget",
)
def get_risk_summary(client_id: int):
    """
    Lightweight endpoint for CRM sidebar — essential risk signals only,
    no demographics or loan detail.
    """
    row = query_one("""
        SELECT clientid, pd_score, pd_risk_tier,
               npl_score, npl_risk_tier,
               top_offer_name, joint_risk_class, nbo_eligible
        FROM rpt.vw_Client360
        WHERE clientid = %s
    """, (client_id,))

    if not row:
        raise HTTPException(404, f"Client {client_id} not found.")

    return {
        "client_id":        client_id,
        "pd_score":         float(row["pd_score"])  if row["pd_score"]  else None,
        "pd_risk_tier":     row["pd_risk_tier"],
        "npl_score":        float(row["npl_score"]) if row["npl_score"] else None,
        "npl_risk_tier":    row["npl_risk_tier"],
        "joint_risk_class": row["joint_risk_class"],
        "nbo_eligible":     row["nbo_eligible"],
        "top_offer":        row["top_offer_name"],
        "alert": (
            "⚠ NPL risk elevated — suppress cross-sell"
            if row["npl_risk_tier"] in ("CONCERN", "CRITICAL")
            else "✓ No active alerts"
        ),
    }


# ============================================================
# ENDPOINTS — PIPELINE
# ============================================================

@app.get(
    "/api/v1/pipeline/status",
    response_model = list[PipelineStatus],
    tags           = ["Pipeline"],
    summary        = "Last N ETL run steps across all pipelines",
)
def get_pipeline_status(n: int = Query(20, ge=1, le=100)):
    """
    Returns the most recent ETL run log entries (PD, NPL, NBO steps).
    Used by the ops team to verify nightly runs completed.
    """
    rows = query_many("""
        SELECT stepname      AS step_name,
               status,
               rundate       AS run_date,
               rowsprocessed AS rows_processed,
               durationsec   AS duration_sec,
               startedat     AS started_at,
               finishedat    AS finished_at,
               errormessage  AS error
        FROM stg.ETL_RunLog
        ORDER BY runid DESC
        LIMIT %s
    """, (n,))
    return [PipelineStatus(**r) for r in rows]


@app.get(
    "/api/v1/pipeline/health",
    tags    = ["Pipeline"],
    summary = "Quick health check — did all three models run today?",
)
def get_pipeline_health():
    """
    Returns ok / stale / never_run for each pipeline.
    Called by the ops monitoring dashboard.
    """
    row = query_one("""
        SELECT
            MAX(CASE WHEN stepname LIKE 'features%%' AND status = 'success'
                     THEN rundate END) AS pd_last_success,
            MAX(CASE WHEN stepname = 'npl_scoring'  AND status = 'success'
                     THEN rundate END) AS npl_last_success,
            MAX(CASE WHEN stepname = 'nbo_scoring'  AND status = 'success'
                     THEN rundate END) AS nbo_last_success,
            MAX(CASE WHEN status = 'failed'
                     THEN errormessage END) AS last_error
        FROM stg.ETL_RunLog
        WHERE rundate >= CURRENT_DATE - INTERVAL '2 days'
    """)

    today = date.today()

    def _status(last_run, allowed_age_days: int = 0) -> str:
        if last_run is None:      return "never_run"
        if last_run >= today - timedelta(days=allowed_age_days): return "ok"
        return "stale"

    statuses = {
        "pd": _status(row["pd_last_success"], 1),
        "npl": _status(row["npl_last_success"]),
        "nbo": _status(row["nbo_last_success"]),
    }
    overall = "ok" if all(status == "ok" for status in statuses.values()) else "degraded"

    return {
        "checked_at"  : datetime.now().isoformat(),
        "pd_pipeline" : {"status": statuses["pd"],
                         "last_success": str(row["pd_last_success"])},
        "npl_pipeline": {"status": statuses["npl"],
                         "last_success": str(row["npl_last_success"])},
        "nbo_pipeline": {"status": statuses["nbo"],
                         "last_success": str(row["nbo_last_success"])},
        "last_error"  : None if overall == "ok" else row["last_error"],
        "overall"     : overall,
    }

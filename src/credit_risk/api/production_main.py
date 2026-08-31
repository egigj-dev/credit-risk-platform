"""Full production API plus the operator dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from credit_risk.config import Settings
from credit_risk.core.full_database import FullDatabasePortfolio
from credit_risk.full_cli import _db_params, _load
from credit_risk.production import run_daily


class OfferAcceptance(BaseModel):
    product_code: str


settings = Settings.from_env()
if not settings.database_url:
    raise RuntimeError("DATABASE_URL is required for the production application")

crm = _load("crm_api.py", "credit_risk_production_crm")
crm.PG = _db_params(settings.database_url)
app = crm.app
app.title = "Credit Risk Platform"
app.description = "Production PD, NPL, NBO, Client 360, CRM API, and operator dashboard"
portfolio = FullDatabasePortfolio(settings.database_url)
static_dir = Path(__file__).resolve().parents[1] / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="platform-static")


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/health/live", tags=["Health"])
def live() -> dict:
    return {"status": "ok"}


@app.get("/health/ready", tags=["Health"])
def ready() -> dict:
    portfolio.summary()
    return {"status": "ready", "mode": "production"}


@app.get("/api/v1/summary", tags=["Dashboard"])
def summary() -> dict:
    return portfolio.summary()


@app.get("/api/v1/clients", tags=["Dashboard"])
def clients(
    query: str = Query("", max_length=100), risk: str = Query("", max_length=12)
) -> list[dict]:
    return portfolio.list_clients(query, risk)


@app.get("/api/v1/clients/{client_id}", tags=["Dashboard"])
def client(client_id: int) -> dict:
    row = portfolio.get_client(client_id)
    if not row:
        raise HTTPException(404, "Client not found")
    return row


@app.post("/api/v1/clients/{client_id}/offers/accept", tags=["Dashboard"])
def accept_offer(client_id: int, body: OfferAcceptance) -> dict:
    try:
        return portfolio.accept_offer(client_id, body.product_code)
    except KeyError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/v1/pipeline/run", tags=["Pipeline"])
def trigger_pipeline() -> dict:
    # Source transactions are aggregated through the preceding complete day.
    run_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    return run_daily(run_date, settings).as_dict()

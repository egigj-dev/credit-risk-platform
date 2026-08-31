"""PostgreSQL-backed dashboard entrypoint: uvicorn credit_risk.api.db_main:app."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from credit_risk.config import Settings
from credit_risk.core.database import DatabasePortfolio


class OfferAcceptance(BaseModel):
    product_code: str


settings = Settings.from_env()
if not settings.database_url:
    raise RuntimeError("DATABASE_URL is required for the PostgreSQL application")
portfolio = DatabasePortfolio(settings.database_url)


@asynccontextmanager
async def lifespan(app: FastAPI):
    portfolio.summary()  # fail early when migrations or connectivity are missing
    yield


app = FastAPI(title="Credit Risk Platform", version="0.1.0", lifespan=lifespan)
static_dir = Path(__file__).resolve().parents[1] / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> dict:
    portfolio.summary()
    return {"status": "ready", "mode": "database"}


@app.get("/api/v1/summary")
def summary() -> dict:
    return portfolio.summary()


@app.get("/api/v1/clients")
def clients(
    query: str = Query("", max_length=100), risk: str = Query("", max_length=12)
) -> list[dict]:
    return portfolio.list_clients(query, risk)


@app.get("/api/v1/clients/{client_id}")
def client(client_id: int) -> dict:
    row = portfolio.get_client(client_id)
    if not row:
        raise HTTPException(status_code=404, detail="Client not found")
    return row


@app.post("/api/v1/clients/{client_id}/offers/accept")
def accept_offer(client_id: int, body: OfferAcceptance) -> dict:
    try:
        return portfolio.accept_offer(client_id, body.product_code)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/pipeline/status")
def status() -> dict:
    return {"mode": "database", "latest": portfolio.latest_run()}


@app.post("/api/v1/pipeline/run")
def run_pipeline() -> dict:
    try:
        return portfolio.run_pipeline()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

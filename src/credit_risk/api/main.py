from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from credit_risk.config import Settings
from credit_risk.core.demo import DemoPortfolio


class OfferAcceptance(BaseModel):
    product_code: str


def create_app() -> FastAPI:
    settings = Settings.from_env()
    portfolio = DemoPortfolio()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Demo mode is intentionally zero-setup. Database mode is introduced via migrations
        # and the same API contract; it is not silently allowed to fall back to demo data.
        app.state.portfolio = portfolio
        app.state.settings = settings
        yield

    app = FastAPI(
        title="Credit Risk Platform",
        version="0.1.0",
        description="PD, NPL early-warning and next-best-offer MVP",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
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
        return {
            "status": "ready",
            "mode": settings.mode,
            "database_configured": bool(settings.database_url),
        }

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
        row = portfolio.clients.get(client_id)
        if not row:
            raise HTTPException(status_code=404, detail="Client not found")
        return row

    @app.get("/api/v1/clients/{client_id}/offers")
    def offers(client_id: int) -> list[dict]:
        row = portfolio.clients.get(client_id)
        if not row:
            raise HTTPException(status_code=404, detail="Client not found")
        return row["offers"]

    @app.post("/api/v1/clients/{client_id}/offers/accept")
    def accept_offer(client_id: int, body: OfferAcceptance) -> dict:
        if client_id not in portfolio.clients:
            raise HTTPException(status_code=404, detail="Client not found")
        try:
            return portfolio.accept_offer(client_id, body.product_code)
        except KeyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/pipeline/status")
    def pipeline_status() -> dict:
        return {
            "mode": settings.mode,
            "latest": portfolio.run_history[-1] if portfolio.run_history else None,
            "steps": [
                "source_staging",
                "data_validation",
                "feature_store_rebuild",
                "pd_scoring",
                "npl_scoring",
                "nbo_scoring",
                "reporting_refresh",
            ],
        }

    @app.post("/api/v1/pipeline/run")
    def run_pipeline() -> dict:
        if settings.mode != "demo":
            raise HTTPException(
                status_code=501,
                detail="Database orchestration will be enabled after migrations and artifact import",
            )
        return portfolio.run_pipeline()

    return app


app = create_app()

"""Production orchestration for the full PostgreSQL PD/NPL/NBO platform."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import psycopg2

from credit_risk.config import Settings
from credit_risk.full_cli import run_nbo, run_npl, run_pd_etl


@dataclass(frozen=True)
class PipelineResult:
    run_date: date
    steps: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "status": "success",
            "run_date": self.run_date.isoformat(),
            "steps": list(self.steps),
        }


def _database_url(settings: Settings) -> str:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for the production pipeline")
    return settings.database_url


def run_sql_preprocessing(run_date: date, settings: Settings) -> bool:
    """Build SQL inputs, or reuse a complete checkpoint for an idempotent replay."""
    with psycopg2.connect(_database_url(settings)) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              EXISTS (SELECT 1 FROM stg.ETL_RunLog
                      WHERE RunDate=%s AND StepName='validation' AND Status='success'),
              EXISTS (SELECT 1 FROM stg.ETL_RunLog
                      WHERE RunDate=%s AND StepName='feature_build' AND Status='success'),
              EXISTS (SELECT 1 FROM stg.FeatureStore_Raw WHERE AsOfDate=%s)
            """,
            (run_date, run_date, run_date),
        )
        if all(cur.fetchone()):
            return False
        cur.execute("SELECT stg.usp_StageSourceData(%s)", (run_date,))
        cur.execute("SELECT stg.usp_ValidateStagedData(%s)", (run_date,))
        cur.execute("SELECT ml.usp_BuildFeatureStoreRaw(%s)", (run_date,))
    return True


def validate_nbo_artifacts(settings: Settings) -> None:
    """Fail early when a nearest-neighbour index was trained on another population."""
    config_path = settings.artifact_dir / "nbo_artifacts" / "nbo_config.json"
    if not config_path.exists():
        raise RuntimeError(f"NBO artifact contract is missing: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    expected = int(config.get("n_clients", -1))
    with psycopg2.connect(_database_url(settings)) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM public.Clients")
        actual = int(cur.fetchone()[0])
    if expected != actual:
        raise RuntimeError(
            "NBO artifacts were trained for "
            f"{expected:,} clients, but the database contains {actual:,}. "
            "Run `credit-risk-model nbo --mode train` against this database "
            "before daily scoring."
        )


def refresh_reporting(run_date: date, settings: Settings) -> None:
    """Refresh reporting, safely handling a materialized view's first population."""
    with psycopg2.connect(_database_url(settings)) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ispopulated FROM pg_matviews "
            "WHERE schemaname='rpt' AND matviewname='vw_applicationdetail'"
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Required materialized view rpt.vw_ApplicationDetail is missing")
        if not row[0]:
            cur.execute("REFRESH MATERIALIZED VIEW rpt.vw_ApplicationDetail")
    with psycopg2.connect(_database_url(settings)) as conn, conn.cursor() as cur:
        cur.execute("SELECT rpt.usp_RefreshReportingCache(%s)", (run_date,))


def run_daily(
    run_date: date,
    settings: Settings,
    *,
    refresh_ownership: bool = False,
) -> PipelineResult:
    """Run the canonical daily pipeline and stop immediately on any failed step."""
    steps: list[str] = []
    run_sql_preprocessing(run_date, settings)
    steps.extend(("source_staging", "data_validation", "feature_store_raw"))
    run_pd_etl(run_date, settings)
    steps.append("pd_scoring")
    run_npl("score", settings)
    steps.append("npl_scoring")
    if refresh_ownership:
        run_nbo("ownership", settings)
        steps.append("nbo_ownership")
    validate_nbo_artifacts(settings)
    run_nbo("daily", settings)
    steps.append("nbo_scoring")
    refresh_reporting(run_date, settings)
    steps.append("reporting_refresh")
    return PipelineResult(run_date, tuple(steps))

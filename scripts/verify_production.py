"""Read-only end-to-end verification for a populated production database."""

from __future__ import annotations

import argparse
import json
import os

import psycopg2
import psycopg2.extras
from fastapi.testclient import TestClient

COUNT_SQL = """
SELECT
  (SELECT COUNT(*) FROM public.Clients)::int AS clients,
  (SELECT COUNT(*) FROM ml.FeatureStore
   WHERE AsOfDate=(SELECT MAX(AsOfDate) FROM ml.FeatureStore))::int AS features,
  (SELECT COUNT(*) FROM ml.PD_Scores
   WHERE ScoreDate=(SELECT MAX(ScoreDate) FROM ml.PD_Scores))::int AS pd_scores,
  (SELECT COUNT(*) FROM ml.SHAP_Values
   WHERE ScoreDate=(SELECT MAX(ScoreDate) FROM ml.SHAP_Values))::int AS pd_shap,
  (SELECT COUNT(*) FROM ml.NPL_Scores
   WHERE ScoreDate=(SELECT MAX(ScoreDate) FROM ml.NPL_Scores))::int AS npl_scores,
  (SELECT COUNT(*) FROM ml.NPL_SHAP_Values
   WHERE ScoreDate=(SELECT MAX(ScoreDate) FROM ml.NPL_SHAP_Values))::int AS npl_shap,
  (SELECT COUNT(*) FROM ml.NBO_Scores
   WHERE SnapshotDate=(SELECT MAX(SnapshotDate) FROM ml.NBO_Scores))::int AS nbo_scores,
  (SELECT COUNT(*) FROM ml.NBO_Scores
   WHERE SnapshotDate=(SELECT MAX(SnapshotDate) FROM ml.NBO_Scores)
     AND EligibleFlag)::int AS eligible_offers,
  (SELECT COUNT(*) FROM public.NBO_ClientOwnership)::int AS ownership,
  (SELECT COUNT(*) FROM rpt.vw_ApplicationDetail)::int AS reporting_rows
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the populated full platform")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL is required")
    os.environ["DATABASE_URL"] = args.database_url
    os.environ["APP_MODE"] = "database"

    with (
        psycopg2.connect(args.database_url, cursor_factory=psycopg2.extras.RealDictCursor) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(COUNT_SQL)
        counts = dict(cur.fetchone())
        cur.execute("""
            SELECT clientid FROM ml.NBO_Scores
            WHERE SnapshotDate=(SELECT MAX(SnapshotDate) FROM ml.NBO_Scores)
              AND EligibleFlag ORDER BY clientid LIMIT 1
        """)
        row = cur.fetchone()
        cur.execute("""
            SELECT StepName, Status FROM stg.ETL_RunLog
            WHERE RunID IN (
              SELECT MAX(RunID) FROM stg.ETL_RunLog
              WHERE StepName IN ('staging','validation','feature_build','write_outputs',
                                 'npl_scoring','nbo_scoring','reporting')
              GROUP BY StepName
            )
        """)
        latest_steps = {item["stepname"]: item["status"] for item in cur.fetchall()}

    if any(value <= 0 for value in counts.values()):
        raise RuntimeError(f"One or more production outputs are empty: {counts}")
    expected = {
        "features": counts["clients"],
        "pd_scores": counts["clients"],
        "pd_shap": counts["pd_scores"] * 5,
        "npl_scores": counts["clients"],
        "npl_shap": counts["npl_scores"] * 5,
        "ownership": counts["clients"],
        "reporting_rows": counts["clients"],
    }
    mismatches = {
        key: {"actual": counts[key], "expected": value}
        for key, value in expected.items()
        if counts[key] != value
    }
    if mismatches:
        raise RuntimeError(f"Production output cardinality mismatch: {mismatches}")
    required_steps = {
        "staging",
        "validation",
        "feature_build",
        "write_outputs",
        "npl_scoring",
        "nbo_scoring",
        "reporting",
    }
    failed = {
        step: latest_steps.get(step)
        for step in required_steps
        if latest_steps.get(step) != "success"
    }
    if failed:
        raise RuntimeError(f"Latest required pipeline steps are not successful: {failed}")
    if not row:
        raise RuntimeError("No eligible NBO client is available for API verification")

    from credit_risk.api.production_main import app

    client_id = row["clientid"]
    paths = (
        "/",
        "/docs",
        "/health/live",
        "/health/ready",
        "/api/v1/summary",
        "/api/v1/clients",
        f"/api/v1/clients/{client_id}",
        f"/api/v1/scoring/client/{client_id}",
        f"/api/v1/npl/client/{client_id}",
        f"/api/v1/nbo/client/{client_id}",
        f"/api/v1/client/{client_id}",
        "/api/v1/pipeline/status",
        "/api/v1/pipeline/health",
    )
    with TestClient(app) as client:
        statuses = {path: client.get(path).status_code for path in paths}
        health = client.get("/api/v1/pipeline/health").json()
    bad_routes = {path: status for path, status in statuses.items() if status != 200}
    if bad_routes:
        raise RuntimeError(f"API verification failed: {bad_routes}")
    if health["overall"] != "ok":
        raise RuntimeError(f"Pipeline health is not OK: {health}")

    print(
        json.dumps(
            {
                "status": "ok",
                "counts": counts,
                "latest_steps": latest_steps,
                "api_routes_verified": len(statuses),
                "pipeline_health": health["overall"],
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

from datetime import date
from uuid import uuid4

import psycopg2
import psycopg2.extras

from credit_risk.core.demo import model_explanations, recommendation_policy, tier


class DatabasePortfolio:
    """PostgreSQL implementation of the dashboard portfolio contract."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _connect(self):
        return psycopg2.connect(self.database_url, cursor_factory=psycopg2.extras.RealDictCursor)

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

    def summary(self) -> dict:
        row = (
            self._one("""
          SELECT COUNT(*)::int AS client_count, COALESCE(AVG(pd_score),0)::float AS mean_pd,
                 COUNT(*) FILTER (WHERE pd_tier IN ('HIGH','CRITICAL'))::int AS high_risk_count,
                 COUNT(*) FILTER (
                     WHERE pd_tier IN ('HIGH','CRITICAL') OR npl_tier IN ('HIGH','CRITICAL')
                 )::int AS protected_client_count,
                 COUNT(*) FILTER (WHERE npl_tier IN ('HIGH','CRITICAL'))::int AS npl_watchlist_count
          FROM rpt.vw_client_risk
        """)
            or {}
        )
        row["offer_count"] = (
            self._one(
                "SELECT COUNT(*)::int AS n FROM ml.nbo_scores WHERE snapshot_date=current_date"
            )
            or {"n": 0}
        )["n"]
        row["last_run"] = self.latest_run()
        return row

    def latest_run(self) -> dict | None:
        return self._one(
            "SELECT run_id, status, started_at, finished_at FROM stg.etl_run_log ORDER BY run_id DESC LIMIT 1"
        )

    def list_clients(self, query: str = "", risk: str = "") -> list[dict]:
        needle = f"%{query.strip()}%"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
              SELECT client_id, full_name AS name, segment, annual_income::float, pd_score::float, pd_tier,
                     npl_score::float, npl_tier FROM rpt.vw_client_risk
              WHERE (%s='' OR full_name ILIKE %s OR client_id::text ILIKE %s)
                AND (%s='' OR pd_tier=%s) ORDER BY pd_score DESC NULLS LAST
            """,
                (query.strip(), needle, needle, risk.upper(), risk.upper()),
            )
            return [dict(x) for x in cur.fetchall()]

    def offers(self, client_id: int) -> list[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
              SELECT p.product_code, p.product_name, n.rank, n.confidence::float, n.reason
              FROM ml.nbo_scores n JOIN public.products p ON p.product_id=n.product_id
              WHERE n.client_id=%s AND n.snapshot_date=current_date AND NOT EXISTS (
                SELECT 1 FROM public.nbo_accepted a WHERE a.client_id=n.client_id AND a.product_id=n.product_id)
              ORDER BY n.rank
            """,
                (client_id,),
            )
            return [dict(x) for x in cur.fetchall()]

    def get_client(self, client_id: int) -> dict | None:
        row = self._one(
            """
          SELECT client_id, full_name AS name, segment, annual_income::float, utilisation::float, days_past_due,
                 COALESCE(pd_score,0)::float AS pd_score, COALESCE(pd_tier,'UNSCORED') AS pd_tier,
                 COALESCE(npl_score,0)::float AS npl_score, COALESCE(npl_tier,'UNSCORED') AS npl_tier
          FROM rpt.vw_client_risk WHERE client_id=%s
        """,
            (client_id,),
        )
        if row:
            policy = recommendation_policy(row["pd_tier"], row["npl_tier"])
            row.update(
                owned_products=[],
                offers=[] if policy["mode"] in {"SUPPORT", "INTERVENE"} else self.offers(client_id),
                recommendation_policy=policy,
                model_explanations=model_explanations(
                    row["utilisation"], row["days_past_due"], row["pd_score"], row["npl_score"]
                ),
                updated_at="Persisted PostgreSQL record",
            )
        return row

    def accept_offer(self, client_id: int, product_code: str) -> dict:
        risk = self._one(
            """
            SELECT COALESCE(pd_tier,'UNSCORED') AS pd_tier,
                   COALESCE(npl_tier,'UNSCORED') AS npl_tier
            FROM rpt.vw_client_risk WHERE client_id=%s
            """,
            (client_id,),
        )
        if risk and recommendation_policy(risk["pd_tier"], risk["npl_tier"])["mode"] in {"SUPPORT", "INTERVENE"}:
            raise KeyError("Product marketing is paused for this client's current risk state")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
              INSERT INTO public.nbo_accepted(client_id, product_id)
              SELECT %s, p.product_id FROM public.products p JOIN ml.nbo_scores n ON n.product_id=p.product_id
              WHERE n.client_id=%s AND n.snapshot_date=current_date AND p.product_code=%s
              ON CONFLICT DO NOTHING RETURNING product_id
            """,
                (client_id, client_id, product_code),
            )
            if not cur.fetchone():
                raise KeyError("Offer is not available for this client")
        return {"status": "accepted", "client_id": client_id, "product_code": product_code}

    def run_pipeline(self) -> dict:
        steps = [
            "source_staging",
            "data_validation",
            "feature_store_rebuild",
            "pd_scoring",
            "npl_scoring",
            "nbo_scoring",
            "reporting_refresh",
        ]
        correlation_id = str(uuid4())
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT client_id, utilisation::float, days_past_due FROM public.clients ORDER BY client_id"
            )
            clients = list(cur.fetchall())
            if not clients:
                raise ValueError("No clients are loaded. Run scripts/load_demo_data.py first.")
            cur.execute(
                "SELECT product_id FROM public.products WHERE is_active ORDER BY product_id"
            )
            products = list(cur.fetchall())
            for step in steps:
                cur.execute(
                    "INSERT INTO stg.etl_run_log(correlation_id,step_name,status) VALUES (%s,%s,'running') RETURNING run_id",
                    (correlation_id, step),
                )
                run_id = cur.fetchone()["run_id"]
                if step in {"pd_scoring", "npl_scoring"}:
                    rows = []
                    for c in clients:
                        pd = min(
                            0.92,
                            max(0.015, 0.035 + c["utilisation"] * 0.38 + c["days_past_due"] / 260),
                        )
                        score = (
                            pd
                            if step == "pd_scoring"
                            else min(0.91, max(0.01, pd * 0.74 + c["days_past_due"] / 180))
                        )
                        rows.append(
                            (
                                c["client_id"],
                                date.today(),
                                "demo-pd-v1" if step == "pd_scoring" else "demo-npl-v1",
                                score,
                                tier(score),
                            )
                        )
                    table = "ml.pd_scores" if step == "pd_scoring" else "ml.npl_scores"
                    column = "pd_score" if step == "pd_scoring" else "npl_score"
                    psycopg2.extras.execute_values(
                        cur,
                        f"INSERT INTO {table}(client_id,score_date,model_version,{column},risk_tier) VALUES %s ON CONFLICT (client_id,score_date,model_version) DO UPDATE SET {column}=EXCLUDED.{column},risk_tier=EXCLUDED.risk_tier",
                        rows,
                    )
                elif step == "nbo_scoring":
                    rows = []
                    for i, c in enumerate(clients):
                        pd = min(
                            0.92,
                            max(0.015, 0.035 + c["utilisation"] * 0.38 + c["days_past_due"] / 260),
                        )
                        if pd < 0.5:
                            for rank in range(1, 4):
                                product = products[(i + rank - 1) % len(products)]
                                rows.append(
                                    (
                                        c["client_id"],
                                        date.today(),
                                        "demo-nbo-v1",
                                        product["product_id"],
                                        rank,
                                        max(0.15, 0.88 - pd - rank * 0.09),
                                        "Fits current segment and product profile",
                                    )
                                )
                    psycopg2.extras.execute_values(
                        cur,
                        "INSERT INTO ml.nbo_scores(client_id,snapshot_date,model_version,product_id,rank,confidence,reason) VALUES %s ON CONFLICT (client_id,snapshot_date,model_version,product_id) DO UPDATE SET rank=EXCLUDED.rank,confidence=EXCLUDED.confidence,reason=EXCLUDED.reason",
                        rows,
                    )
                cur.execute(
                    "UPDATE stg.etl_run_log SET status='success',rows_processed=%s,finished_at=now() WHERE run_id=%s",
                    (len(clients), run_id),
                )
        return {"run_id": correlation_id, "status": "success", "steps": steps}

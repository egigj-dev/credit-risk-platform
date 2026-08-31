"""Dashboard adapter for the full legacy-compatible production schema."""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

from credit_risk.core.demo import model_explanations, recommendation_policy


class FullDatabasePortfolio:
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
            SELECT COUNT(*)::int AS client_count,
                   COALESCE(AVG(pd_score), 0)::float AS mean_pd,
                   COUNT(*) FILTER (WHERE pd_risk_tier IN ('HIGH','CRITICAL'))::int AS high_risk_count,
                   COUNT(*) FILTER (
                       WHERE pd_risk_tier IN ('HIGH','CRITICAL')
                          OR npl_risk_tier IN ('CONCERN','CRITICAL')
                   )::int AS protected_client_count,
                   COUNT(*) FILTER (WHERE npl_risk_tier IN ('WATCH','CONCERN','CRITICAL'))::int AS npl_watchlist_count
            FROM rpt.vw_Client360
        """)
            or {}
        )
        offers = self._one("""
            SELECT COUNT(*)::int AS n FROM ml.NBO_Scores n
            WHERE n.snapshotdate=(SELECT MAX(snapshotdate) FROM ml.NBO_Scores)
              AND n.eligibleflag
              AND NOT EXISTS (
                SELECT 1 FROM public.NBO_Accepted a
                WHERE a.clientid=n.clientid AND a.productid=n.productid AND a.acceptedflag)
        """)
        row["offer_count"] = (offers or {"n": 0})["n"]
        return row

    def list_clients(self, query: str = "", risk: str = "") -> list[dict]:
        needle = f"%{query.strip()}%"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT clientid AS client_id,
                       'Client ' || clientid AS name,
                       COALESCE(NULLIF(incometype,''), 'Retail') AS segment,
                       COALESCE(pd_score,0)::float AS pd_score,
                       COALESCE(pd_risk_tier,'UNSCORED') AS pd_tier,
                       COALESCE(npl_score,0)::float AS npl_score,
                       COALESCE(npl_risk_tier,'UNSCORED') AS npl_tier
                FROM rpt.vw_Client360
                WHERE (%s='' OR clientid::text ILIKE %s OR COALESCE(incometype,'') ILIKE %s)
                  AND (%s='' OR pd_risk_tier=%s)
                ORDER BY pd_score DESC NULLS LAST, clientid
                LIMIT 500
            """,
                (query.strip(), needle, needle, risk.upper(), risk.upper()),
            )
            return [dict(row) for row in cur.fetchall()]

    def offers(self, client_id: int) -> list[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.productcode AS product_code, p.productname AS product_name,
                       n.rank, n.propensityscore::float AS confidence,
                       COALESCE(n.method, 'MODEL') || ' recommendation · ' || p.category AS reason
                FROM ml.NBO_Scores n JOIN public.Products p ON p.productid=n.productid
                WHERE n.clientid=%s
                  AND n.snapshotdate=(SELECT MAX(snapshotdate) FROM ml.NBO_Scores)
                  AND n.eligibleflag
                  AND NOT EXISTS (
                    SELECT 1 FROM public.NBO_Accepted a
                    WHERE a.clientid=n.clientid AND a.productid=n.productid AND a.acceptedflag)
                ORDER BY n.rank
            """,
                (client_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    def model_explanations(self, client_id: int) -> dict:
        result = {"type": "shap", "pd": [], "npl": []}
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    WITH latest AS (
                        SELECT applicationid, scoredate FROM ml.PD_Scores
                        WHERE clientid=%s ORDER BY scoredate DESC LIMIT 1
                    )
                    SELECT s.featurename AS feature, s.featurevalue::float AS value,
                           s.shapvalue::float AS shap_value
                    FROM ml.SHAP_Values s JOIN latest l
                      ON l.applicationid=s.applicationid AND l.scoredate=s.scoredate
                    ORDER BY s.shaprank LIMIT 5
                    """,
                    (client_id,),
                )
                result["pd"] = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                    SELECT featurename AS feature, featurevalue::float AS value,
                           shapvalue::float AS shap_value
                    FROM ml.NPL_SHAP_Values
                    WHERE clientid=%s
                      AND scoredate=(SELECT MAX(scoredate) FROM ml.NPL_SHAP_Values WHERE clientid=%s)
                    ORDER BY shaprank LIMIT 5
                    """,
                    (client_id, client_id),
                )
                result["npl"] = [dict(row) for row in cur.fetchall()]
        except psycopg2.Error:
            return {"type": "unavailable", "pd": [], "npl": []}
        return result

    def get_client(self, client_id: int) -> dict | None:
        row = self._one(
            """
            SELECT c.clientid AS client_id, 'Client ' || c.clientid AS name,
                   COALESCE(NULLIF(c.incometype,''), 'Retail') AS segment,
                   COALESCE(c.incometotal,0)::float AS annual_income,
                   LEAST(1, GREATEST(0, COALESCE(fs.bureau_debt_limit_ratio,0)))::float AS utilisation,
                   GREATEST(0, COALESCE(fs.pos_dpd_max,0))::int AS days_past_due,
                   COALESCE(c.pd_score,0)::float AS pd_score,
                   COALESCE(c.pd_risk_tier,'UNSCORED') AS pd_tier,
                   COALESCE(c.npl_score,0)::float AS npl_score,
                   COALESCE(c.npl_risk_tier,'UNSCORED') AS npl_tier,
                   COALESCE(c.nbo_score_date,c.npl_score_date,c.pd_score_date,CURRENT_DATE)::text AS updated_at,
                   c.has_personal_loan, c.has_savings, c.has_credit_card,
                   c.has_mortgage, c.has_investment
            FROM rpt.vw_Client360 c
            LEFT JOIN LATERAL (
                SELECT bureau_debt_limit_ratio, pos_dpd_max FROM ml.FeatureStore f
                WHERE f.clientid=c.clientid ORDER BY f.asofdate DESC LIMIT 1
            ) fs ON TRUE
            WHERE c.clientid=%s
        """,
            (client_id,),
        )
        if not row:
            return None
        products = [
            label
            for field, label in (
                ("has_personal_loan", "PERSONAL_LOAN"),
                ("has_savings", "SAVINGS"),
                ("has_credit_card", "CREDIT_CARD"),
                ("has_mortgage", "MORTGAGE"),
                ("has_investment", "INVESTMENT"),
            )
            if row.pop(field)
        ]
        policy = recommendation_policy(row["pd_tier"], row["npl_tier"])
        offers = [] if policy["mode"] in {"SUPPORT", "INTERVENE"} else self.offers(client_id)
        explanations = self.model_explanations(client_id)
        if explanations["type"] == "unavailable":
            explanations = model_explanations(
                row["utilisation"], row["days_past_due"], row["pd_score"], row["npl_score"]
            )
        row.update(
            owned_products=products,
            offers=offers,
            recommendation_policy=policy,
            model_explanations=explanations,
        )
        return row

    def accept_offer(self, client_id: int, product_code: str) -> dict:
        risk = self._one(
            """
            SELECT COALESCE(pd_risk_tier,'UNSCORED') AS pd_tier,
                   COALESCE(npl_risk_tier,'UNSCORED') AS npl_tier
            FROM rpt.vw_Client360 WHERE clientid=%s
            """,
            (client_id,),
        )
        if risk and recommendation_policy(risk["pd_tier"], risk["npl_tier"])["mode"] in {"SUPPORT", "INTERVENE"}:
            raise KeyError("Product marketing is paused for this client's current risk state")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.nbo_scoreid, n.productid, n.snapshotdate
                FROM ml.NBO_Scores n JOIN public.Products p ON p.productid=n.productid
                WHERE n.clientid=%s AND p.productcode=%s AND n.eligibleflag
                  AND n.snapshotdate=(SELECT MAX(snapshotdate) FROM ml.NBO_Scores)
                  AND NOT EXISTS (
                    SELECT 1 FROM public.NBO_Accepted a
                    WHERE a.clientid=n.clientid AND a.productid=n.productid AND a.acceptedflag)
                LIMIT 1
            """,
                (client_id, product_code),
            )
            offer = cur.fetchone()
            if not offer:
                raise KeyError("Offer is not available for this client")
            cur.execute(
                """
                INSERT INTO public.NBO_Accepted
                    (clientid,productid,nbo_scoreid,offerdate,accepteddate,acceptedflag,channelcode)
                VALUES (%s,%s,%s,%s,%s,TRUE,'DASHBOARD')
            """,
                (
                    client_id,
                    offer["productid"],
                    offer["nbo_scoreid"],
                    offer["snapshotdate"],
                    datetime.now(timezone.utc).date(),
                ),
            )
        return {"status": "accepted", "client_id": client_id, "product_code": product_code}

    def latest_run(self) -> dict | None:
        return self._one("""
            SELECT runid AS run_id, rundate AS run_date, stepname AS step_name,
                   status, startedat AS started_at, finishedat AS finished_at,
                   rowsprocessed AS rows_processed, errormessage AS error
            FROM stg.ETL_RunLog ORDER BY runid DESC LIMIT 1
        """)

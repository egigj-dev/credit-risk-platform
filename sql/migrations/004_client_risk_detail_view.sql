-- Extend the client-risk view with the operational context consumed by the dashboard.
CREATE OR REPLACE VIEW rpt.vw_client_risk AS
SELECT c.client_id, c.full_name, c.segment, c.annual_income, c.utilisation, c.days_past_due,
       pd.score_date, pd.pd_score, pd.risk_tier AS pd_tier,
       npl.npl_score, npl.risk_tier AS npl_tier
FROM public.clients c
LEFT JOIN LATERAL (
  SELECT * FROM ml.pd_scores s WHERE s.client_id = c.client_id ORDER BY score_date DESC LIMIT 1
) pd ON true
LEFT JOIN LATERAL (
  SELECT * FROM ml.npl_scores s WHERE s.client_id = c.client_id ORDER BY score_date DESC LIMIT 1
) npl ON true;

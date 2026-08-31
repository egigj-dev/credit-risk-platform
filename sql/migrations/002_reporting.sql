CREATE OR REPLACE VIEW rpt.vw_portfolio_risk_summary AS
SELECT score_date, risk_tier, COUNT(*) AS client_count, AVG(pd_score) AS mean_pd
FROM ml.pd_scores
GROUP BY score_date, risk_tier;

CREATE OR REPLACE VIEW rpt.vw_client_risk AS
SELECT c.client_id, c.full_name, c.segment, c.annual_income,
       pd.score_date, pd.pd_score, pd.risk_tier AS pd_tier,
       npl.npl_score, npl.risk_tier AS npl_tier
FROM public.clients c
LEFT JOIN LATERAL (
  SELECT * FROM ml.pd_scores s WHERE s.client_id = c.client_id ORDER BY score_date DESC LIMIT 1
) pd ON true
LEFT JOIN LATERAL (
  SELECT * FROM ml.npl_scores s WHERE s.client_id = c.client_id ORDER BY score_date DESC LIMIT 1
) npl ON true;

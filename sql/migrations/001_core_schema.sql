-- MVP schema. All identifiers are lower-case and migration-safe.
CREATE SCHEMA IF NOT EXISTS stg;
CREATE SCHEMA IF NOT EXISTS ml;
CREATE SCHEMA IF NOT EXISTS rpt;

CREATE TABLE IF NOT EXISTS public.clients (
  client_id BIGINT PRIMARY KEY,
  full_name TEXT NOT NULL,
  segment TEXT,
  annual_income NUMERIC(18,2),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.products (
  product_id BIGSERIAL PRIMARY KEY,
  product_code TEXT NOT NULL UNIQUE,
  product_name TEXT NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS stg.etl_run_log (
  run_id BIGSERIAL PRIMARY KEY,
  correlation_id UUID,
  run_date DATE NOT NULL DEFAULT current_date,
  step_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
  rows_processed BIGINT,
  error_message TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ml.pd_scores (
  client_id BIGINT NOT NULL REFERENCES public.clients(client_id),
  score_date DATE NOT NULL,
  model_version TEXT NOT NULL,
  pd_score NUMERIC(8,6) NOT NULL CHECK (pd_score >= 0 AND pd_score <= 1),
  risk_tier TEXT NOT NULL CHECK (risk_tier IN ('LOW','MEDIUM','HIGH','CRITICAL')),
  PRIMARY KEY (client_id, score_date, model_version)
);

CREATE TABLE IF NOT EXISTS ml.npl_scores (
  client_id BIGINT NOT NULL REFERENCES public.clients(client_id),
  score_date DATE NOT NULL,
  model_version TEXT NOT NULL,
  npl_score NUMERIC(8,6) NOT NULL CHECK (npl_score >= 0 AND npl_score <= 1),
  risk_tier TEXT NOT NULL CHECK (risk_tier IN ('LOW','MEDIUM','HIGH','CRITICAL')),
  PRIMARY KEY (client_id, score_date, model_version)
);

CREATE TABLE IF NOT EXISTS ml.nbo_scores (
  client_id BIGINT NOT NULL REFERENCES public.clients(client_id),
  snapshot_date DATE NOT NULL,
  model_version TEXT NOT NULL,
  product_id BIGINT NOT NULL REFERENCES public.products(product_id),
  rank SMALLINT NOT NULL CHECK (rank BETWEEN 1 AND 5),
  confidence NUMERIC(6,4) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  reason TEXT NOT NULL,
  PRIMARY KEY (client_id, snapshot_date, model_version, product_id)
);

CREATE TABLE IF NOT EXISTS public.nbo_accepted (
  client_id BIGINT NOT NULL REFERENCES public.clients(client_id),
  product_id BIGINT NOT NULL REFERENCES public.products(product_id),
  accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_pd_scores_date_tier ON ml.pd_scores(score_date, risk_tier);
CREATE INDEX IF NOT EXISTS idx_npl_scores_date_tier ON ml.npl_scores(score_date, risk_tier);
CREATE INDEX IF NOT EXISTS idx_nbo_scores_client_date ON ml.nbo_scores(client_id, snapshot_date);

INSERT INTO public.products (product_code, product_name) VALUES
  ('checking_account', 'Checking account'), ('savings_account', 'Savings account'),
  ('credit_card', 'Credit card'), ('personal_loan', 'Personal loan'),
  ('term_deposit', 'Term deposit'), ('life_insurance', 'Life insurance')
ON CONFLICT (product_code) DO UPDATE SET product_name = EXCLUDED.product_name;

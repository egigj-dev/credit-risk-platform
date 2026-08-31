-- ============================================================
-- Credit Risk PD Model — Create Database (PostgreSQL)
-- Data Science Division | Bank | April 2026
-- ============================================================
-- Run this file ONCE as a superuser (e.g. postgres) before
-- all other files.
--
-- Execution order:
--   1. create_database_pg.sql   ← YOU ARE HERE
--   2. credit_risk_ddl_pg.sql
--   3. credit_risk_proc_pg.sql
--   4. credit_risk_etl_pg.py  (nightly, via cron / Airflow)
-- ============================================================

-- ============================================================
-- 1. CREATE DATABASE
-- ============================================================
-- PostgreSQL does not support CREATE DATABASE inside a
-- transaction or DO block.  Connect to the 'postgres' DB as
-- a superuser and run this file; the \gexec trick below makes
-- it idempotent (creates only if absent).

SELECT 'CREATE DATABASE credit_risk_db_v2
    ENCODING    = ''UTF8''
    LC_COLLATE  = ''en_US.UTF-8''
    LC_CTYPE    = ''en_US.UTF-8''
    TEMPLATE    = template0'
WHERE NOT EXISTS (
    SELECT 1 FROM pg_database WHERE datname = 'credit_risk_db_v2'
)\gexec

-- ============================================================
-- 2. CONNECT
-- ============================================================
-- Uncomment when running interactively in psql:
-- \c credit_risk_db_v2;

-- ============================================================
-- 3. EXTENSIONS
-- ============================================================
-- pgcrypto       — required for gen_random_uuid() on PG ≤ 12,
--                  and for encode(sha256(...)) used in row-hash
--                  computation in the staging procedures.
--                  On PG ≥ 13, gen_random_uuid() is built-in
--                  but sha256() still needs pgcrypto unless you
--                  use the built-in digest() from pg 11+.
--                  Install unconditionally to keep the DDL
--                  portable across PG 12-16.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- pg_stat_statements — query-level performance monitoring.
--   Requires postgresql.conf:  shared_preload_libraries = 'pg_stat_statements'
--   and a server restart before this takes effect.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- pg_cron — nightly scheduler.
--   Requires postgresql.conf:  shared_preload_libraries = 'pg_cron'
--   and a server restart.  Uncomment after restarting the server.
-- CREATE EXTENSION IF NOT EXISTS pg_cron;

-- ============================================================
-- 4. SCHEMAS
-- ============================================================
CREATE SCHEMA IF NOT EXISTS stg;   -- staging / ETL audit
CREATE SCHEMA IF NOT EXISTS ml;    -- feature store, scores, monitoring
CREATE SCHEMA IF NOT EXISTS rpt;   -- Power BI reporting views

COMMENT ON SCHEMA stg IS
    'Staging schema — per-run delta tables, ETL run log, '
    'watermark control, validation error log';
COMMENT ON SCHEMA ml IS
    'Machine-learning schema — feature store, PD scores, SHAP '
    'explanations, PSI monitoring, model registry, encoder map';
COMMENT ON SCHEMA rpt IS
    'Reporting schema — Power BI consumption views (read-only)';

-- ============================================================
-- 5. ROLES & GRANTS  (template — adjust to your environment)
-- ============================================================
-- DO $$
-- BEGIN
--     IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'credit_risk_app') THEN
--         CREATE ROLE credit_risk_app LOGIN PASSWORD 'change_me';
--     END IF;
-- END
-- $$;
--
-- GRANT USAGE  ON SCHEMA stg, ml, rpt              TO credit_risk_app;
-- GRANT SELECT, INSERT, UPDATE, DELETE
--       ON ALL TABLES    IN SCHEMA stg, ml          TO credit_risk_app;
-- GRANT SELECT           ON ALL TABLES IN SCHEMA rpt TO credit_risk_app;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA stg, ml
--       GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO credit_risk_app;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA rpt
--       GRANT SELECT ON TABLES TO credit_risk_app;

-- ============================================================
-- END OF create_database_pg.sql
-- ============================================================
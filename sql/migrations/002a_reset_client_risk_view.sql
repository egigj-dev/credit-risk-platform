-- The initial MVP view is intentionally replaced before the detailed contract is created.
-- Keeping this as a migration avoids PostgreSQL's positional view-column rename restriction.
DROP VIEW IF EXISTS rpt.vw_client_risk;

-- Read-only role for the frontend
-- Run with:
--   source .env && psql "$DATABASE_URL" -v frontend_reader_password="$FRONTEND_READER_PASSWORD" -f ingestion/scripts/setup_readonly_role.sql
-- Idempotent: safe to re-run

\set ON_ERROR_STOP 0
CREATE ROLE frontend_reader WITH LOGIN PASSWORD :'frontend_reader_password';
\set ON_ERROR_STOP 1

GRANT USAGE ON SCHEMA public TO frontend_reader;
GRANT SELECT ON latest_snapshot TO frontend_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO frontend_reader;

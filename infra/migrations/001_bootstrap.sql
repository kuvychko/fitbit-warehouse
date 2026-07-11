-- 001: tenant bootstrap — roles, schema, privileges.
-- Idempotent: guarded CREATE ROLE / CREATE SCHEMA via \gexec (plain
-- CREATE SCHEMA IF NOT EXISTS would need database CREATE privilege on a
-- shared cluster); ALTER statements are naturally re-runnable.
-- Runs as an admin user; passwords arrive as psql vars from run.sh.

\set ON_ERROR_STOP on

-- TimescaleDB (no-op with a NOTICE when the extension already exists)
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Roles ----------------------------------------------------------------------
SELECT format('CREATE ROLE %I LOGIN', r)
FROM (VALUES ('health_owner'), ('health_rw'), ('health_ro')) AS t(r)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = t.r)
\gexec

ALTER ROLE health_owner LOGIN PASSWORD :'owner_pw';
ALTER ROLE health_rw    LOGIN PASSWORD :'rw_pw';
ALTER ROLE health_ro    LOGIN PASSWORD :'ro_pw';

-- TimescaleDB functions live in public; keep it on every role's search_path.
ALTER ROLE health_owner SET search_path = health, public;
ALTER ROLE health_rw    SET search_path = health, public;
ALTER ROLE health_ro    SET search_path = health, public;

-- Schema ---------------------------------------------------------------------
SELECT 'CREATE SCHEMA health AUTHORIZATION health_owner'
WHERE NOT EXISTS (SELECT FROM pg_namespace WHERE nspname = 'health')
\gexec

GRANT USAGE ON SCHEMA health TO health_rw, health_ro;

-- Cover tables health_owner will create later (002+), plus any already there.
ALTER DEFAULT PRIVILEGES FOR ROLE health_owner IN SCHEMA health
  GRANT SELECT, INSERT, UPDATE ON TABLES TO health_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE health_owner IN SCHEMA health
  GRANT SELECT ON TABLES TO health_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE health_owner IN SCHEMA health
  GRANT USAGE, SELECT ON SEQUENCES TO health_rw;

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA health TO health_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA health TO health_ro;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA health TO health_rw;

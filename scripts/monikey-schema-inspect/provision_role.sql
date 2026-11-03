-- Create or rotate only the dedicated schema-inspection login.
-- Run as an explicitly approved database administrator through the
-- protected provisioning workflow. The password is supplied as a psql
-- variable over stdin; never pass it on the command line or log it.
\set ON_ERROR_STOP on
BEGIN;

DO $check_bootstrap$
BEGIN
  IF current_database() <> 'monikey_dev' THEN
    RAISE EXCEPTION 'unexpected database; refusing role provisioning';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles
    WHERE rolname = current_user AND rolsuper
  ) THEN
    RAISE EXCEPTION 'bootstrap connection is not a PostgreSQL superuser';
  END IF;
END
$check_bootstrap$;

DO $check_existing_role$
DECLARE
  inspector_oid oid;
BEGIN
  SELECT oid INTO inspector_oid
  FROM pg_catalog.pg_roles
  WHERE rolname = 'monikey_schema_inspector';

  IF inspector_oid IS NULL THEN
    RETURN;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles AS r
    WHERE r.oid = inspector_oid
      AND (
        r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
        OR r.rolbypassrls OR r.rolinherit
        OR pg_catalog.has_schema_privilege(r.oid, 'public', 'CREATE')
        OR pg_catalog.has_database_privilege(r.oid, 'monikey_dev', 'CREATE')
        OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_database AS d,
               LATERAL pg_catalog.aclexplode(
                 COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))
               ) AS acl
          WHERE d.datname = 'monikey_dev'
            AND acl.grantee = r.oid
            AND acl.privilege_type IN ('CREATE', 'TEMPORARY')
        )
        OR EXISTS (
          SELECT 1 FROM pg_catalog.pg_auth_members AS m
          WHERE m.member = r.oid OR m.roleid = r.oid
        )
        OR EXISTS (
          SELECT 1 FROM pg_catalog.pg_database AS d
          WHERE d.datname = 'monikey_dev' AND d.datdba = r.oid
        )
        OR EXISTS (
          SELECT 1 FROM pg_catalog.pg_namespace AS n
          WHERE n.nspname = 'public' AND n.nspowner = r.oid
        )
        OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_class AS c
          JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
          WHERE n.nspname = 'public' AND c.relowner = r.oid
        )
        OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_class AS c
          JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
          WHERE n.nspname = 'public'
            AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
            AND (
              pg_catalog.has_table_privilege(r.oid, c.oid, 'SELECT')
              OR pg_catalog.has_table_privilege(r.oid, c.oid, 'INSERT')
              OR pg_catalog.has_table_privilege(r.oid, c.oid, 'UPDATE')
              OR pg_catalog.has_table_privilege(r.oid, c.oid, 'DELETE')
              OR pg_catalog.has_table_privilege(r.oid, c.oid, 'TRUNCATE')
              OR pg_catalog.has_table_privilege(r.oid, c.oid, 'REFERENCES')
              OR pg_catalog.has_table_privilege(r.oid, c.oid, 'TRIGGER')
            )
        )
        OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_class AS s
          JOIN pg_catalog.pg_namespace AS n ON n.oid = s.relnamespace
          WHERE n.nspname = 'public' AND s.relkind = 'S'
            AND (
              pg_catalog.has_sequence_privilege(r.oid, s.oid, 'USAGE')
              OR pg_catalog.has_sequence_privilege(r.oid, s.oid, 'SELECT')
              OR pg_catalog.has_sequence_privilege(r.oid, s.oid, 'UPDATE')
            )
        )
      )
  ) THEN
    RAISE EXCEPTION 'existing inspection role has unexpected privileges or ownership; no changes made';
  END IF;
END
$check_existing_role$;

SELECT EXISTS (
  SELECT 1 FROM pg_catalog.pg_roles
  WHERE rolname = 'monikey_schema_inspector'
) AS inspector_exists
\gset

\if :inspector_exists
  ALTER ROLE monikey_schema_inspector
    LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 2 PASSWORD :'inspector_password';
\else
  CREATE ROLE monikey_schema_inspector
    LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 2 PASSWORD :'inspector_password';
\endif

GRANT CONNECT ON DATABASE monikey_dev TO monikey_schema_inspector;
ALTER ROLE monikey_schema_inspector IN DATABASE monikey_dev
  SET default_transaction_read_only = on;
ALTER ROLE monikey_schema_inspector IN DATABASE monikey_dev
  SET statement_timeout = '15s';
ALTER ROLE monikey_schema_inspector IN DATABASE monikey_dev
  SET idle_in_transaction_session_timeout = '30s';

-- Intentionally no table, sequence, schema, TEMPORARY, or administrative grants.
COMMIT;

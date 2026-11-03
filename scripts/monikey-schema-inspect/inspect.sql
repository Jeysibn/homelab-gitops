-- Catalog-only inventory for the fixed MoniKey schema.
-- No application table rows are selected. Call through psql with ON_ERROR_STOP.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '15s';

SELECT jsonb_build_object(
  'database', current_database(),
  'server_version', current_setting('server_version'),
  'schema', 'public',
  'relation_kinds', COALESCE((
    SELECT jsonb_object_agg(c.relname, c.relkind::text)
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname IN (
      'accounts', 'categories', 'transactions', 'credit_card_payments',
      'account_transfers', 'kakeibo_month_plans', 'kakeibo_weekly_reviews'
    )
  ), '{}'::jsonb),
  'role_security', (
    SELECT jsonb_build_object(
      'role', r.rolname,
      'superuser', r.rolsuper,
      'create_database', r.rolcreatedb,
      'create_role', r.rolcreaterole,
      'replication', r.rolreplication,
      'inherit', r.rolinherit,
      'schema_create', pg_catalog.has_schema_privilege(r.rolname, 'public', 'CREATE'),
      'database_create', pg_catalog.has_database_privilege(r.rolname, current_database(), 'CREATE'),
      'database_temp', pg_catalog.has_database_privilege(r.rolname, current_database(), 'TEMP'),
      'owns_database', EXISTS (
        SELECT 1 FROM pg_catalog.pg_database d
        WHERE d.datname = current_database() AND d.datdba = r.oid
      ),
      'owns_schema', EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace ns
        WHERE ns.nspname = 'public' AND ns.nspowner = r.oid
      ),
      'has_role_memberships', EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid
      ),
      'owns_target_objects', EXISTS (
        SELECT 1 FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND (
          c.relname IN ('accounts', 'categories', 'transactions', 'credit_card_payments',
            'account_transfers', 'kakeibo_month_plans', 'kakeibo_weekly_reviews')
          OR c.relkind = 'S'
        ) AND c.relowner = r.oid
      ),
      'table_privileges', (
        SELECT jsonb_object_agg(t.name, jsonb_build_object(
          'select', pg_catalog.has_table_privilege(r.rolname, format('public.%I', t.name), 'SELECT'),
          'insert', pg_catalog.has_table_privilege(r.rolname, format('public.%I', t.name), 'INSERT'),
          'update', pg_catalog.has_table_privilege(r.rolname, format('public.%I', t.name), 'UPDATE'),
          'delete', pg_catalog.has_table_privilege(r.rolname, format('public.%I', t.name), 'DELETE'),
          'truncate', pg_catalog.has_table_privilege(r.rolname, format('public.%I', t.name), 'TRUNCATE'),
          'references', pg_catalog.has_table_privilege(r.rolname, format('public.%I', t.name), 'REFERENCES'),
          'trigger', pg_catalog.has_table_privilege(r.rolname, format('public.%I', t.name), 'TRIGGER')
        ))
        FROM unnest(ARRAY[
          'accounts', 'categories', 'transactions', 'credit_card_payments',
          'account_transfers', 'kakeibo_month_plans', 'kakeibo_weekly_reviews'
        ]) AS t(name)
        JOIN pg_catalog.pg_class target ON target.relname = t.name
        JOIN pg_catalog.pg_namespace target_ns ON target_ns.oid = target.relnamespace
        WHERE target_ns.nspname = 'public' AND target.relkind IN ('r', 'p', 'v', 'm', 'f')
      ),
      'sequence_privileges', (
        SELECT COALESCE(bool_or(
          pg_catalog.has_sequence_privilege(r.rolname, s.oid, 'USAGE') OR
          pg_catalog.has_sequence_privilege(r.rolname, s.oid, 'SELECT') OR
          pg_catalog.has_sequence_privilege(r.rolname, s.oid, 'UPDATE')
        ), false)
        FROM pg_catalog.pg_class s
        JOIN pg_catalog.pg_namespace ns ON ns.oid = s.relnamespace
        WHERE ns.nspname = 'public' AND s.relkind = 'S'
      )
    )
    FROM pg_catalog.pg_roles r WHERE r.rolname = current_user
  ),
  'tables', COALESCE((
    SELECT jsonb_object_agg(t.relname, jsonb_build_object(
      'columns', COALESCE((
        SELECT jsonb_object_agg(a.attname, jsonb_build_object(
          'type', pg_catalog.format_type(a.atttypid, a.atttypmod),
          'nullable', NOT a.attnotnull
        ) ORDER BY a.attnum)
        FROM pg_catalog.pg_attribute a
        WHERE a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped
      ), '{}'::jsonb),
      'constraints', COALESCE((
        SELECT jsonb_object_agg(con.conname, jsonb_build_object(
          'type', con.contype::text,
          'definition', pg_catalog.pg_get_constraintdef(con.oid, true)
        ) ORDER BY con.conname)
        FROM pg_catalog.pg_constraint con WHERE con.conrelid = t.oid
      ), '{}'::jsonb),
      'indexes', COALESCE((
        SELECT jsonb_object_agg(ix.relname, pg_catalog.pg_get_indexdef(ix.oid))
        FROM pg_catalog.pg_index i
        JOIN pg_catalog.pg_class ix ON ix.oid = i.indexrelid
        WHERE i.indrelid = t.oid
      ), '{}'::jsonb),
      'comments', COALESCE((
        SELECT jsonb_object_agg(a.attname, pg_catalog.col_description(t.oid, a.attnum))
        FROM pg_catalog.pg_attribute a
        WHERE a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped
          AND pg_catalog.col_description(t.oid, a.attnum) IS NOT NULL
      ), '{}'::jsonb)
    ))
    FROM pg_catalog.pg_class t
    JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
    WHERE n.nspname = 'public' AND t.relkind IN ('r', 'p')
      AND t.relname IN ('accounts', 'categories', 'transactions',
        'account_transfers', 'kakeibo_month_plans', 'kakeibo_weekly_reviews')
  ), '{}'::jsonb)
)::text;

COMMIT;

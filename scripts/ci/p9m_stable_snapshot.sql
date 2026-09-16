\set ON_ERROR_STOP on
\pset tuples_only on
\pset format unaligned

-- Stable business-data fingerprints for the deterministic P9-M predecessor.
SELECT 'organizations|'
       || count(*)::text || '|'
       || md5(COALESCE(string_agg(
            jsonb_build_array(
                id::text, name, slug, tier, is_active, max_branches,
                default_currency_code, website_verified, social_links,
                verification_status, country, profile_completed
            )::text,
            E'\n' ORDER BY id
          ), ''))
FROM public.organizations
WHERE slug LIKE 'p9m-org-%';

SELECT 'branches|'
       || count(*)::text || '|'
       || md5(COALESCE(string_agg(
            jsonb_build_array(
                id::text, org_id::text, branch_name, branch_code,
                internal_slug::text, search_normalized_name, timezone,
                currency_code, region_code, country_code, branch_metadata
            )::text,
            E'\n' ORDER BY id
          ), ''))
FROM public.org_branches
WHERE internal_slug::text LIKE 'p9m-%';

SELECT 'branch_outbox|'
       || count(*)::text || '|'
       || md5(COALESCE(string_agg(
            jsonb_build_array(
                outbox_id::text, tenant_id::text, branch_id::text,
                event_type, payload, created_at::text, process_after::text,
                status, attempt_count, max_attempts,
                last_attempted_at::text, last_error,
                correlation_id::text, leased_by::text, leased_until::text,
                lease_fence
            )::text,
            E'\n' ORDER BY outbox_id
          ), ''))
FROM public.branch_outbox_events
WHERE payload->>'p9m_synthetic' = 'true';

SELECT 'outbox_status_distribution|'
       || md5(COALESCE(string_agg(
            jsonb_build_array(event_type, status, row_count)::text,
            E'\n' ORDER BY event_type, status
          ), ''))
FROM (
    SELECT event_type, status, count(*) AS row_count
    FROM public.branch_outbox_events
    WHERE payload->>'p9m_synthetic' = 'true'
    GROUP BY event_type, status
) AS distribution;

SELECT 'outbox_table_acl|'
       || md5(COALESCE(string_agg(
            jsonb_build_array(grantor, grantee, privilege_type, is_grantable)::text,
            E'\n' ORDER BY grantor, grantee, privilege_type, is_grantable
          ), ''))
FROM information_schema.table_privileges
WHERE table_schema = 'public'
  AND table_name = 'branch_outbox_events';

SELECT 'outbox_column_acl|'
       || md5(COALESCE(string_agg(
            jsonb_build_array(
                grantor, grantee, column_name, privilege_type, is_grantable
            )::text,
            E'\n' ORDER BY grantor, grantee, column_name, privilege_type, is_grantable
          ), ''))
FROM information_schema.column_privileges
WHERE table_schema = 'public'
  AND table_name = 'branch_outbox_events';

SELECT 'critical_relation_security|'
       || md5(COALESCE(string_agg(
            jsonb_build_array(
                c.relname,
                pg_catalog.pg_get_userbyid(c.relowner),
                c.relrowsecurity,
                c.relforcerowsecurity
            )::text,
            E'\n' ORDER BY c.relname
          ), ''))
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname IN ('organizations', 'org_branches', 'branch_outbox_events');

SELECT 'critical_role_posture|'
       || md5(COALESCE(string_agg(
            jsonb_build_array(
                rolname, rolcanlogin, rolsuper, rolinherit,
                rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
            )::text,
            E'\n' ORDER BY rolname
          ), ''))
FROM pg_catalog.pg_roles
WHERE rolname IN (
    'migration_owner',
    'app_security_owner',
    'lifecycle_maintenance_runtime',
    'app_runtime',
    'auth_runtime',
    'worker_runtime',
    'finance_config_runtime'
);

SELECT 'app_security_owner_raw_outbox_select|'
       || pg_catalog.has_table_privilege(
            'app_security_owner', 'public.branch_outbox_events', 'SELECT'
          )::text;

SELECT 'lifecycle_maintenance_raw_outbox_select|'
       || pg_catalog.has_table_privilege(
            'lifecycle_maintenance_runtime', 'public.branch_outbox_events', 'SELECT'
          )::text;

SELECT 'dead_lifecycle_count|'
       || count(*)::text
FROM public.branch_outbox_events
WHERE payload->>'p9m_synthetic' = 'true'
  AND event_type = 'branch.lifecycle_saga'
  AND status = 'dead_lettered';

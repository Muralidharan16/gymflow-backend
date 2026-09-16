\set ON_ERROR_STOP on

DO $$
DECLARE
    fn_oid oid;
    fn_owner text;
    fn_security_definer boolean;
    fn_volatility "char";
    fn_config text[];
    dead_count bigint;
BEGIN
    SELECT p.oid,
           owner.rolname,
           p.prosecdef,
           p.provolatile,
           p.proconfig
    INTO fn_oid, fn_owner, fn_security_definer, fn_volatility, fn_config
    FROM pg_catalog.pg_proc AS p
    JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
    JOIN pg_catalog.pg_roles AS owner ON owner.oid = p.proowner
    WHERE n.nspname = 'app_secure'
      AND p.proname = 'lifecycle_saga_dead_letter_count'
      AND p.pronargs = 0;

    IF fn_oid IS NULL THEN
        RAISE EXCEPTION 'P9-M expected zk07 function is absent';
    END IF;
    IF fn_owner <> 'app_security_owner' THEN
        RAISE EXCEPTION 'P9-M expected app_security_owner, got %', fn_owner;
    END IF;
    IF NOT fn_security_definer THEN
        RAISE EXCEPTION 'P9-M zk07 function lost SECURITY DEFINER';
    END IF;
    IF fn_volatility <> 's' THEN
        RAISE EXCEPTION 'P9-M zk07 function is not STABLE';
    END IF;
    IF NOT ('row_security=on' = ANY(COALESCE(fn_config, ARRAY[]::text[]))) THEN
        RAISE EXCEPTION 'P9-M zk07 function lost row_security=on';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM unnest(COALESCE(fn_config, ARRAY[]::text[])) AS setting
        WHERE setting LIKE 'search_path=%'
    ) THEN
        RAISE EXCEPTION 'P9-M zk07 function lost hardened search_path';
    END IF;

    IF NOT pg_catalog.has_function_privilege(
        'lifecycle_maintenance_runtime', fn_oid, 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'P9-M lifecycle maintenance EXECUTE missing';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS p
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
        ) AS acl
        WHERE p.oid = fn_oid
          AND acl.grantee = 0
          AND acl.privilege_type = 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'P9-M unexpected PUBLIC EXECUTE';
    END IF;

    IF pg_catalog.has_function_privilege('app_runtime', fn_oid, 'EXECUTE')
       OR pg_catalog.has_function_privilege('auth_runtime', fn_oid, 'EXECUTE')
       OR pg_catalog.has_function_privilege('worker_runtime', fn_oid, 'EXECUTE')
       OR pg_catalog.has_function_privilege('finance_config_runtime', fn_oid, 'EXECUTE') THEN
        RAISE EXCEPTION 'P9-M zk07 function leaked EXECUTE to a blocked runtime role';
    END IF;

    IF pg_catalog.has_table_privilege(
        'lifecycle_maintenance_runtime', 'public.branch_outbox_events', 'SELECT'
    ) THEN
        RAISE EXCEPTION 'P9-M lifecycle maintenance gained raw outbox SELECT';
    END IF;

    SELECT count(*) INTO dead_count
    FROM public.branch_outbox_events
    WHERE payload->>'p9m_synthetic' = 'true'
      AND event_type = 'branch.lifecycle_saga'
      AND status = 'dead_lettered';

    IF dead_count <> 204 THEN
        RAISE EXCEPTION 'P9-M expected 204 durable dead lifecycle rows, got %', dead_count;
    END IF;
END
$$;

SET ROLE lifecycle_maintenance_runtime;

DO $$
BEGIN
    BEGIN
        PERFORM 1 FROM public.branch_outbox_events LIMIT 1;
        RAISE EXCEPTION 'P9-M lifecycle maintenance unexpectedly read raw outbox rows';
    EXCEPTION
        WHEN insufficient_privilege THEN
            NULL;
    END;
END
$$;

DO $$
DECLARE
    observed bigint;
BEGIN
    SELECT app_secure.lifecycle_saga_dead_letter_count() INTO observed;
    IF observed <> 204 THEN
        RAISE EXCEPTION 'P9-M aggregate returned %, expected 204', observed;
    END IF;
END
$$;

RESET ROLE;

SELECT 'P9M_EXPECTED_CAPABILITY_DELTA=PASS';

\set ON_ERROR_STOP on

-- P9-M uses synthetic deterministic data only. This file must never ingest a
-- production/customer dump or depend on external services.

INSERT INTO public.organizations (
    id,
    name,
    slug,
    tier,
    is_active,
    max_branches,
    default_currency_code,
    website_verified,
    social_links,
    verification_status,
    country,
    profile_completed
)
SELECT
    md5('p9m-org-' || g::text)::uuid,
    format('P9-M Synthetic Org %s', g),
    format('p9m-org-%s', g),
    'basic',
    TRUE,
    10,
    'INR',
    FALSE,
    '{}'::jsonb,
    'pending',
    'India',
    TRUE
FROM generate_series(1, 64) AS g;

-- The certified branch-limit trigger intentionally requires trusted tenant
-- context even when a fixture is seeded under migration/infrastructure
-- authority. Seed each tenant separately through that invariant rather than
-- disabling the trigger or broadening any database privilege.
DO $$
DECLARE
    org_sequence integer;
    branch_sequence integer;
    org_id uuid;
BEGIN
    FOR org_sequence IN 1..64 LOOP
        org_id := md5('p9m-org-' || org_sequence::text)::uuid;
        PERFORM pg_catalog.set_config('app.current_org_id', org_id::text, true);

        FOR branch_sequence IN 1..3 LOOP
            INSERT INTO public.org_branches (
                id,
                org_id,
                branch_name,
                branch_code,
                internal_slug,
                timezone,
                currency_code,
                region_code,
                country_code,
                branch_metadata
            ) VALUES (
                md5(format('p9m-branch-%s-%s', org_sequence, branch_sequence))::uuid,
                org_id,
                format('P9-M Synthetic Branch %s-%s', org_sequence, branch_sequence),
                format('P9M-%s-%s', lpad(org_sequence::text, 3, '0'), branch_sequence),
                format('p9m-%s-%s', org_sequence, branch_sequence),
                'Asia/Kolkata',
                'INR',
                'TN',
                'IN',
                jsonb_build_object(
                    'p9m_synthetic', TRUE,
                    'org_sequence', org_sequence,
                    'branch_sequence', branch_sequence
                )
            );
        END LOOP;
    END LOOP;

    PERFORM pg_catalog.set_config('app.current_org_id', '', true);
END
$$;

WITH source AS (
    SELECT
        g,
        ((g - 1) % 64) + 1 AS org_sequence,
        ((g - 1) % 3) + 1 AS branch_sequence
    FROM generate_series(1, 4096) AS g
)
INSERT INTO public.branch_outbox_events (
    outbox_id,
    tenant_id,
    branch_id,
    event_type,
    payload,
    created_at,
    process_after,
    status,
    attempt_count,
    max_attempts,
    last_attempted_at,
    last_error,
    correlation_id,
    leased_by,
    leased_until,
    lease_fence
)
SELECT
    md5('p9m-outbox-' || g::text)::uuid,
    md5('p9m-org-' || org_sequence::text)::uuid,
    md5(format('p9m-branch-%s-%s', org_sequence, branch_sequence))::uuid,
    CASE (g % 4)
        WHEN 0 THEN 'branch.lifecycle_saga'
        WHEN 1 THEN 'branch.search_sync'
        WHEN 2 THEN 'branch.notification'
        ELSE 'branch.audit'
    END,
    jsonb_build_object(
        'p9m_synthetic', TRUE,
        'sequence', g,
        'org_sequence', org_sequence,
        'branch_sequence', branch_sequence
    ),
    '2026-09-01 00:00:00+00'::timestamptz + (g::text || ' seconds')::interval,
    '2026-09-01 00:05:00+00'::timestamptz + (g::text || ' seconds')::interval,
    CASE
        WHEN g % 10 = 0 THEN 'dead_lettered'
        WHEN g % 7 = 0 THEN 'delivered'
        ELSE 'pending'
    END,
    CASE
        WHEN g % 10 = 0 THEN 5
        WHEN g % 7 = 0 THEN 1
        ELSE 0
    END,
    5,
    CASE
        WHEN g % 10 = 0 OR g % 7 = 0
            THEN '2026-09-01 00:10:00+00'::timestamptz + (g::text || ' seconds')::interval
        ELSE NULL
    END,
    CASE
        WHEN g % 10 = 0 THEN 'p9m_synthetic_terminal_failure'
        ELSE NULL
    END,
    md5('p9m-correlation-' || g::text)::uuid,
    NULL,
    NULL,
    0
FROM source;

DO $$
DECLARE
    org_count bigint;
    branch_count bigint;
    outbox_count bigint;
    dead_lifecycle_count bigint;
BEGIN
    SELECT count(*) INTO org_count
    FROM public.organizations
    WHERE slug LIKE 'p9m-org-%';

    SELECT count(*) INTO branch_count
    FROM public.org_branches
    WHERE internal_slug::text LIKE 'p9m-%';

    SELECT count(*) INTO outbox_count
    FROM public.branch_outbox_events
    WHERE payload->>'p9m_synthetic' = 'true';

    SELECT count(*) INTO dead_lifecycle_count
    FROM public.branch_outbox_events
    WHERE payload->>'p9m_synthetic' = 'true'
      AND event_type = 'branch.lifecycle_saga'
      AND status = 'dead_lettered';

    IF org_count <> 64 THEN
        RAISE EXCEPTION 'P9-M expected 64 synthetic organizations, got %', org_count;
    END IF;
    IF branch_count <> 192 THEN
        RAISE EXCEPTION 'P9-M expected 192 synthetic branches, got %', branch_count;
    END IF;
    IF outbox_count <> 4096 THEN
        RAISE EXCEPTION 'P9-M expected 4096 synthetic outbox rows, got %', outbox_count;
    END IF;
    IF dead_lifecycle_count <> 204 THEN
        RAISE EXCEPTION 'P9-M expected 204 dead lifecycle rows, got %', dead_lifecycle_count;
    END IF;
END
$$;

SELECT 'P9M_SYNTHETIC_PRODUCTION_SHAPE=PASS';

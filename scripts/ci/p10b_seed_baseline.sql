\set ON_ERROR_STOP on

-- P10-B representative performance calibration data.
-- Synthetic deterministic data only. Never replace this with production/customer data.

DO $$
DECLARE
    org_sequence integer;
    member_sequence integer;
    v_org_id uuid;
    owner_id uuid;
    branch_id uuid;
    phone_value text;
BEGIN
    FOR org_sequence IN 1..8 LOOP
        v_org_id := md5('p9m-org-' || org_sequence::text)::uuid;
        owner_id := md5('p10b-owner-' || org_sequence::text)::uuid;

        INSERT INTO public.owners (
            id,
            org_id,
            owner_name,
            email,
            hashed_password,
            email_verified,
            onboarding_completed
        ) VALUES (
            owner_id,
            v_org_id,
            format('P10-B Synthetic Owner %s', org_sequence),
            format('p10b-owner-%s@example.com', org_sequence),
            'p10b-synthetic-no-login',
            TRUE,
            TRUE
        );

        FOR member_sequence IN 1..500 LOOP
            branch_id := md5(
                format(
                    'p9m-branch-%s-%s',
                    org_sequence,
                    ((member_sequence - 1) % 3) + 1
                )
            )::uuid;
            phone_value := '8' || lpad(
                (org_sequence * 100000 + member_sequence)::text,
                9,
                '0'
            );

            INSERT INTO public.members (
                id,
                org_id,
                home_branch_id,
                member_uid,
                member_number,
                name,
                phone,
                email,
                date_of_birth,
                emergency_contact_name,
                emergency_contact_phone,
                status,
                source,
                is_active,
                is_migrated,
                qr_token,
                created_by,
                updated_by
            ) VALUES (
                md5(format('p10b-member-%s-%s', org_sequence, member_sequence))::uuid,
                v_org_id,
                branch_id,
                format('p10b%02s%04s', org_sequence, member_sequence),
                99 + member_sequence,
                format('P10-B Member %s-%s', org_sequence, member_sequence),
                phone_value,
                format('p10b-member-%s-%s@example.com', org_sequence, member_sequence),
                DATE '1990-01-01' + ((member_sequence % 365) * INTERVAL '1 day'),
                '9000000001',
                '9000000002',
                'active',
                'p10b_synthetic',
                TRUE,
                FALSE,
                substr(md5(format('p10b-qr-%s-%s', org_sequence, member_sequence)), 1, 16),
                owner_id,
                owner_id
            );
        END LOOP;

        INSERT INTO public.organization_counters (
            id,
            org_id,
            counter_key,
            current_value
        ) VALUES (
            md5('p10b-member-counter-' || org_sequence::text)::uuid,
            v_org_id,
            'member',
            599
        )
        ON CONFLICT (org_id, counter_key)
        DO UPDATE SET current_value = EXCLUDED.current_value, updated_at = now();
    END LOOP;
END
$$;

DO $$
DECLARE
    owner_count bigint;
    member_count bigint;
    counter_count bigint;
BEGIN
    SELECT count(*) INTO owner_count
    FROM public.owners
    WHERE email LIKE 'p10b-owner-%@example.com';

    SELECT count(*) INTO member_count
    FROM public.members
    WHERE source = 'p10b_synthetic';

    SELECT count(*) INTO counter_count
    FROM public.organization_counters
    WHERE counter_key = 'member'
      AND org_id IN (
        SELECT md5('p9m-org-' || g::text)::uuid
        FROM generate_series(1, 8) AS g
      )
      AND current_value = 599;

    IF owner_count <> 8 THEN
        RAISE EXCEPTION 'P10-B expected 8 owners, got %', owner_count;
    END IF;
    IF member_count <> 4000 THEN
        RAISE EXCEPTION 'P10-B expected 4000 members, got %', member_count;
    END IF;
    IF counter_count <> 8 THEN
        RAISE EXCEPTION 'P10-B expected 8 synchronized member counters, got %', counter_count;
    END IF;
END
$$;

SELECT 'P10B_SYNTHETIC_BASELINE_SEED=PASS';
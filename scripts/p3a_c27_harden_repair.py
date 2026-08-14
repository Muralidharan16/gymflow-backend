from pathlib import Path

MIGRATION = Path("alembic/versions/c27d8e9f0a1c_organization_onboarding_authorization.py")
TEST = Path("tests/test_organization_onboarding_authorization_boundary.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


migration = MIGRATION.read_text()
old_validation = '''    IF p_patch->'address_line2' <> 'null'::jsonb
       AND pg_catalog.jsonb_typeof(p_patch->'address_line2') <> 'string' THEN
        RAISE EXCEPTION 'organization onboarding address_line2 is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch ? 'social_links' AND (
        p_patch->'social_links' = 'null'::jsonb
        OR pg_catalog.jsonb_typeof(p_patch->'social_links') <> 'object'
    ) THEN
        RAISE EXCEPTION 'organization onboarding social_links must be an object'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch ? 'year_established'
       AND p_patch->'year_established' <> 'null'::jsonb
       AND pg_catalog.jsonb_typeof(p_patch->'year_established') <> 'number' THEN
        RAISE EXCEPTION 'organization onboarding year_established is invalid'
            USING ERRCODE = '22023';
    END IF;
'''
new_validation = '''    IF p_patch->'address_line2' <> 'null'::jsonb
       AND pg_catalog.jsonb_typeof(p_patch->'address_line2') <> 'string' THEN
        RAISE EXCEPTION 'organization onboarding address_line2 is invalid'
            USING ERRCODE = '22023';
    END IF;

    IF (p_patch->>'phone') !~ '^(\\+91)?[6-9][0-9]{9}$' THEN
        RAISE EXCEPTION 'organization onboarding phone is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF pg_catalog.char_length(p_patch->>'address_line1') < 3 THEN
        RAISE EXCEPTION 'organization onboarding address_line1 is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF (p_patch->>'pincode') !~ '^[1-9][0-9]{5}$' THEN
        RAISE EXCEPTION 'organization onboarding pincode is invalid'
            USING ERRCODE = '22023';
    END IF;

    IF p_patch ? 'tagline'
       AND p_patch->'tagline' <> 'null'::jsonb
       AND (
            pg_catalog.jsonb_typeof(p_patch->'tagline') <> 'string'
            OR pg_catalog.char_length(p_patch->>'tagline') > 150
       ) THEN
        RAISE EXCEPTION 'organization onboarding tagline is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch ? 'description'
       AND p_patch->'description' <> 'null'::jsonb
       AND pg_catalog.jsonb_typeof(p_patch->'description') <> 'string' THEN
        RAISE EXCEPTION 'organization onboarding description is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch ? 'website_url'
       AND p_patch->'website_url' <> 'null'::jsonb
       AND pg_catalog.jsonb_typeof(p_patch->'website_url') <> 'string' THEN
        RAISE EXCEPTION 'organization onboarding website_url is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch ? 'social_links' AND (
        p_patch->'social_links' = 'null'::jsonb
        OR pg_catalog.jsonb_typeof(p_patch->'social_links') <> 'object'
    ) THEN
        RAISE EXCEPTION 'organization onboarding social_links must be an object'
            USING ERRCODE = '22023';
    END IF;
    IF p_patch ? 'year_established'
       AND p_patch->'year_established' <> 'null'::jsonb THEN
        IF pg_catalog.jsonb_typeof(p_patch->'year_established') <> 'number'
           OR (p_patch->>'year_established')::numeric
                <> pg_catalog.trunc((p_patch->>'year_established')::numeric)
           OR (p_patch->>'year_established')::numeric < 1800
           OR (p_patch->>'year_established')::numeric
                > pg_catalog.date_part('year', CURRENT_DATE) THEN
            RAISE EXCEPTION 'organization onboarding year_established is invalid'
                USING ERRCODE = '22023';
        END IF;
    END IF;
'''
migration = replace_once(migration, old_validation, new_validation, "validation block")

old_tokens = '''        "organization.id = v_org_id",
        "unknown organization onboarding fields",
    ):
'''
new_tokens = '''        "organization.id = v_org_id",
        "unknown organization onboarding fields",
        "organization onboarding phone is invalid",
        "organization onboarding address_line1 is invalid",
        "organization onboarding pincode is invalid",
        "organization onboarding tagline is invalid",
        "pg_catalog.trunc",
        "pg_catalog.date_part",
    ):
'''
migration = replace_once(migration, old_tokens, new_tokens, "source contract tokens")
MIGRATION.write_text(migration)

test = TEST.read_text()
anchor = "def test_security_owner_receives_only_onboarding_specific_extra_update_authority() -> None:\n"
addition = r'''def test_auth_onboarding_capability_mirrors_application_input_invariants() -> None:
    source = _source(MIGRATION)

    for token in (
        "organization onboarding phone is invalid",
        "'^(\\+91)?[6-9][0-9]{9}$'",
        "organization onboarding address_line1 is invalid",
        "pg_catalog.char_length(p_patch->>'address_line1') < 3",
        "organization onboarding pincode is invalid",
        "'^[1-9][0-9]{5}$'",
        "organization onboarding tagline is invalid",
        "pg_catalog.char_length(p_patch->>'tagline') > 150",
        "organization onboarding description is invalid",
        "organization onboarding website_url is invalid",
        "pg_catalog.trunc((p_patch->>'year_established')::numeric)",
        "(p_patch->>'year_established')::numeric < 1800",
        "pg_catalog.date_part('year', CURRENT_DATE)",
    ):
        assert token in source


'''
if test.count(anchor) != 1:
    raise SystemExit("static guard insertion point drift")
test = test.replace(anchor, addition + anchor, 1)
TEST.write_text(test)

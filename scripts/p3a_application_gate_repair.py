from pathlib import Path

ROUTER = Path("app/routers/organizations.py")
STATIC = Path("tests/test_organization_profile_authorization_boundary.py")
SQL_GATE = Path(".github/workflows/p3a-organization-profile-boundary.yml")
APP_GATE = Path(".github/workflows/p3a-organization-profile-application.yml")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)


# Router: translate only the DB authorization SQLSTATE-domain exception to 403.
router = ROUTER.read_text()
router = replace_once(
    router,
    '''from app.repositories.organization_profile import (
    get_current_organization_profile,
    update_current_organization_profile,
)
''',
    '''from app.repositories.organization_profile import (
    OrganizationProfileAuthorizationError,
    get_current_organization_profile,
    update_current_organization_profile,
)
''',
    "router import",
)
insert_at = "\n\n@router.post(\"/registrations\", response_model=Response[RegistrationResponse])\n"
helpers = '''

async def _get_profile_or_forbidden(db: AsyncSession) -> dict | None:
    try:
        return await get_current_organization_profile(db)
    except OrganizationProfileAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization profile access denied",
        ) from exc


async def _update_profile_or_forbidden(
    db: AsyncSession,
    patch: dict,
) -> dict | None:
    try:
        return await update_current_organization_profile(db, patch)
    except OrganizationProfileAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization profile access denied",
        ) from exc
'''
if router.count(insert_at) != 1:
    raise SystemExit("router helper insertion point drift")
router = router.replace(insert_at, helpers + insert_at, 1)
router = router.replace("org = await get_current_organization_profile(db)", "org = await _get_profile_or_forbidden(db)")
router = router.replace(
    "org = await update_current_organization_profile(db, update_data)",
    "org = await _update_profile_or_forbidden(db, update_data)",
)
if "await get_current_organization_profile(db)" not in helpers:
    raise SystemExit("helper construction error")
ROUTER.write_text(router)

# Static guard: 42501 mapping must be narrow and sanitized.
static = STATIC.read_text()
anchor = "def test_profile_route_never_loads_or_refreshes_the_organization_orm_row() -> None:\n"
addition = '''def test_profile_authorization_denial_is_sanitized_to_http_403() -> None:
    repository = _source(REPOSITORY)
    router = _source(ROUTER)

    assert "class OrganizationProfileAuthorizationError(PermissionError)" in repository
    assert 'if _sqlstate(exc) == "42501"' in repository
    assert "except DBAPIError as exc" in repository
    assert "OrganizationProfileAuthorizationError" in router
    assert "status.HTTP_403_FORBIDDEN" in router
    assert 'detail="Organization profile access denied"' in router
    assert "str(exc)" not in router


'''
if static.count(anchor) != 1:
    raise SystemExit("static 403 insertion point drift")
static = static.replace(anchor, addition + anchor, 1)
STATIC.write_text(static)

# SQL gate: direct auth capability callers cannot bypass onboarding schema invariants.
sql_gate = SQL_GATE.read_text()
section_start = sql_gate.index("      - name: Prove auth onboarding is bounded and direct auth UPDATE is denied\n")
section_end = sql_gate.index("      - name: Verify durable state and protected columns\n", section_start)
section = sql_gate[section_start:section_end]
marker = '''          printf '%s\\n' "$result" | grep -qx 't'

          if PGPASSWORD='ci-auth-runtime' psql -X -v ON_ERROR_STOP=1 \\
'''
if section.count(marker) != 1:
    raise SystemExit("auth negative runtime insertion marker drift")
negative = '''          printf '%s\\n' "$result" | grep -qx 't'

          for invalid_patch in \\
            '{"phone":"123","address_line1":"1 Alpha Street","address_line2":null,"city":"Puducherry","state":"Puducherry","pincode":"605001"}' \\
            '{"phone":"+919876543210","address_line1":"x","address_line2":null,"city":"Puducherry","state":"Puducherry","pincode":"605001"}' \\
            '{"phone":"+919876543210","address_line1":"1 Alpha Street","address_line2":null,"city":"Puducherry","state":"Puducherry","pincode":"012345"}' \\
            '{"phone":"+919876543210","address_line1":"1 Alpha Street","address_line2":null,"city":"Puducherry","state":"Puducherry","pincode":"605001","year_established":1799}' \\
            '{"phone":"+919876543210","address_line1":"1 Alpha Street","address_line2":null,"city":"Puducherry","state":"Puducherry","pincode":"605001","year_established":2000.5}' \\
            '{"phone":"+919876543210","address_line1":"1 Alpha Street","address_line2":null,"city":"Puducherry","state":"Puducherry","pincode":"605001","year_established":9999}'
          do
            if PGPASSWORD='ci-auth-runtime' psql -X -v ON_ERROR_STOP=1 \\
                -h 127.0.0.1 -U auth_p3a_runtime -d gymflow_p3a_test \\
                -v invalid_patch="$invalid_patch" <<'SQL'; then
          BEGIN;
          SELECT pg_catalog.set_config('app.current_org_id','11111111-1111-4111-8111-111111111111',true);
          SELECT pg_catalog.set_config('app.current_user_id','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',true);
          SELECT pg_catalog.set_config('app.current_principal_type','owner',true);
          SELECT pg_catalog.set_config('app.current_role','owner',true);
          SELECT app_secure.complete_current_organization_onboarding_profile(
              CAST(:'invalid_patch' AS jsonb)
          );
          COMMIT;
          SQL
              echo "auth onboarding capability accepted invalid patch: $invalid_patch" >&2
              exit 1
            fi
          done

          if PGPASSWORD='ci-auth-runtime' psql -X -v ON_ERROR_STOP=1 \\
'''
section = section.replace(marker, negative, 1)
sql_gate = sql_gate[:section_start] + section + sql_gate[section_end:]
SQL_GATE.write_text(sql_gate)

# Application gate: certify P3A through actual FastAPI middleware/get_db/repository,
# while explicitly preserving P3B's ungranted registration surface.
app_gate = APP_GATE.read_text()
step_start = app_gate.index("      - name: Exercise FastAPI profile GET and PATCH through the real API login\n")
step_end = app_gate.index("      - name: Verify application path persisted only allowed Alpha state\n", step_start)
replacement = '''      - name: Prove P3B registration access is not broadened by P3A
        run: |
          set -euo pipefail
          observed=$(PGPASSWORD='ci-migration-owner' psql -X -A -t -v ON_ERROR_STOP=1 \\
            -h 127.0.0.1 -U migration_owner -d gymflow_p3a_app_test \\
            -c "SELECT pg_catalog.has_table_privilege('app_p3a_runtime','public.organization_registrations','SELECT')")
          test "$observed" = 'f'

      - name: Exercise P3A FastAPI boundary through the real API login
        env:
          DATABASE_URL: postgresql+asyncpg://app_p3a_runtime:ci-api-runtime@127.0.0.1:5432/gymflow_p3a_app_test
          AUTH_DATABASE_URL: postgresql+asyncpg://auth_p3a_runtime:ci-auth-runtime@127.0.0.1:5432/gymflow_p3a_app_test
        run: |
          set -euo pipefail
          python - <<'PY'
          import asyncio

          from fastapi import Depends, HTTPException, status
          from httpx import ASGITransport, AsyncClient
          from sqlalchemy.ext.asyncio import AsyncSession

          from app.core.database import get_db
          from app.core.deps import Staff, require_org_admin
          from app.core.security import create_access_token
          from app.main import app
          from app.repositories.organization_profile import (
              OrganizationProfileAuthorizationError,
              get_current_organization_profile,
              update_current_organization_profile,
          )
          from app.schemas.organization import OrganizationUpdate

          ALPHA_ORG = '31111111-1111-4111-8111-111111111111'
          BETA_ORG = '32222222-2222-4222-8222-222222222222'
          ALPHA_OWNER = 'caaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
          PROFILE_FIELDS = {
              'name', 'business_type', 'tagline', 'description',
              'year_established', 'website_url', 'social_links',
          }

          async def profile_or_403(db: AsyncSession):
              try:
                  return await get_current_organization_profile(db)
              except OrganizationProfileAuthorizationError as exc:
                  raise HTTPException(
                      status_code=status.HTTP_403_FORBIDDEN,
                      detail='Organization profile access denied',
                  ) from exc

          @app.get('/__p3a/profile-only')
          async def p3a_profile_only(
              current_staff: Staff = Depends(require_org_admin),
              db: AsyncSession = Depends(get_db),
          ):
              del current_staff
              return await profile_or_403(db)

          @app.patch('/__p3a/profile-only')
          async def p3a_update_profile_only(
              data: OrganizationUpdate,
              current_staff: Staff = Depends(require_org_admin),
              db: AsyncSession = Depends(get_db),
          ):
              del current_staff
              update_data = data.model_dump(exclude_unset=True)
              organization_updates = {
                  key: value
                  for key, value in update_data.items()
                  if key in PROFILE_FIELDS
              }
              try:
                  return await update_current_organization_profile(
                      db, organization_updates
                  )
              except OrganizationProfileAuthorizationError as exc:
                  raise HTTPException(
                      status_code=status.HTTP_403_FORBIDDEN,
                      detail='Organization profile access denied',
                  ) from exc

          def token(*, org_id: str = ALPHA_ORG, role: str = 'owner') -> str:
              return create_access_token(
                  owner_id=ALPHA_OWNER,
                  org_id=org_id,
                  email='app-alpha-owner@example.test',
                  role=role,
                  principal_type='owner',
              )

          async def main() -> None:
              async with AsyncClient(
                  transport=ASGITransport(app=app, raise_app_exceptions=False),
                  base_url='http://p3a.test',
              ) as client:
                  headers = {'Authorization': f'Bearer {token()}'}

                  response = await client.get('/__p3a/profile-only', headers=headers)
                  assert response.status_code == 200, response.text
                  payload = response.json()
                  assert payload['id'] == ALPHA_ORG, payload
                  assert payload['name'] == 'Application Alpha', payload
                  assert payload['tagline'] == 'alpha-before', payload
                  assert payload['year_established'] == 2001, payload

                  response = await client.patch(
                      '/__p3a/profile-only',
                      headers=headers,
                      json={
                          'name': 'Application Alpha Hardened',
                          'tagline': None,
                          'year_established': 2003,
                          'website_url': 'https://alpha.example.test',
                      },
                  )
                  assert response.status_code == 200, response.text
                  payload = response.json()
                  assert payload['name'] == 'Application Alpha Hardened', payload
                  assert payload['tagline'] is None, payload
                  assert payload['year_established'] == 2003, payload

                  # These fail before the P3B registration path is reached.
                  for body in (
                      {'year_established': 1799},
                      {'year_established': 9999},
                      {'tier': 'elite'},
                  ):
                      response = await client.patch(
                          '/organizations/profile', headers=headers, json=body
                      )
                      assert response.status_code == 422, (
                          body, response.status_code, response.text
                      )

                  response = await client.patch(
                      '/organizations/profile',
                      headers={'Authorization': f'Bearer {token(role="trainer")}'},
                      json={'tagline': 'wrong-role'},
                  )
                  assert response.status_code == 403, response.text

                  response = await client.patch(
                      '/organizations/profile',
                      headers={'Authorization': f'Bearer {token(org_id=BETA_ORG)}'},
                      json={'tagline': 'cross-tenant'},
                  )
                  assert response.status_code == 403, response.text
                  assert response.json()['detail'] == 'Organization profile access denied'

          asyncio.run(main())
          PY

'''
app_gate = app_gate[:step_start] + replacement + app_gate[step_end:]
APP_GATE.write_text(app_gate)

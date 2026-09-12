# PostgreSQL identity contract

DOERS uses separate PostgreSQL deployment logins and NOLOGIN capability roles. The canonical cluster-role contract lives under `security/cluster_role_bootstrap/`; runtime deployment bindings live in `security/runtime_identity/runtime_bindings.v1.json`; process exposure rules live in `security/runtime_identity/process_profiles.v1.json`.

## Runtime identities

- API deployment login inherits `app_runtime` and `app_user`.
- Auth deployment login inherits `auth_runtime`, `app_runtime`, and `app_user`.
- Ordinary worker login inherits only `worker_runtime`.
- Lifecycle-maintenance login inherits only `lifecycle_maintenance_runtime`.
- `migration_owner` is the only application migration LOGIN. It is not a runtime identity.

Runtime deployment memberships use PostgreSQL 16 membership options `ADMIN FALSE, INHERIT TRUE, SET FALSE`. Runtime logins may use the capability privileges but cannot switch identity with `SET ROLE`.

`migration_owner` has only the approved SET-only helper edges required by historical migration ownership boundaries. It must not inherit API, auth, worker, or maintenance runtime capabilities.

## Mandatory posture

Deployment logins are LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOINHERIT, NOREPLICATION, NOBYPASSRLS. Capability roles are NOLOGIN and similarly non-privileged.

Governed runtime settings include `row_security=on`, statement timeout, lock timeout, and idle-in-transaction timeout. Database-specific role setting overrides are rejected because they can silently change process posture.

## Tenant context

Tenant/audit context is transaction-local PostgreSQL configuration installed from verified request/task state. Pooled connections must not carry tenant state between units of work. Ordinary workers must receive explicit tenant identity in the command they process; they must not discover a tenant by probing unrestricted tables.

RLS is mandatory for governed tenant tables. Do not add BYPASSRLS, disable row security, convert capability roles to LOGIN, or solve a missing worker capability by granting a broad API role.

## Verification

Before Alembic HEAD, CI/release automation must:

1. run the canonical cluster-role bootstrap,
2. verify the exact role/settings/membership contract,
3. verify the P2C semantic non-escalation graph,
4. reject any forbidden drift before migration mutation.

Runtime processes additionally run P2D principal attestation and P2E process-profile validation. Any mismatch is a deployment failure, not a warning.

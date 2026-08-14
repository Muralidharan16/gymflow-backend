# DOERS production runtime processes

This document is the deployment contract for production runtime processes. Development may intentionally use shared local credentials, but production must use the process profiles enforced by `app/core/config.py` and `security/runtime_identity/process_profiles.v1.json`.

## Process matrix

| Process | `DOERS_PROCESS_PROFILE` | Database credentials present | Celery queue/profile |
|---|---|---|---|
| FastAPI API | `api` | API + auth only | none |
| Ordinary Celery worker | `worker` | worker only | queue `worker`, profile `worker` |
| Lifecycle maintenance worker | `maintenance` | maintenance only | queue `lifecycle-maintenance`, profile `maintenance` |
| Celery beat | `beat` | none | scheduler only |
| Flower/control plane | `beat` | none | observation/control only |
| Alembic migration job | not an application process | migration owner only | none |

Production startup is fail-closed. A process that receives a database environment variable outside its profile must fail validation before serving traffic or consuming broker messages.

## Database identity rules

The deployment LOGIN roles are distinct from capability roles. Application capability roles remain NOLOGIN. Deployment logins inherit only their approved capability set and must not receive `SET ROLE`, ADMIN, superuser, create-role, create-database, replication, or BYPASSRLS capabilities.

API and auth are separate database identities even though they live in the same FastAPI process. Ordinary queue work and lifecycle maintenance are separate operating-system/process boundaries and must not receive each other's database secret. Beat and Flower do not need a database credential.

## Container deployment

`docker-compose.yml` is a development convenience configuration. Apply `deploy/docker-compose.production-identities.yml` (or reproduce the same environment isolation in Kubernetes/systemd/another orchestrator) for production identity compartmentalization.

Do not place all database URLs in one shared production secret bundle and rely on application code to choose the correct one. Secret distribution is part of the security boundary.

## Startup expectations

1. Infrastructure provisions PostgreSQL capability roles using the canonical cluster-role bootstrap.
2. Deployment logins are provisioned with the exact runtime binding contract.
3. Alembic runs separately as `migration_owner` and reaches the single repository HEAD.
4. API/worker/maintenance startup attestation verifies the live PostgreSQL principal before work begins.
5. RLS remains enabled and forced on governed tenant tables; no production process disables it.

A green application health check is not a substitute for these identity proofs.

# DOERS / GymFlow Backend

FastAPI/PostgreSQL backend for the DOERS multi-tenant fitness and studio platform.

The repository is hardened around PostgreSQL 16, forced row-level security, explicit database identities, Alembic-only schema evolution, bounded worker/maintenance processes, and fail-closed production startup validation.

## Supported stack

- Python 3.12
- FastAPI + SQLAlchemy 2
- PostgreSQL 16 with Alembic migrations
- asyncpg for asynchronous PostgreSQL access
- Psycopg 3 for synchronous PostgreSQL access
- Redis + Celery
- PostgreSQL RLS for governed tenant data

The P2E certification workflow records the exact Python, PostgreSQL, and extension versions used by the hardened candidate. `requirements-test.lock` is the exact Python test environment used by that certification lane.

## Local development

Create a virtual environment and install the development/test dependency set:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-test.txt
```

For an environment matching the P2E certified dependency set exactly:

```bash
python -m pip install 'pip==26.2.1'
python -m pip install -r requirements-test.lock
python -m pip check
```

Copy the development example and configure local credentials:

```bash
cp .env.example .env
```

`.env.example` is intentionally a **development-only** example. It is not a production secret bundle. Authentication/bootstrap uses a separate PostgreSQL login from ordinary API traffic, even in a production-shaped local setup.

Apply the repository migration lineage before starting the application:

```bash
python -m alembic -c alembic.ini upgrade head
python -m alembic -c alembic.ini current --check-heads
```

Then run the API:

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## Database and tenant security

RLS is part of the database security boundary; it is not optional production hardening. Governed tenant tables remain RLS-enabled/forced, and application code installs transaction-local principal and tenant context before database work.

DOERS separates PostgreSQL deployment LOGIN roles from NOLOGIN capability roles. Production database identities include:

- API login → `app_runtime` + `app_user`
- auth/bootstrap login → `auth_runtime` + `app_runtime` + `app_user`
- ordinary worker login → `worker_runtime`
- lifecycle-maintenance login → `lifecycle_maintenance_runtime`
- migration login → `migration_owner`

Runtime logins do not receive `SET ROLE`, ADMIN membership, BYPASSRLS, SUPERUSER, CREATEDB, CREATEROLE, or replication privileges.

See:

- `docs/operations/database-identities.md`
- `docs/operations/runtime-processes.md`
- `docs/operations/migrations.md`
- `security/cluster_role_bootstrap/`
- `security/runtime_identity/`

## Production process isolation

Production has four application process profiles:

| Process | `DOERS_PROCESS_PROFILE` | Database credentials exposed |
|---|---|---|
| FastAPI | `api` | API + auth only |
| ordinary Celery worker | `worker` | worker only |
| lifecycle-maintenance worker | `maintenance` | maintenance only |
| Celery beat / database-free control process | `beat` | none |

A production process fails startup if it receives a forbidden database variable or its live PostgreSQL principal does not match the runtime identity contract.

`docker-compose.yml` is a development convenience. Production must reproduce the credential isolation in `deploy/docker-compose.production-identities.yml` or an equivalent Kubernetes/systemd/orchestrator configuration. Do not distribute every database secret to every container and rely on application code to choose safely.

## Migrations

Schema changes are applied only through the existing Alembic lineage. Do **not** run `alembic init`, generate a parallel migration tree, or create application tables automatically at startup.

Before production HEAD, infrastructure must provision the external capability-role contract and infrastructure-owned PostgreSQL extensions. The migration job then runs as the reduced `migration_owner` identity. P2B/P2C identity verification is performed before and after HEAD in the hardening certification path.

Operational procedure and rollback rules are documented in `docs/operations/migrations.md`.

## Tests and hardening gates

The repository contains general, finance, migration-lineage, worker, lifecycle, branch-hours, PostgreSQL identity, and runtime-principal regression suites. Passing an individual test file is not the production-readiness definition; the hardened candidate is accepted only when the complete required workflow matrix is green on one unchanged candidate.

Useful local commands include:

```bash
python -m pytest -q
python -s scripts/verify_alembic_graph.py
python -s scripts/migration_semantics_gate.py
python -s scripts/verify_runtime_identity_routing.py
python -s scripts/verify_head_workflow_bootstrap.py
```

The dedicated P2E workflow additionally installs `requirements-test.lock`, runs `pip check`, and compares the installed `pip freeze` against the committed lock to detect dependency drift.

## Production safety rules

Do not solve failures by weakening RLS, granting BYPASSRLS, converting capability roles to LOGIN, introducing broad runtime grants, sharing worker/maintenance secrets with API processes, or bypassing migration/identity preflight checks.

A green health endpoint or a successful Alembic command by itself is not sufficient evidence of a safe deployment. Production readiness requires the identity, migration, data-preservation, adversarial, runtime-boundary, and regression gates to remain green together.

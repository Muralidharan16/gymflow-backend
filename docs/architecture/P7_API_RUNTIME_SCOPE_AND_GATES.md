# P7 API Runtime, Probes and Graceful Deployment Behavior

Status: P7-G governance candidate

Certified P6 merge base: `561510eed6244cee268215ff1c53af72a636a601`

Certified P6 tree: `6ae7a92bd06100434b80aed98772147f2bbeaef4`

P7 branch: `hardening/p7-api-runtime-graceful-deployment`

## 1. Authoritative scope

P7 hardens the DOers API process lifecycle for production rollouts and shutdowns.
It covers system endpoint authorization, independent liveness/readiness semantics,
request draining, resource cleanup, worker recoverability inheritance, and
production-like orchestration evidence.

The hard gate is:

> Rollout and shutdown tests must pass under production-like orchestration without
> accepting new business work after drain begins, aborting admitted in-flight
> work, leaking API resources, or losing durable worker work.

## 2. Inherited P6 truths

P7 begins from the exact merged P6 tree and may not weaken it:

- PostgreSQL remains durable business authority.
- Redis/Celery/Beat remain delivery or coordination mechanisms only.
- At-least-once delivery and P6 worker redelivery/fencing guarantees remain in force.
- Production Redis TLS/auth/HA/persistence/noeviction requirements remain in force.
- Refund-provider execution remains deferred and fail-closed.
- RLS and process/database identity boundaries remain mandatory.
- P7 does not authorize release, tag, deployment, provider activation, or live money movement.

## 3. Secure preStop contract

`/_system/preStop` is orchestration control, not a tenant API. A public or ordinary
authenticated application caller must not be able to place a pod into DRAINING.

Production preStop invocation must use a dedicated high-entropy secret delivered
to the API process through an orchestration secret mechanism. The endpoint must:

- fail closed when the secret is absent from production configuration;
- compare the supplied credential without data-dependent string comparison;
- reject missing or invalid credentials without changing drain state;
- never log or return the credential;
- remain independent of tenant JWT/RLS identity; and
- be exempt only from tenant authentication after its own system authorization is
  mechanically enforced.

The orchestration contract must invoke this endpoint from inside the pod/container
or another explicitly trusted control path. Merely hiding the route is not security.

## 4. Liveness and readiness contract

Liveness and readiness have different meanings and must use different endpoints.

- **Liveness** answers whether the API process/event loop is alive enough to be
  restarted only when the process is actually unhealthy. It must not fail merely
  because PostgreSQL/Redis is temporarily unavailable or because the pod is draining.
- **Readiness** answers whether the pod may receive new business traffic. It must
  fail with HTTP 503 immediately when drain begins and may include bounded checks of
  required serving dependencies.

A draining pod therefore has liveness=200 and readiness=503 until process exit.
The legacy `/health` route may remain only as a compatibility alias if its meaning
is unambiguous and it is not used as both Kubernetes probes.

## 5. Drain and in-flight contract

Drain begins atomically before waiting for load-balancer propagation. Once DRAINING:

- readiness is false immediately;
- newly arriving ordinary business requests are rejected with 503 and a retryable
  response before tenant/business side effects begin;
- already admitted requests are allowed to complete within the termination budget;
- system liveness/readiness/preStop requests are not counted as business in-flight
  work and cannot make drain wait on itself;
- in-flight accounting cannot underflow or remain permanently positive after a
  request exits through success, error, or cancellation;
- repeated preStop calls are idempotent and do not extend the drain indefinitely;
- shutdown proceeds after in-flight reaches zero or a governed hard deadline.

## 6. Resource shutdown contract

FastAPI lifespan shutdown must close process-owned resources after request drain:

- supervised API-local background tasks stop cleanly;
- Redis clients are closed;
- API SQLAlchemy async pool is disposed;
- API synchronous compatibility engine, when present, is disposed;
- cleanup is idempotent and bounded;
- worker/maintenance engines not owned by the API process are not granted broader
  credentials or treated as API resources merely to simplify shutdown.

Readiness must not return to true after shutdown/drain has begun.

## 7. Worker termination inheritance

P7 does not replace P6 worker semantics. It must re-prove that the deployment
termination model does not weaken P6: Celery SIGTERM remains warm shutdown, late
ack/redelivery remains recoverable, and durable PostgreSQL work cannot be lost
because an API or worker container is terminated during rollout.

## 8. Production-like orchestration evidence

Decisive P7 runtime evidence must use real processes rather than TestClient-only
proof. At minimum it must:

1. start the API process with production-equivalent lifecycle configuration;
2. establish separate liveness and readiness probes;
3. start a controlled slow in-flight request;
4. invoke authorized preStop;
5. observe readiness become 503 while liveness remains 200;
6. prove a new business request is rejected after drain begins;
7. prove the admitted in-flight request completes before process termination;
8. send the production termination signal and observe bounded clean exit;
9. prove Redis/database resources are closed or no longer held by the exited API;
10. re-prove worker work remains recoverable under termination using inherited P6
   process semantics.

Mocks may validate authorization/parsing but may not be the decisive rollout/shutdown gate.

## 9. Controlled execution order

| Slice | Scope | Exit condition |
|---|---|---|
| P7-G | Governance | Exact P6 base, contracts, hard gate and hard stops frozen |
| P7-S | System endpoint security and probes | Public drain blocked; liveness/readiness separated and fail correctly |
| P7-D | Request drain semantics | New work rejected after drain; admitted work completes; no self-count deadlock |
| P7-R | Resource shutdown | Supervisor, Redis and API DB resources close cleanly and idempotently |
| P7-W | Worker termination inheritance | P6 worker termination/redelivery guarantees re-proved under deployment model |
| P7-O | Production-like orchestration | Real process rollout/shutdown scenario passes end-to-end |
| P7-F | Final same-head certification | P7 plus inherited P1-P6 critical gates pass on one immutable SHA |

## 10. Hard stops

P7 does not authorize:

- a public or tenant-authenticated caller draining a pod;
- one endpoint being used as both liveness and dependency-sensitive readiness;
- accepting new business work after DRAINING begins;
- killing admitted requests before the governed grace budget without explicit failure evidence;
- treating Redis/Celery state as durable business authority;
- weakening P6 task acknowledgement/redelivery/fencing behavior;
- broadening database credentials for probes or shutdown;
- embedding privileged infrastructure credentials in probe responses or logs;
- merge, retarget, tag, release, deployment, refund-provider activation, or live money movement.

## 11. Change control

Changing preStop authorization, probe semantics, request-admission behavior,
resource ownership, termination signal semantics, orchestration evidence, or the
hard gate is a P7 governance amendment and invalidates prior P7-G certification.

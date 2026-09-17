# P10 — Performance, security and final production certification

## Baseline

Phase 10 starts only from merged P9 `main` commit `33abd2bad81c65ac998b91726cc314ab54080010`, tree `a6a0a87ebbaa251482c67f1bc4cbd9dc8ab7d1e0`.

The Alembic head at the P10 boundary is `zk07d8e9f0a45`. P10 does not authorize a schema change by default; any migration introduced during P10 must independently preserve the complete P9 recovery and compatibility contract.

PostgreSQL remains the durable authority for business work. Redis, Celery and Beat remain delivery/coordination surfaces. Refund-provider execution remains deferred and fail-closed. No release, production deployment, live-provider activation, production-data copy or live money movement is authorized by P10 certification.

## Slices

- **P10-G — Governance.** Freeze the P10 baseline, scope, hard stops, evidence requirements and terminal markers.
- **P10-B — Representative baseline and numeric budget freeze.** Build a production-shaped synthetic environment using real PostgreSQL 16, real Redis and the production container. Record representative load, queue and resource baselines, then freeze numeric pass/fail budgets before the load, queue or soak certification slices. Budgets may be tightened only with full recertification; loosening them to make a candidate pass is forbidden.
- **P10-L — Representative load and concurrency stress.** Exercise representative multi-tenant read/write traffic plus deliberate same-entity and cross-tenant contention. Record p50/p95/p99 latency, throughput, errors, CPU, RSS, database-pool and Redis health while preserving Finance/lifecycle/security invariants.
- **P10-Q — Queue throughput and backlog recovery.** Measure enqueue/drain throughput, seed a bounded durable backlog, replace a worker during the backlog, and prove bounded recovery with no lost durable work or duplicate terminal effect.
- **P10-D — Query plans, indexes, N+1 and pagination.** Capture `EXPLAIN (ANALYZE, BUFFERS)` for critical queries, review index effectiveness, detect N+1 behavior, measure endpoint query counts, and prove bounded pagination with an explicit maximum page size.
- **P10-S — Long soak.** Run sustained mixed API and worker activity in the hardened production container with real dependencies and time-series resource evidence. Progressive memory, connection, latency or queue-depth growth is a hard stop.
- **P10-X — Dependency, container, static-security, secret and configuration audit.** Produce an SBOM; run locked dependency, container-image, static-security and secret scans; prove required security-sensitive configuration fails closed. Unresolved critical/high runtime vulnerabilities and verified secrets are hard stops unless an explicit machine-readable false-positive/non-exploitability exception is separately reviewed and frozen.
- **P10-H — Production container hardening.** Prove non-root execution, no reload/debug, healthcheck, correct SIGTERM behavior, read-only-root-filesystem operation, no-new-privileges, dropped Linux capabilities, CPU/memory/PID limits and only the minimum exposed application port.
- **P10-F — Final one-commit certification.** Bind every inherited P1-P9 gate and every P10 gate to one immutable SHA/tree and emit the final production-certification decision.

## Budget discipline

P10 deliberately does not invent pass/fail latency, throughput, backlog or resource numbers before the representative baseline exists. P10-B must create the numeric budget artifact and bind it to an exact SHA. P10-L, P10-Q and P10-S are forbidden to certify until that artifact exists.

The budget artifact must include at least latency percentiles, request throughput, error budget, queue-drain throughput/deadline, memory-growth budget, connection-growth budget and soak-duration target. The environment, synthetic dataset scale, worker count and client concurrency used to derive the baseline must also be recorded.

A later candidate may improve or tighten a budget. It may not loosen a frozen budget merely to convert a failure into a pass.

## Representative load and concurrency

The load mix must include meaningful authenticated multi-tenant reads and writes, not only `/health`. There must be evidence from both normal parallel traffic and deliberate contention. Five-hundred errors without an injected fault are a certification failure.

Performance tuning may not weaken tenant isolation, authorization, RLS, idempotency, Finance correctness, lifecycle fencing, durable-worker semantics or P9 recovery behavior.

## Queue certification

PostgreSQL remains the durable work authority. P10-Q must measure backlog depth over time and prove that replacing a worker while a backlog exists does not lose work, duplicate terminal effects or require manual database edits to resume progress.

## Database/API efficiency

Critical application query plans must be captured against production-shaped synthetic cardinality. Sequential scans are not automatically failures; an unexplained plan regression, unbounded collection scan or index strategy that violates the frozen budget is.

All collection endpoints in the representative set must have bounded pagination and a tested maximum page size. Representative endpoint query counts must be measured so N+1 regressions cannot hide behind acceptable wall-clock latency.

## Long soak

The soak must use the production container and real PostgreSQL/Redis. It must contain both API and worker activity and collect time-series latency, CPU, RSS, database-pool/connection and queue-depth data. Restarting processes to hide progressive growth is forbidden.

## Security and secrets

P10-X must produce machine-readable scan evidence and an SBOM for the exact candidate. Live credentials are forbidden in CI. Mandatory production secrets and security-sensitive settings must fail closed when absent or invalid; insecure default credentials are forbidden.

## Production container

The production image must run as a non-root user, use exec-form process startup without `--reload`, expose only the required application port, provide an application-aware healthcheck, and terminate correctly on SIGTERM.

The certification runtime must also prove read-only-root-filesystem operation, `no-new-privileges`, all Linux capabilities dropped, and explicit CPU, memory and PID controls. A privileged container is forbidden.

## Hard stops

P10 stops rather than certifies on any condition listed in `p10_performance_security_final_certification_matrix.json`, including inherited semantic weakening, budget manipulation, lost/duplicated durable work, verified secrets, unresolved critical/high runtime vulnerabilities, fail-open security configuration, root/privileged production containers, missing resource controls, unbounded resource growth, live-provider activation, release or production deployment.

The final gate is: **representative performance, concurrency, queue recovery, query efficiency, long-soak stability, security posture and hardened production-container behavior are all proven on the same immutable head while every inherited P1-P9 invariant remains green.**

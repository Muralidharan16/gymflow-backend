# P8-F — Final Same-Head Certification

## Purpose

P8-F is the terminal certification gate for Phase 8 — Observability and operational response. It does not add product behavior. It proves that the complete P8 candidate, all P8 observability gates, and every inherited P1-P7 hardening gate succeed on one immutable Git commit before any integration decision.

## Immutable parent checkpoint

P8-F is rooted in the certified P8-O production-like observability checkpoint:

- P8 implementation branch: `hardening/p8-observability-operational-response`
- Certified P8-O HEAD: `d8c22bd6b26f0da861b7d8957fd9d9d81bc0fcf6`
- Certified P8-O tree: `426b57b351d9ab85df17cc49b151186e18faf4b5`
- Certified P8-O workflow run: `34999434896`
- Current Alembic head: `zk07d8e9f0a45`

Same-head P8 evidence at that checkpoint:

- P8-G governance: `34999434927` — success
- P8-L structured logging/redaction: `34999434979` — success
- P8-M runtime metrics: `34999434988` — success
- P8-A alerts/SLOs: `34999434894` — success
- P8-R operational runbooks: `34999434915` — success
- P8-O production-like observability: `34999434896` — success

The P8-F certification branch is `hardening/p8f-final-certification-temp` and must remain a certification-only descendant of the exact P8-O checkpoint above.

## Final certification topology

P8-F has exactly **46 prerequisite jobs** before the terminal decision:

1. Final P8 certification contract
2. Architecture contracts
3. General/platform-billing/fresh-lineage migration regression
4. Finance Core regression
5. Full migration lifecycle
6. Populated migration data preservation
7. Adversarial migration recovery
8. Migration lifecycle contracts
9. Migration semantics inventory
10. Inherited P1/P2/P3 certification
11. P3A broad regression
12. Lifecycle maintenance production boundary
13. Platform maintenance production boundary
14. P4B real OpenSearch behavior
15. P4B provider evidence compatibility
16. P4B drift-repair compatibility
17. P4C broad regression
18. P4C notification compatibility
19. P4D refund-obligation authority without provider execution
20. P4E external-effect contract
21. P4E operational observability
22. P5 governance/fault matrix
23. P5-W1 fencing
24. P5-W2 real worker crash/redelivery
25. P5-E provider-success/DB-ack ambiguity
26. P5-D dependency/database loss
27. P5-R race/deadlock interleavings
28. P5-C compensation crash/replay
29. P6 governance
30. P6 Redis production contract
31. P6 broker restart/reconnect
32. P6 graceful worker shutdown/redelivery
33. P6 poison-message containment
34. P6 scheduler ownership
35. P7 governance/API runtime
36. P7 system control/probes
37. P7 request drain
38. P7 API resource shutdown
39. P7 worker termination inheritance
40. P7 production-like orchestration
41. P8-G governance/observability
42. P8-L structured logging/redaction
43. P8-M runtime metrics
44. P8-A alerts/SLOs
45. P8-R operational runbooks
46. P8-O production-like observability

The terminal certification job must run with `if: always()`, inspect every prerequisite result, require all 46 to be `success`, and reject any topology change.

## Required P8 invariants

P8-F must preserve and reprove the complete Phase 8 contract:

- structured request/task context remains bounded and authority-derived;
- secrets, credentials and sensitive PII are recursively redacted before telemetry sinks;
- metrics have bounded cardinality and do not label tenant, branch, principal, request, saga, task, trace or span identifiers;
- API, database, queues, lifecycle, Finance, providers and platform signals remain instrumented;
- every critical failure mode has an actionable alert and runbook path;
- SLO/error-budget contracts remain frozen;
- production-like failure injection proves real PostgreSQL, Redis/Celery, provider and OTLP behavior;
- observability remains evidence only and never becomes business authority;
- lifecycle maintenance retains aggregate-only access to lifecycle-saga dead-letter truth and does not gain raw `branch_outbox_events` SELECT authority;
- PostgreSQL remains durable business authority;
- refund-provider execution remains deferred and fail-closed.

## Inherited safety invariants

P8-F must not weaken any certified P1-P7 boundary. In particular:

- tenant/RLS and role-authority boundaries remain intact;
- platform billing and member payments remain separated;
- external effects remain idempotent/fenced and recoverable;
- crash/redelivery, race/deadlock and compensation guarantees remain intact;
- Redis/Celery/Beat remain coordination/delivery infrastructure rather than durable business authority;
- API graceful-drain and worker termination guarantees remain intact;
- no lost durable business work is accepted.

## Hard stops and non-goals

P8-F does **not** authorize or perform:

- merge to `main`;
- tag or release creation;
- deployment;
- production configuration changes;
- branch-protection administration changes;
- refund-provider activation;
- live money movement;
- expansion of database privileges merely to make certification pass.

Any failed gate is a hard stop. A repair creates a new candidate and requires the entire final same-head certification to run again on that exact head.

## Terminal markers

A successful terminal decision must emit all of the following on the exact immutable candidate:

```text
P8_EVERY_CRITICAL_FAILURE_VISIBLE_ACTIONABLE=PASS
P8_PRODUCTION_LIKE_OBSERVABILITY=PASS
P8_OBSERVABILITY_NON_AUTHORITATIVE=PASS
P8_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
P8F_FINAL_SAME_HEAD_CERTIFICATION=PASS
```

Only `P8F_FINAL_SAME_HEAD_CERTIFICATION=PASS` on a run where all 46 prerequisite jobs succeeded constitutes final Phase 8 certification. Integration remains a separate explicitly authorized lifecycle step.

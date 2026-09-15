# P7-F Final Same-Head Certification

## Purpose

P7-F is the terminal certification gate for Phase 7 API runtime graceful deployment and shutdown readiness. It does not add production behavior. It re-proves the inherited P1-P6 critical gates and every frozen P7 slice against one immutable Git candidate.

## Frozen P7 slice baseline

The final certification candidate must descend from the fully green P7 slice head:

- commit: `916bc58e80f06e46311b3c3572e54268a06c82f8`
- tree: `b13592b0cd80a8112826c26af538e06d7369450f`
- governance anchor: `a666250e8dd83a7de195ce06731f935406a90846`

That slice head passed P7-G, P7-S, P7-D, P7-R, P7-W and P7-O. P7-O used real PostgreSQL 16, a reduced production API/auth database identity, authenticated TLS Redis with persistence/noeviction/replication, and a real Uvicorn process. P7-W inherits the real Celery worker SIGTERM and late-ack/redelivery evidence from the exact same candidate.

## Same-head topology

The P7-F workflow has one frozen contract job, the 33 reusable inherited P1-P6 gates from P6-F, and six P7 reusable gates. The terminal decision therefore depends on exactly 40 prerequisite results:

1. Final P7 contract
2. Inherited architecture contracts
3. General/platform/migration hardening
4. Finance regression
5. Full migration lifecycle
6. Migration data preservation
7. Adversarial migration recovery
8. Migration lifecycle contracts
9. Migration semantic inventory
10. P1-P3 certification
11. P3A general regression
12. Lifecycle maintenance production boundary
13. Platform maintenance production boundary
14. P4B real OpenSearch
15. P4B provider evidence
16. P4B drift repair
17. P4C general regression
18. P4C real notification compatibility
19. P4D refund authority without provider execution
20. P4E external-effect contract
21. P4E operational observability
22. P5 governance
23. P5 worker fencing
24. P5 worker crash/redelivery
25. P5 provider-success/DB-ack ambiguity
26. P5 dependency loss
27. P5 race/deadlock interleavings
28. P5 compensation replay
29. P6 governance
30. P6 Redis production contract
31. P6 broker recovery
32. P6 worker shutdown/redelivery
33. P6 poison-message containment
34. P6 scheduler ownership
35. P7 governance
36. P7 system control and probes
37. P7 request drain
38. P7 API resource shutdown
39. P7 worker termination inheritance
40. P7 production-like orchestration

Every P6 and P7 gate that accepts a certification head is bound to `${{ github.sha }}`. The terminal job runs with `always()` and fails if any prerequisite result is not `success`.

## Terminal invariants

Before P7-F may emit PASS, the terminal job must prove:

- checked-out `HEAD` equals `GITHUB_SHA`;
- `HEAD^{tree}` equals `git write-tree`;
- the worktree is clean;
- all 40 prerequisite results are successful.

It then emits only the frozen P7 terminal markers:

- `P7_PRESTOP_AUTHORIZATION=PASS`
- `P7_LIVENESS_READINESS_SEPARATED=PASS`
- `P7_READINESS_FALSE_BEFORE_SHUTDOWN=PASS`
- `P7_NEW_REQUEST_DRAIN=PASS`
- `P7_INFLIGHT_COMPLETION=PASS`
- `P7_API_RESOURCE_SHUTDOWN=PASS`
- `P7_WORKER_TERMINATION_RECOVERABLE=PASS`
- `P7_PRODUCTION_LIKE_ROLLOUT=PASS`
- `P7_NO_LOST_DURABLE_BUSINESS_WORK=PASS`
- `P7_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`
- `P7F_FINAL_SAME_HEAD_CERTIFICATION=PASS`

## Hard boundary

P7-F is certification only. Passing it does not authorize or perform a merge, release, tag, deployment, refund-provider activation, or live money movement. Refund provider execution remains deferred and fail-closed.

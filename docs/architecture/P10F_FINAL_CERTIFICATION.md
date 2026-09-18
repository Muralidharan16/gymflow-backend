# P10-F — Final Same-Head Production Certification

## Purpose

P10-F is the terminal certification gate for Phase 10. It adds no product
behavior and authorizes no release or deployment. It binds the complete
performance, concurrency, recovery, query-efficiency, soak, security and
container-hardening evidence to one immutable candidate SHA/tree while
re-proving every inherited P1-P9 boundary.

## Immutable baseline

- merged P9 base SHA: `33abd2bad81c65ac998b91726cc314ab54080010`
- merged P9 base tree: `a6a0a87ebbaa251482c67f1bc4cbd9dc8ab7d1e0`
- Alembic head: `zk07d8e9f0a45`
- frozen P10 budget digest:
  `b8613f5deab4d1dba77ba41d86b4cb4d8e8dab6b85fa5630a6206105951b8520`
- P10-B recertification note: the soak-stability limits were added before the accepted P10-S run; no previously frozen threshold was loosened, and all downstream P10 gates must re-certify on the resulting exact SHA.

## Inherited same-head proof

The final workflow preserves the proven P9-F topology. Inherited reusable gates
that do not have canonical P10-PR sibling runs are invoked directly. Existing
canonical workflows are bound by exact candidate SHA and successful completion.

P10-L contains the real P5-R lifecycle/Finance/deadlock reusable gate at the
current Alembic head. P10-Q contains the real P5-W2 worker crash/redelivery gate
at the current Alembic head. Their successful terminal decisions therefore
serve as the same-head P5-R/P5-W2 inherited reproof while also satisfying their
P10 slice responsibilities.

## P10 slice proof

The terminal decision requires successful exact-head runs for:

- P10-G governance;
- P10-B immutable performance-budget freeze;
- P10-L representative load and concurrency stress;
- P10-Q queue throughput/backlog recovery;
- P10-D query/index/N+1/pagination performance;
- P10-S long soak;
- P10-X security scans and fail-closed configuration;
- P10-H production container hardening.

Evidence-producing slices must expose Actions artifacts. P10-F records their
artifact metadata together with exact run IDs and the committed budget digest.

## Hard stops

Any missing, pending, cancelled or failed prerequisite is a hard stop. P10-F
does not allow budget loosening, security suppression, privilege broadening,
customer/production data in CI, live provider credentials, refund-provider
execution, live money movement, tag/release creation, merge, or deployment.

## Terminal markers

A successful immutable candidate emits:

```text
P10_P1_P9_INHERITED=PASS
P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
P10_FINAL_PRODUCTION_CERTIFICATION=PASS
```

Integration remains a separate explicitly authorized lifecycle step.

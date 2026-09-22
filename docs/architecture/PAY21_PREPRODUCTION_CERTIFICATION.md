# PAY-21 — Pre-Production Certification

PAY-21 certifies the exact payment architecture in a production-shaped, non-production environment before any production customer or money rollout.

## Immutable predecessor

PAY-21 begins from the exact certified PAY-20 commit:

- SHA: `5abfeb7a9a3cee44fc362e1a80f74599a6f694ee`
- Tree: `996da603e4cdd088e91e0fef1ecc0dc7d7cddfea`
- Alembic head: `zz47d8e9f0a64`

PAY-21 is a certification phase. It adds no new financial authority and no migration.

## Production-shaped environment

Certification requires all of the following on one exact candidate:

- PostgreSQL 16 with canonical cluster roles and distinct runtime logins.
- Redis 7 with TLS, authentication, persistence, noeviction, and a private container network.
- A real Celery worker running the production image and the production worker process profile.
- The production API image behind a TLS reverse proxy. The API, worker, Redis, and PostgreSQL bridge are not directly published as customer-facing ports.
- Secret material injected through the CI secret boundary for the real Razorpay test account. Live-mode keys are rejected before network I/O.
- Real Razorpay test-mode HTTPS order creation against `https://api.razorpay.com/v1`. No live provider key, production customer, or real-money payment is allowed.
- Production-style PostgreSQL capability roles and row-security settings.
- Backup/restore and bad-deployment rollback rehearsal.

The real Razorpay network proof establishes credential validity, TLS reachability, account mode, and provider order creation. Deterministic payment success/failure/refund/dispute scenarios then execute against the certified Finance/Platform Billing test-mode boundaries so the suite remains non-interactive, replayable, and free of real money.

## Required end-to-end scenarios

The exact candidate must prove:

1. member admission and exact-once activation;
2. online checkout/order plus signed payment confirmation/application;
3. cash payment approval;
4. partial payment;
5. renewal lineage;
6. refund;
7. partial refund / remaining-refundable-balance behavior;
8. settlement and accounting reconciliation;
9. failed payment with no accidental financial application;
10. platform subscription;
11. recurring attempt;
12. dunning;
13. mandate revocation;
14. dispute simulation;
15. backup and restore with financial fingerprint preservation;
16. bad deployment rollback with the last-known-good image/schema retained.

## Safety

PAY-21 must fail closed if any of the following occurs:

- Razorpay key id is not `rzp_test_*`;
- required secret values are absent;
- a production/live key marker is observed;
- provider secrets appear in logs or artifacts;
- any production data source is referenced;
- API/worker roles are privileged, shared, or can SET ROLE into protected owners;
- TLS validation is bypassed;
- Redis or internal application ports are exposed outside the private pre-production network;
- any scenario leaves duplicate money, duplicate subscription activation, open reconciliation drift, or an unbalanced ledger;
- backup/restore changes the financial fingerprint;
- rollback cannot restore the last-known-good service state.

## Terminal decision

Only the same exact commit that passes every PAY-21 job may emit:

```text
PAY21_PREPRODUCTION_CERTIFICATION=PASS
```

A PAY-21 pass does not itself authorize production deployment, production credentials, production customer data, or live money movement.

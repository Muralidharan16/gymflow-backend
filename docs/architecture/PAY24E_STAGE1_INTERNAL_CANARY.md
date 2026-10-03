# PAY-24-E — Stage-1 Internal Canary

PAY-24-E prepares the first live activation boundary but does **not** authorize
or perform Stage 1. Its predecessor is PAY-24-D evidence head
`79868320b72d4fba66aee9a48598060401fbbaf4`.

PAY-24-C isolated entitlement mutation to `entitlement_runtime`, but the
claim/apply path did not independently consult PAY-24-A. PAY-24-E closes that
gap in PostgreSQL.

Stage 0 may accumulate durable entitlement commands, but cannot claim them. The
old PAY-24-C claim capability is revoked from entitlement runtime. The new claim
capability returns no work in Stage 0 and, in Stage 1, only claims commands for
the exact durable internal organization.

Protected term, V2 and freeze mutation guards re-read PAY-24-A at mutation time.
They require Stage 1, open egress, exact internal organization, exact
release/authorization SHA binding, and exactly these enabled capabilities:
checkout, webhooks, payment_application and subscription_activation. Refund
execution, recurring billing, dunning and platform billing must remain disabled.

Because mutation rechecks durable authority, begin-emergency-rollback
(`provider_egress_state=closing`) blocks a command that was leased just before
rollback began.

This slice intentionally supports Stage 1 only. Later rollout stages require a
separate reviewed expansion.

```text
PAY24E_STAGE1_DB_AUTHORITY_FENCE=CANDIDATE
PAY24_STAGE1_LIVE_ACTIVATION=NOT_AUTHORIZED
PAY24_REAL_PROVIDER_CALLS=0
PAY24_REAL_MONEY_MOVEMENT=0
PAY23_ENTERPRISE_PAYMENT_SYSTEM=NOT_CERTIFIED
```

## Prepared activation package

The certified candidate includes an isolated one-purpose scheduler and
entitlement worker overlay under the `pay24-stage1-canary` profile. Both
services declare zero replicas, so repository/compose presence alone cannot
start the canary.

The scheduler has no database or provider credential and publishes only
`app.tasks.entitlement_dispatcher.run` once per minute. Refund entitlement
remains unscheduled.

`scripts/pay24e_stage1_canary_preflight.py` is read-only. It verifies either
the Stage-0 locked posture or the exact Stage-1 SHA/internal-organization/four-
switch posture and reports aggregate entitlement backlog without exposing
secrets or changing activation state.

## Certification evidence

PAY-24-E application/runtime code is frozen at
`eb4732ac94d3a0eadd9ca9005f2e63665f3377d0`
(tree `7554309f60f4e2d0d0654f57869c8b945d7ee4ad`).

P2D Runtime Principal Attestation run `37107265551` completed successfully on
head `29099dfdca3ce57df60fefe544ecc1a07abbc612`. The two commits after the
frozen application SHA changed only
`tests/test_pay24c_entitlement_boundary_runtime.py`: one made synthetic
PAY-24-A authorization IDs unique, and one isolated infrastructure-superuser
fixture cleanup from production trigger enforcement.

The successful run proved:
- PAY-24-E static Stage-1/canary package contracts;
- canonical external PostgreSQL role bootstrap;
- fresh PostgreSQL 16 migration through `zzc7d8e9f0a72`;
- exact live runtime-principal bindings;
- Stage-0 entitlement commands remain durable but unclaimable;
- the predecessor PAY-24-C claim is no longer executable by entitlement runtime;
- exact internal-organization Stage-1 claim/application succeeds only under the
  minimal four-switch PAY-24-A posture and exact release/authorization binding;
- rollback `closing` denies further claim/mutation;
- adversarial principal drift fails closed;
- P2B/P2C role contracts remain valid after restoration.

This is **activation-package certification, not live activation**. Both canary
services remain profile-gated with zero replicas; no production Stage-1
transition, provider call, or real-money movement is authorized by this result.

```text
PAY24E_STAGE1_DB_AUTHORITY_FENCE=PASS
PAY24E_STAGE0_COMMAND_CLAIM=DENIED
PAY24E_INTERNAL_ORG_SCOPE=PASS
PAY24E_ROLLBACK_MUTATION_RACE=PASS
PAY24E_INERT_ACTIVATION_PACKAGE=PASS
PAY24E_EXACT_SHA_PREFLIGHT=PASS
PAY24_STAGE1_CANARY_PACKAGE=PASS

PAY24_STAGE1_LIVE_ACTIVATION=NOT_AUTHORIZED
PAY24_REAL_PROVIDER_CALLS=0
PAY24_REAL_MONEY_MOVEMENT=0
PAY23_ENTERPRISE_PAYMENT_SYSTEM=NOT_CERTIFIED
```


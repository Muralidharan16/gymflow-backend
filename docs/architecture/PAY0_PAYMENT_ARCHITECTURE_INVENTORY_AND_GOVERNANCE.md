# PAY-0 Payment Architecture Inventory and Governance

**Phase:** PAY-0  
**Date:** 2026-09-19  
**Branch:** `hardening/pay0-payment-architecture-inventory`  
**Immutable inherited P10 SHA:** `4357fb14b406514d80376f6d23aed4dc185c4c00`  
**Inherited P10 tree:** `66e4528ee2380a5591ee8a94d9df3ff465e3581c`  
**Alembic head:** `zk07d8e9f0a45`

## Purpose

PAY-0 freezes the current DOers payment and billing architecture before any new
money-moving implementation is admitted. It is inventory/governance only.

PAY-0 changes no application behavior, no Alembic revision, no database ACL,
no RLS policy, no provider configuration, no payment state, no refund state and
no subscription entitlement.

## Authority model

The programme freezes these rules:

- PostgreSQL Finance Core is the durable internal financial/accounting authority.
- Verified provider evidence establishes external provider money facts.
- Member Commerce owns membership/subscription business state, not accounting truth.
- DOers Platform Billing is a separate SaaS billing domain and must not reuse
  member-commerce authoritative payment rows.
- Browser callbacks are non-authoritative.
- Redis, Celery and Beat are delivery/coordination only.
- Telemetry and operator input are never financial authority.
- Legacy `public.payments` / `public.invoices` are compatibility/deprecation
  surfaces and must not be extended into a second authoritative ledger.

## Current-state findings frozen by PAY-0

1. Finance Core already contains the strongest accounting primitives: invoices,
   payment state/evidence, allocations, ledger, settlement reconciliation,
   credit notes, refund intent, idempotency and outbox.
2. Live Finance payment API and live money movement remain disabled.
3. Refund provider execution remains deferred and fail-closed.
4. The mounted legacy member-payment route and its service/schema/model contracts
   have drifted and are not acceptable as the future authoritative payment path.
5. Modern `member_subscriptions_v2` creation currently produces an active
   subscription before authoritative payment completion.
6. Member subscription checkout is currently Razorpay sandbox/test-mode only.
7. Finance Core emits durable outbox rows, but the production product-consumer
   path that idempotently activates member subscriptions is not yet implemented.
8. Platform Billing has provider-neutral/fake-provider persistence and
   reconciliation foundations, but no admitted real provider/webhook route.
9. Platform checkout, webhook processing, dunning transitions and notifications
   remain disabled by default.

## Migration safety baseline

PAY-0 deliberately adds **zero migrations**.

The certified Alembic graph must remain a single head at `zk07d8e9f0a45`.
The inherited payment/billing migration inventory includes:

- `aa9303384b66_initial_schema.py` — legacy payment/invoice baseline.
- `c3a4b5c6d7e8_create_subscription_lifecycle_foundation.py`.
- `e5f6a7b8c9d0_harden_subscription_lifecycle_constraints.py`.
- `f1a2b3c4d5e6_platform_billing_phase_1_foundation.py`.
- `f2b3c4d5e6a7_platform_billing_phase_2_resolver.py`.
- `014167728f4a_platform_billing_phase_4a_provider_persistence.py`.
- `1a2b3c4d5e7f_finance_core_phase_5b_foundation.py`.
- `zc07d8e9f0a3d_p4d_refund_authority_boundary.py`.
- `zd07d8e9f0a3e_p4d_refund_obligation_resolution.py`.

Every future PAY migration must be additive unless a separately reviewed
compatibility migration proves otherwise. It must pass forward lifecycle,
downgrade/re-upgrade where supported, populated-data preservation, ACL/RLS
preservation, ownership checks, lock/rewrite analysis and rolling-version
compatibility before acceptance.

## PAY-0 hard stops

PAY-0 fails if it:

- modifies any file under `alembic/versions/`;
- modifies application/runtime code;
- changes DB roles, grants, RLS or SECURITY DEFINER functions;
- enables live provider credentials, live checkout or live webhook processing;
- enables refund provider execution;
- changes subscription activation behavior;
- changes Platform Billing enforcement/dunning/notifications;
- weakens inherited P1-P10 security or migration guarantees;
- authorizes merge, release, deployment or live money movement.

## Required evidence

The PAY-0 workflow must prove on one exact SHA:

- exact inherited P10 parent/tree;
- only the PAY-0 documentation/test/workflow allowlist changed;
- Alembic graph verification passes;
- Alembic has exactly one head: `zk07d8e9f0a45`;
- PAY-0 static inventory tests pass;
- inherited architecture, general/Platform Billing, Finance Core and all
  migration lifecycle/preservation/adversarial/semantics gates pass;
- no live-money or refund-provider authority is enabled.

## Terminal markers

```text
PAY0_ARCHITECTURE_INVENTORY=PASS
PAY0_MIGRATION_BASELINE=PASS
PAY0_P10_INHERITED=PASS
PAY0_LIVE_MONEY_MOVEMENT=DISABLED
PAY0_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
PAY0_FINAL=PASS
```

PAY-0 does not authorize PAY-1 implementation, merge, release, deployment,
provider activation, refund execution or live money movement.

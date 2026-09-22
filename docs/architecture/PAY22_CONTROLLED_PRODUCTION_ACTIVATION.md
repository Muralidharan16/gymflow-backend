# PAY-22 — Controlled Production Activation

## Certified predecessor

PAY-22 begins from the exact PAY-21 certified predecessor:

- SHA: `fd5370320e3117b4220e79ebc8e45d227ccf0077`
- tree: `f66f789328e4ad934b8a59758bd91f87b365ecb3`
- workflow run: `35733153483`
- marker: `PAY21_PREPRODUCTION_CERTIFICATION=PASS`

PAY-22 introduces activation authority only. It does not turn a sandbox adapter into
a live provider adapter, execute a live refund, move real money, or authorize a
production deployment by source code alone.

## Governing rule

There is no `payments_enabled` switch. A production action is allowed only when:

1. deployed SHA equals the exact certified PAY-22 executable candidate;
2. external human authorization references that exact SHA;
3. live provider configuration is present;
4. the organization is admitted by the current rollout stage;
5. the requested capability's independent kill switch is enabled;
6. provider egress is enabled for outbound provider operations; and
7. all existing domain, RLS, idempotency, provider, accounting and reconciliation
   guards also pass.

Every failure is deny-by-default.

## Stages

### Stage 0 — live config, egress blocked

Live provider configuration may be installed by the secrets system, but provider
egress must remain blocked, all eight capability switches remain disabled and no
organization is admitted.

### Stage 1 — internal organization only

Only the configured DOers internal organization is cohort-eligible.

### Stage 2 — selected test organization

Stage 1 remains admitted and one explicit selected test organization is added.

### Stage 3 — very small merchant cohort

The previous organizations remain admitted. An explicit allowlist may add at most
10 merchants. Percentage rollout is forbidden.

### Stage 4 — limited percentage

Previous cohorts remain admitted. Stable SHA-256 bucketing may admit additional
organizations, capped at 1,000 basis points (10%). A rollout seed is mandatory.

### Stage 5 — general availability

All organizations are cohort-eligible. Exact-SHA authorization, provider-egress
fencing and all individual kill switches remain authoritative.

## Independent kill switches

Exactly these switches exist:

- `checkout`
- `webhooks`
- `payment_application`
- `subscription_activation`
- `refund_execution`
- `recurring_billing`
- `dunning`
- `platform_billing`

Disabling one switch does not require disabling another. The shared service uses
immutable runtime posture so a switch-store refresh can replace the posture
without shutting down the whole DOers application.

## Provider-egress fence

Provider egress is a separate fence. When blocked, checkout, refund execution and
recurring billing are denied. Webhooks, payment application, subscription
activation, dunning and Platform Billing may remain available when their own
switches and normal guards permit them. This is the required outage-isolation
property.

## Existing guards remain inherited

PAY-22 does not weaken the current Finance Core guard that rejects unapproved live
behavior. The new `app.payment_activation` authority is an additional mandatory
gate for any later live member-payment or Platform Billing entry point.

Provider refund execution remains fail-closed until a separately certified live
refund adapter exists. A refund kill switch is not itself refund implementation
authority.

## Human authorization

Human authorization is external to the executable candidate. Source code cannot
authorize itself and an authorization boolean committed into the same candidate
would create an exact-SHA circularity.

The release authorization record must contain an authorization ID, authorized
exact candidate SHA, human approver identity and timezone-aware timestamp. Runtime
requires:

`authorized_sha == certified_sha == deployed_sha`

The executable candidate first earns `PAY22_CONTROL_PLANE_CERTIFIED=PASS`. A
human must then explicitly authorize that exact SHA before the external release
system may record:

`PAY22_PRODUCTION_ACTIVATION_AUTHORIZED`

## Non-authorizations

PAY-22 certification does not itself authorize live credentials in CI, production
customer data in CI, live money movement in CI, live refund execution, production
deployment, or rollout beyond the separately authorized production stage.

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

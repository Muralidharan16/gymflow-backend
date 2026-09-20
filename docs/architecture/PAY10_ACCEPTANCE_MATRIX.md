# PAY-10 Acceptance Matrix

| ID | Gate | Required result |
|---|---|---|
| P10-A01 | Exact predecessor | PAY-10 is an exact descendant of certified PAY-9 SHA `581c3d83a1e78ba59213c90309166913fedf9c3e`. |
| P10-A02 | Alembic graph | Single head; PAY-10 directly revises `zs07d8e9f0a53`. |
| P10-A03 | Reduced identities | No runtime role becomes LOGIN, owner, SUPERUSER, BYPASSRLS, or gains migration-owner reachability. |
| P10-A04 | Direct DML denial | Refund/payment/credit-note/provider-evidence mutation remains capability-bound. |
| P10-A05 | Evidence immutability | Runtime UPDATE/DELETE of provider evidence is rejected. |
| P10-A06 | Refund authority | Provider call inputs are derived from Finance command/refund/payment truth; caller cannot choose payment or amount. |
| P10-A07 | Refundable balance | Full/partial/concurrent refunds cannot exceed applied refundable value. |
| P10-A08 | Credit-note gate | Terminal cash refund cannot exceed linked issued credit-note backing. |
| P10-A09 | Stable provider receipt | Same logical refund uses the same deterministic receipt across retry/recovery. |
| P10-A10 | Lost acknowledgement | Unknown submission result enters reconciliation and is not blindly re-issued under new identity. |
| P10-A11 | Duplicate provider response | Duplicate exact evidence is a no-op; conflicting duplicate identity fails closed. |
| P10-A12 | Webhook authenticity | Raw body HMAC verification occurs before parsing/business mutation. |
| P10-A13 | Webhook event identity | Duplicate provider event id is idempotent; changed replay is rejected. |
| P10-A14 | Out-of-order callbacks | Older/non-terminal callback cannot regress terminal success. |
| P10-A15 | Reconciliation | Provider fetch can close accepted/unknown outcomes without duplicate refund. |
| P10-A16 | Stale worker fence | Expired/reclaimed worker cannot acknowledge or finalize an effect. |
| P10-A17 | Provider success / DB ack loss | Replay/reconciliation produces one local terminal effect. |
| P10-A18 | Financial atomicity | Evidence, refund state, payment state, ledger and outbox terminal effects commit atomically. |
| P10-A19 | Ledger balance | Successful refund posts exactly one balanced cash/provider-clearing ledger entry. |
| P10-A20 | No double revenue reversal | Refund ledger does not repeat credit-note revenue/tax reversal. |
| P10-A21 | Payment state | Successful cumulative partial/full refunds derive `partially_refunded` / `refunded` deterministically. |
| P10-A22 | Provider rejection | Permanent rejection is durable and does not silently free reserved balance. |
| P10-A23 | Reversal contradiction | Contradictory post-success evidence is durable and cannot rewrite immutable history. |
| P10-A24 | Empty downgrade | PAY-10 -> PAY-9 restores predecessor relation/ACL graph. |
| P10-A25 | Populated downgrade | PAY-10 downgrade refuses when durable PAY-10 evidence/provenance exists. |
| P10-A26 | Predecessor preservation | Upgrade does not rewrite unrelated PAY-9/Finance tables. |
| P10-A27 | Inherited regression | General, Finance, migration lifecycle, preservation, adversarial and architecture suites pass. |
| P10-A28 | Test-mode only | No live Razorpay credentials, live provider environment, production money movement, merge, release or deploy. |
| P10-A29 | Exact candidate | Final decision binds one commit SHA and tree with every required gate green. |
| P10-B01 | Provider-neutral refund contract | Adapter input is command/refund/payment/provider-payment/amount/currency authority loaded from Finance truth. |
| P10-B02 | Deterministic provider receipt | Receipt is derived from durable command/refund identity and is stable across retry/recovery. |
| P10-B03 | Razorpay submit boundary | Test/sandbox only POST uses the exact Finance provider payment reference and amount. |
| P10-B04 | Conservative retry classification | Only known non-acceptance is retryable; timeout/network/ambiguous HTTP or response mismatch requires reconciliation. |
| P10-B05 | Exact reconciliation fetch | Known provider refund is fetched by payment id + refund id without issuing a second POST. |
| P10-B06 | Safe provider output | Raw response bodies, credentials, customer PII and provider-private payload fields do not leave the adapter. |
| P10-B07 | No Finance mutation | Provider adapter performs no Finance-table mutation; PAY-10-C/D remain authority for durable state and finalization. |

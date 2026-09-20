# PAY-10 Acceptance Matrix

| ID | Gate | Required result |
|---|---|---|
| P10-A01 | Exact predecessor | PAY-10 is an exact descendant of certified PAY-9 SHA `581c3d83a1e78ba59213c90309166913fedf9c3e`. |
| P10-A02 | Alembic graph | Single head; PAY-10-A/B revision `zt07d8e9f0a54` revises `zs07d8e9f0a53`, and PAY-10-C revision `zu07d8e9f0a55` revises `zt07d8e9f0a54`. |
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
| P10-C01 | Fenced claim | Due commands are claimed under a worker id and monotonically increasing lease fence. |
| P10-C02 | Expired reclaim | Expired processing work is reclaimable under a new fence without incrementing the same in-flight logical attempt. |
| P10-C03 | Server-authoritative request | Provider code/payment ref/amount/currency must exactly match locked Finance payment/refund/command truth. |
| P10-C04 | Stable request identity | Request SHA-256 is bound once and cannot drift across retry or reclaim. |
| P10-C05 | Refundable reservation gate | Active/successful refund reservations cannot exceed authoritative applied payment value. |
| P10-C06 | Stale fence rejection | Wrong/expired worker fence cannot bind, acknowledge, fail, or mark an unknown outcome. |
| P10-C07 | Known non-acceptance retry | Only known non-acceptance can enter retry_pending; bounded attempt/backoff rules remain in force. |
| P10-C08 | Unknown outcome | Ambiguous provider acceptance enters reconciliation_pending and is not blindly reclaimed/resubmitted. |
| P10-C09 | Active lease/reconciliation exclusion | Reconciliation cannot race a still-active execution lease. |
| P10-C10 | Submission evidence replay | Exact normalized submission evidence replay is a no-op; changed replay fails closed. |
| P10-C11 | External event replay | Exact provider event replay is a no-op; changed use of the same event identity fails closed. |
| P10-C12 | Out-of-order evidence | Pending/failed evidence arriving after processed evidence cannot erase the processed success candidate. |
| P10-C13 | Provider payment binding | External evidence must map to the Finance-owned provider payment reference and known refund identity. |
| P10-C14 | Processed is not final | Provider processed evidence stops at reconciliation_pending; C cannot set refund/command succeeded. |
| P10-C15 | No financial side effects | C does not issue credit notes, change payment refund status, post ledger entries, or emit financial outbox events. |
| P10-C16 | Least privilege | finance_refund_runtime and finance_reconciliation_runtime remain direct-table blind and receive only their exact app_secure capabilities. |
| P10-C17 | Capability lifecycle | Empty `zu07d8e9f0a55 -> zt07d8e9f0a54` downgrade removes PAY-10-C functions/ACLs while preserving A/B; full empty PAY-10 downgrade restores PAY-9. |

| P10-D01 | Issued credit-note provenance | Refund backing can reference only issued, numbered credit notes with posted balanced accounting reversal. |
| P10-D02 | Payment/invoice authority | Every backing credit note belongs to an invoice actually allocated to the same payment, organization, entity, division, brand and currency. |
| P10-D03 | Server-derived link amount | Link amount is computed from remaining refund backing and credit-note value; caller cannot choose the monetary amount. |
| P10-D04 | Exact backing gate | Terminal finalization requires aggregate immutable credit-note links to equal the refund amount exactly. |
| P10-D05 | Processed evidence gate | Finalization requires the exact processed provider evidence hash/ref bound to the durable command. |
| P10-D06 | Atomic terminal state | Refund succeeded, command succeeded/completed, payment refund state, ledger and outbox commit in one database transaction. |
| P10-D07 | Refund ledger | Exactly one posted refund ledger exists; it debits AR and credits PAYMENT_CLEARING for the refund amount. |
| P10-D08 | No double revenue reversal | Refund finalization never posts revenue/GST reversal; credit-note accounting remains sole authority. |
| P10-D09 | Payment state derivation | Cumulative successful refund total derives partially_refunded vs refunded and cannot exceed allocated or paid value. |
| P10-D10 | Exactly-once outbox | Refund completion, payment refund-state change and ledger-posted events each use deterministic logical idempotency. |
| P10-D11 | Replay safety | Re-finalization of a succeeded command returns the same ledger result and cannot duplicate ledger/outbox effects. |
| P10-D12 | Ledger uniqueness | A partial unique index prevents duplicate posted refund ledgers for the same refund. |
| P10-D13 | Least privilege | Only finance_refund_runtime can execute D capabilities; runtime identities remain direct-table blind. |
| P10-D14 | Reversible owner ACL delta | Any owner column authority added for D is recorded exactly and removed on empty downgrade. |
| P10-D15 | Populated downgrade | D -> C downgrade refuses after durable finalized refund effects exist. |

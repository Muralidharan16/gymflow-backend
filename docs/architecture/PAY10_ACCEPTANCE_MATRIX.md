# PAY-10 Acceptance Matrix

| ID | Gate | Required result |
|---|---|---|
| P10-A01 | Exact predecessor | PAY-10 is an exact descendant of certified PAY-9 SHA `581c3d83a1e78ba59213c90309166913fedf9c3e`. |
| P10-A02 | Alembic graph | Single head; PAY-10-A `zt07d8e9f0a54` directly revises `zs07d8e9f0a53`; later PAY-10 revisions are append-only exact successors and never rewrite a certified migration. |
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
| P10-C01 | Certified migration immutability | PAY-10-C adds `zu07d8e9f0a55` after certified `zt07d8e9f0a54`; zt content/identity is unchanged. |
| P10-C02 | Claim mode | Durable claim returns exactly one of `submit`, `discover`, or `fetch` from server-side command state. |
| P10-C03 | Per-lease preparation fence | Provider submission preparation binds the request hash to the live lease fence; stale/reclaimed prepared attempts cannot be blindly re-submitted. |
| P10-C04 | Known pre-send failure | Only a classified retryable/pre-send failure may return to submission retry. |
| P10-C05 | Unknown outcome | Timeout/network/ack ambiguity enters reconciliation and cannot return directly to provider submission. |
| P10-C06 | Lost refund id | Ambiguous attempt without a provider refund id uses payment+deterministic-receipt discovery. |
| P10-C07 | Known refund id | Reconciliation with a provider refund id uses specific refund fetch. |
| P10-C08 | Reconciliation bounds | Reconciliation attempts are separately counted, back off, and eventually dead-letter rather than loop forever. |
| P10-C09 | Evidence fencing | Provider evidence can be recorded only against the current live claim fence and exact request hash. |
| P10-C10 | Refund reservation | Provider failure/dead-letter never silently releases the Finance refund reservation. |

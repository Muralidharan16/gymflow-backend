# PAY-8 Durable Checkout and Webhook Recovery

**Certified PAY-7 predecessor:** \`3489f0841866e7ffcd01a16c753c3e382293e22e\`  
**PAY-7 tree:** \`b652d3a4ab09bf93c30a5834729cd189334b9d0f\`  
**Alembic predecessor:** \`zq07d8e9f0a51\`  
**PAY-8 head:** \`zr07d8e9f0a52\`

PAY-8 adds the durable provider-I/O boundary that PAY-7 intentionally deferred.

The phase does not make a provider authoritative for money. Finance Core remains
the only source of payment, allocation, ledger and entitlement-driving truth.

## Outbound provider-operation saga

Every checkout provider create is represented by
\`finance.provider_operations\` before network I/O.

The sequence is:

1. create/issue Finance invoice and checkout payment;
2. reserve a provider operation using tenant + provider + environment +
   operation + idempotency key + canonical request hash;
3. commit local authority;
4. claim the operation with a database lease/fence;
5. commit the \`in_flight\` state before the external POST;
6. perform sandbox/test provider I/O;
7. finish with one of \`succeeded\`, \`failed_retryable\`,
   \`failed_final\`, or \`unknown\`;
8. commit the provider outcome.

A successful finish atomically binds the provider order reference to the Finance
payment and stores a SHA-256 digest of normalized provider success evidence.

Provider operations are tenant-bound with a composite foreign key to
\`finance.payments(id, organization_id)\`.

## Provider success / database acknowledgement ambiguity

If the provider may have accepted a request but the local acknowledgement does
not commit, the durable operation remains \`in_flight\`.

An expired \`in_flight\` lease becomes \`unknown\`, never retryable.

\`unknown\` cannot be claimed by application checkout again. It may move only
through the bounded \`finance_reconciliation_runtime\` capability to:

- \`succeeded\` with a provider object and reconciliation evidence hash; or
- \`failed_final\` with a safe error code and reconciliation evidence hash.

PAY-8 therefore makes blind duplicate provider creation structurally impossible
after an ambiguous outbound result.

Actual provider lookup/reconciliation policy remains a later reconciliation
phase; PAY-8 provides the durable/fail-closed authority required by that phase.

## Retry and fencing

Only operations previously classified \`failed_retryable\` may be submitted
again. Every attempt receives a new monotonic lease fence.

A stale caller cannot finish a newer attempt because completion requires exact:

- operation id;
- tenant;
- \`in_flight\` status;
- lease owner;
- lease fence.

Retry exhaustion becomes \`failed_final\`.

## Durable verified webhook inbox

PAY-8 adds \`finance.provider_webhook_inbox\`.

The raw Razorpay signature is verified and the raw JSON is normalized before
anything is inserted. The database persists only:

- provider/environment/event id;
- SHA-256 payload digest;
- SHA-256 signature digest;
- normalized provider identifiers/status/amount/currency evidence;
- processing state and lease metadata.

Raw provider bodies and webhook secrets are not stored in the inbox.

The inbox row is committed before provider evidence is applied to Finance.

## Webhook replay

The provider event key is unique by
\`(provider_code, environment, provider_event_id)\`.

An exact duplicate is an idempotent replay.

The same event id with a different payload digest, signature digest or any
normalized evidence is rejected as a replay conflict. A provider event can
therefore never silently change meaning after first durable receipt.

## Webhook processing and crash recovery

Processing uses a fenced claim:

\`received/retry → processing → processed | retry | dead_letter\`.

The claim is committed before Finance mutation.

Finance provider evidence is then applied through the already-certified
\`confirm_finance_provider_evidence\` state machine. PAY-8 does not duplicate
payment-state logic.

Payment state, payment event, Finance outbox effect and inbox completion commit
together.

If the process crashes before that commit, the transaction rolls back while the
already-committed inbox claim remains. After lease expiry,
\`finance_payment_runtime\` can reclaim it through
\`claim_next_finance_provider_webhook\`.

Every reclaim receives a new lease fence; a stale worker cannot complete/fail
the newer attempt.

Retry exhaustion becomes \`dead_letter\`.

## Authority boundaries

\`app_runtime\` can invoke bounded request-path capabilities but has no direct
DML on either PAY-8 table.

\`finance_payment_runtime\` can claim/recover/complete/fail webhook inbox work
but has no direct table DML.

\`finance_reconciliation_runtime\` can reconcile only \`unknown\` outbound
operations and has no direct table DML.

\`worker_runtime\` receives no PAY-8 capability.

All SECURITY DEFINER functions are owned by \`app_security_owner\`; PAY-8 tables
remain owned by \`migration_owner\`.

## Migration policy

PAY-8 is one additive migration:

\`zq07d8e9f0a51 → zr07d8e9f0a52\`.

It creates two new Finance relations and bounded functions. It does not backfill
or rewrite predecessor business tables.

The phase requires:

- no predecessor relfilenode rewrite on upgrade;
- exact predecessor ACL restoration on empty downgrade;
- populated downgrade failure if any durable PAY-8 evidence exists;
- re-upgrade equivalence.

## Deliberately out of scope

PAY-8 does not authorize or implement:

- live provider mode;
- live money movement;
- live credentials;
- production customer checkout activation;
- provider refund execution;
- settlement accounting;
- general reconciliation engine;
- disputes/dunning/recurring billing.

## Terminal markers

\`\`\`text
PAY8_DURABLE_PROVIDER_OPERATION=PASS
PAY8_UNKNOWN_OUTCOME_FENCING=PASS
PAY8_PROVIDER_RECONCILIATION=PASS
PAY8_WEBHOOK_INBOX=PASS
PAY8_WEBHOOK_REPLAY_FENCING=PASS
PAY8_WEBHOOK_CRASH_RECOVERY=PASS
PAY8_DIRECT_DML_DENIAL=PASS
PAY8_MIGRATION_LIFECYCLE=PASS
PAY8_PAY7_INHERITED=PASS
PAY8_LIVE_PROVIDER=DISABLED
PAY8_LIVE_MONEY_MOVEMENT=DISABLED
PAY8_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
PAY8_FINAL=PASS
\`\`\`

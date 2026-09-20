# PAY-10 — Refund and Credit-Note Provider Execution

## Status

Implementation phase. PAY-10 inherits the exact certified PAY-9 candidate
`581c3d83a1e78ba59213c90309166913fedf9c3e` and does not authorize merge,
release, deployment, live provider credentials, or production money movement.

## Purpose

PAY-10 closes the refund-provider gap deliberately left fail-closed by P4D and
PAY-9.  It connects the durable refund obligation and execution-command model
to provider submission, durable evidence, webhook/reconciliation recovery, and
accounting finalization without creating a second refund source of truth.

The core rule is:

```
Finance refund obligation
  -> durable execution command
  -> bounded provider attempt identity
  -> provider evidence
  -> reconciliation when ambiguous
  -> accounting finalization
  -> durable Finance outbox
```

A request submission, HTTP 2xx, browser callback, task success, queue
acknowledgement, or operator assertion is never by itself proof that money was
refunded.

## Authority boundaries

1. `finance.refunds` remains the refund business obligation.
2. `finance.refund_execution_commands` remains the durable execution state
   machine introduced by P4D.
3. PAY-10 adds append-only provider evidence rather than overwriting history.
4. Runtime roles remain table-blind.  All Finance mutation is through bounded
   `app_secure` capabilities.
5. A caller cannot nominate a different payment, organization, amount,
   currency, provider payment reference, or entitlement target.
6. Refund amount remains bounded by Finance allocation/refund truth.
7. The same logical provider receipt/idempotency identity survives retry,
   redelivery, process crash, and reconciliation.
8. Ambiguous provider outcomes move to reconciliation; they are not blindly
   re-submitted under a new identity.
9. Webhook evidence is accepted only after signature verification and exact
   mapping to pre-existing Finance/provider identities.
10. Duplicate and out-of-order callbacks are replay-safe and cannot regress a
    terminal state.

## Credit-note and accounting rule

Provider refund success and statutory/accounting correction are separate
facts.  PAY-10 must not double-reverse revenue.

For an invoiced payment, cash-refund finalization requires sufficient issued
credit-note backing linked to the refund.  Credit notes reverse revenue/tax and
credit AR.  The successful provider cash refund then debits AR and credits
`PAYMENT_CLEARING`, closing the customer credit without reversing revenue a
second time.

PAY-10 therefore introduces explicit refund-to-credit-note provenance.  A
provider result alone cannot manufacture a statutory credit note or bypass
credit-note numbering/issuance authority.

Multiple credit notes may back one refund when a payment spans multiple
invoices.  A single credit note may back only one refund.  Link amounts must be
positive and the aggregate backing must equal the cash refund before terminal
success.

## Provider contract

PAY-10 is provider-neutral at the Finance boundary and initially implements
Razorpay test/sandbox behavior only.

For Razorpay, the provider payment identifier is loaded from Finance truth.
The refund request amount is sent in currency subunits and the deterministic
Finance receipt is reused for every attempt of the same logical refund.  The
receipt is derived from the durable Finance command/refund identity, never from
browser input.  A duplicate provider receipt is treated as an
idempotency/reconciliation signal, not permission to create a second refund.

PAY-10-B implements only the Razorpay test/sandbox adapter boundary:

- submit: `POST /v1/payments/:payment_id/refund`;
- reconcile: `GET /v1/payments/:payment_id/refunds/:refund_id`;
- request metadata contains Finance UUIDs only, with no customer PII, secrets,
  authorization material, or raw provider payload;
- the adapter returns normalized/redacted provider facts and performs no
  Finance database mutation;
- an HTTP 400 from refund submission is conservatively ambiguous because a
  duplicate deterministic receipt may represent a previously accepted refund;
- only a failure known to occur before provider acceptance is automatically
  retryable. Timeouts, post-connect network failure, malformed responses and
  response mismatches require reconciliation.

Normalized provider states:

- `pending` -> accepted but not terminal; reconcile.
- `processed` -> authoritative success candidate.
- `failed` -> authoritative failure evidence.

Network timeout, connection reset after request write, malformed/partial
response, and any response where acceptance cannot be disproved are
`unknown` and must enter reconciliation.

## State machine

```
pending/retry_pending
  -> processing
  -> provider_accepted
  -> reconciliation_pending
  -> succeeded

processing
  -> retry_pending       only when non-acceptance is known
  -> reconciliation_pending  when outcome is unknown
  -> rejected/dead_lettered  on bounded terminal failure

provider_accepted
  -> reconciliation_pending
  -> succeeded
  -> rejected/dead_lettered
```

A stale worker whose lease fence no longer matches cannot record provider
acceptance, failure, or success.

## Evidence model

PAY-10 provider evidence stores only normalized/redacted facts:

- command/refund/payment/organization identity
- provider code
- provider event id when webhook-originated
- provider refund reference when known
- evidence source: submission, webhook, reconciliation
- normalized provider state
- request SHA-256
- evidence SHA-256
- provider occurrence timestamp when available
- local recording timestamp

Raw webhook bodies, API secrets, authentication headers, card data, and
sensitive provider payloads are not stored in Finance tables.

Provider evidence is immutable for runtime identities.

## Refundable-balance invariants

Before every provider attempt and every terminal success transition:

- refund, command, payment, organization and currency must agree;
- payment must have a provider payment reference;
- payment must remain eligible for refund processing;
- total non-cancelled refund reservations must not exceed allocated payment
  value;
- this refund amount must exactly match its durable execution command;
- terminal success must not exceed the successful-refundable balance;
- sufficient issued credit-note backing must exist.

Concurrent attempts serialize on the durable command/payment authority.

## PAY-10-C execution and reconciliation boundary

PAY-10-C connects the durable P4D execution command to provider execution
without giving provider results financial-finalization authority.

The bounded capability split is:

- `finance_refund_runtime` may claim a due refund command, bind the exact
  server-authoritative provider request, record normalized submission evidence,
  move an ambiguous result to reconciliation, or record a known
  non-acceptance/failure.
- `finance_reconciliation_runtime` may record only normalized webhook or
  reconciliation evidence for a previously bound command.
- Neither runtime identity receives direct Finance table DML.
- Every mutating execution acknowledgement from a worker requires the exact
  current worker id + lease fence and an unexpired processing lease.
- Expired processing work may be reclaimed under a new fence. The old fence
  cannot bind, acknowledge, retry, or otherwise mutate the command.
- A reclaimed in-flight attempt does not increment the logical attempt count;
  a fresh retry does.
- The provider request SHA-256 is bound once and must remain identical across
  retries. A known provider refund reference forbids provider resubmission and
  forces reconciliation.
- Only known provider non-acceptance may become `retry_pending`. Unknown
  acceptance, timeout, post-connect network ambiguity, malformed response, or
  otherwise indeterminate outcome becomes `reconciliation_pending`.
- Active worker leases block reconciliation from racing the executing worker.
  Reconciliation can proceed only after the command is no longer actively
  leased.
- Exact provider evidence replay is a no-op. Changed replay of the same
  evidence hash or provider event id fails closed.
- Once processed provider evidence exists, later pending/failed callback
  ordering cannot erase that fact; the command remains
  `reconciliation_pending` for PAY-10-D financial-finalization checks.
- Provider state `processed` is **not** local refund success in PAY-10-C.
  PAY-10-C does not mark the refund or command `succeeded`, post ledger
  entries, issue credit notes, alter payment refund state, or emit Finance
  financial outbox events.

The request/provider network call remains outside the database transaction.
The service layer wraps only the short database capability calls and does not
commit by itself. This avoids holding Finance locks across provider latency.

## Financial finalization

Terminal processed evidence is applied once:

1. persist provider evidence;
2. mark the refund `succeeded`;
3. mark the execution command `succeeded`;
4. recompute the payment refund state from successful refunds:
   `partially_refunded` or `refunded`;
5. post exactly one balanced refund ledger entry;
6. emit exactly-once logical Finance outbox events for refund completion,
   payment refund-state change, and ledger posting;
7. commit atomically.

The refund ledger is a cash/provider clearing movement only.  Revenue and tax
reversal remain credit-note responsibility.

## Failure and reversal handling

A deterministic provider rejection is durable evidence and is surfaced as a
terminal exception state.  It does not silently release the refund reservation.

If later authoritative evidence contradicts an earlier non-terminal result,
reconciliation may advance the command.  Terminal success never regresses by
ordinary callback ordering.

A provider-side reversal discovered after local success requires a compensating
financial event and is not implemented as history rewrite.  PAY-10 records the
contradictory evidence and routes it to the explicit reversal/recovery path;
PAY-13 remains the authority for dispute/chargeback semantics.

## Migration safety

PAY-10 uses one successor revision after `zs07d8e9f0a53`.

Upgrade rules:

- reduced `migration_owner` identity only;
- no role creation or membership mutation;
- bounded lock/statement timeouts;
- no predecessor table rewrite for schema-only changes;
- new RLS tables are FORCE RLS;
- runtime roles receive no direct table privilege;
- temporary owner-context privileges are removed before migration exit.

Downgrade rules:

- empty PAY-10 state must return exactly to PAY-9;
- downgrade refuses when PAY-10 provider evidence, refund-credit-note
  provenance, credit-note series state, or PAY-10 execution metadata exists;
- no CASCADE is used to hide dependency loss.

## PAY-10 slices

- **A — authority/evidence schema:** durable provider evidence, credit-note
  provenance, command attempt identity, migration/security contracts.
- **B — provider adapter:** sandbox/test refund submit/fetch with deterministic
  receipt and classified failure semantics.
- **C — execution/reconciliation capabilities:** fenced claim, acceptance,
  unknown-outcome recovery, webhook/reconciliation evidence.
- **D — financial finalization:** refund/payment state, cash ledger, outbox,
  credit-note gate, replay fencing.
- **E — worker and crash recovery:** lease expiry, redelivery, DB-ack failure,
  provider-success/local-ack ambiguity.
- **F — exact-head certification:** PG16 runtime, migration lifecycle,
  adversarial safety, Finance/general regressions, identity attestation.

## Deferred

PAY-10 does not authorize or implement production/live provider enablement,
platform recurring billing, mandates/dunning, chargeback adjudication,
treasury closure, or production activation.  Those remain later PAY phases.

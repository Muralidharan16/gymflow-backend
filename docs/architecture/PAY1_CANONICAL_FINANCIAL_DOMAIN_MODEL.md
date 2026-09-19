# PAY-1 Canonical Financial Domain Model

**Phase:** PAY-1  
**Date:** 2026-09-19  
**Branch:** `hardening/pay1-canonical-financial-domain-model`  
**Certified PAY-0 parent:** `a9fa1c521bcd1685ee822d332eeaf87c7cf5554a`  
**PAY-0 tree:** `724671aecdcc2d322c405855a26d93be1cd8968c`  
**Alembic head:** `zk07d8e9f0a45`

## Goal

PAY-1 freezes the enterprise financial vocabulary and legal state machines before
PAY-2 introduces database enforcement. This phase deliberately avoids Alembic
changes and avoids wiring the new model into live request/worker behavior.

The target is a model where each fact has exactly one meaning.

## Core separation

The old anti-pattern is one overloaded payment status trying to describe:

- provider acquisition;
- bank settlement;
- refund progress;
- dispute/chargeback effects.

PAY-1 forbids that design.

The canonical model separates:

```text
Payment acquisition
Payment attempt/execution
Provider evidence
Invoice lifecycle
Invoice due state
Payment allocation/reversal
Settlement
Credit note
Refund intent
Refund execution
Dispute
Chargeback
Manual adjustment
Provider operation
```

A successfully captured payment remains **captured** even when it is later
settled, refunded, disputed, charged back, or reversed by a separate financial
event. Those later facts are represented by their own immutable histories.

## Payment acquisition state

```text
created
  ├─> pending
  ├─> requires_action
  ├─> authorized
  ├─> captured
  ├─> failed
  ├─> cancelled
  └─> expired

pending / requires_action
  ├─> authorized
  ├─> captured
  ├─> failed
  ├─> cancelled
  └─> expired

authorized
  ├─> captured
  ├─> cancelled
  └─> expired

captured = terminal acquisition truth
```

There is deliberately no `captured -> refunded` or
`captured -> settled` transition.

## Payment-attempt state

Payment attempts represent provider execution, not business entitlement.

`unknown` means the provider effect may have happened and local evidence is
insufficient. It must be reconciled before a replacement external effect is
allowed unless a provider-supported idempotency guarantee proves replacement safe.

Retryability belongs to an attempt outcome. A retry creates a new attempt under
the same logical payment command/idempotency lineage instead of rewriting a
failed attempt.

## Provider evidence

Provider evidence is append-only and classified as:

- `verified`;
- `rejected`;
- `unresolved`.

A provider event is not considered stale merely because its mapped status appears
"earlier" in an enum. Staleness requires authoritative provider version,
effective timestamp, sequence, object version, or equivalent evidence.

## Invoice lifecycle

Canonical invoice lifecycle:

```text
draft -> issued -> partially_paid -> paid
   \-> voided
issued -> cancelled
issued/partially_paid/paid -> credited
```

`overdue` is not a canonical lifecycle mutation. It is derived from:

- due date;
- business time zone;
- current outstanding amount;
- cancellation/credit state.

Issued statutory document facts are immutable. Corrections use controlled
status transitions and/or linked corrective documents such as credit notes.

## Allocations

Payment allocations are append-only.

Reversal never deletes or edits the original financial history.

```text
applied -> partially_reversed -> reversed
```

Total reversals may never exceed the original allocation.

## Settlement

Settlement describes provider-to-bank/treasury evidence and never activates a
subscription.

```text
reported -> verified -> partially_reconciled -> reconciled
       \-> discrepant -> verified/partially_reconciled/reconciled
```

A later provider debit/reversal is represented as a new linked settlement with
kind `debit_adjustment` or `reversal`; a reconciled record is not rewritten.

## Refund model

Refund approval and provider execution are separate.

Business refund:

```text
requested -> approved -> processing -> succeeded
    |           |            \-> failed
    |           \-> cancelled
    ├-> rejected
    \-> cancelled
```

Refund execution owns lease/retry/provider ambiguity states, including
`retry_pending`, `provider_accepted`, `reconciliation_pending`,
`unknown`, `dead_lettered`, and terminal provider outcomes.

A worker lease does not grant refund approval.

## Dispute and chargeback model

Disputes and chargebacks do not rewrite the historical fact that the original
payment was captured.

A dispute records the provider/customer adjudication lifecycle.

A chargeback records the actual financial debit/reversal consequence.

This allows the system to explain:

```text
payment captured on day 1
dispute opened on day 20
chargeback debited on day 25
dispute won on day 40
chargeback reversed on day 43
```

without lying about the original payment.

## Manual adjustments

Manual adjustments require a proposed/approved/post lifecycle. Once posted, the
financial effect is immutable. Reversal uses a new linked adjustment rather than
editing the posted record.

PAY-2/PAY-16 will later enforce maker-checker roles, step-up authentication and
database authority.

## Money representation

Business-domain money uses `Decimal`.

Provider transport uses integer minor units.

Floats are forbidden.

PAY-1 freezes INR as the only production currency allowlisted by this canonical
module:

```text
INR -> 2 minor-unit decimal places
```

Adding a currency is a reviewed contract change because tax, rounding,
settlement and provider support must all be certified.

Cross-currency allocation/refund/credit/settlement operations are forbidden.

## Existing Finance compatibility

The current Finance schema is not changed in PAY-1.

Existing payment rows map as:

```text
created            -> created
pending            -> pending
authorized         -> authorized
captured           -> captured
failed             -> failed
cancelled          -> cancelled

settled            -> captured + separate settlement projection
partially_refunded -> captured + separate refund projection
refunded           -> captured + separate refund projection
```

Existing invoice `overdue` becomes a derived due-state concept in the target
model.

Existing Platform Billing provider-operation `failed` cannot be blindly mapped
to retryable/final. PAY-2/PAY-11 must classify legacy rows from authoritative
evidence.

## PAY-1 migration rule

PAY-1 adds **zero Alembic revisions**.

The expected single Alembic head remains `zk07d8e9f0a45`.

PAY-2 may introduce additive schema support only after it produces:

- exact predecessor/head guards;
- data compatibility mapping;
- populated migration rehearsal;
- downgrade/re-upgrade proof where supported;
- ACL/RLS/ownership preservation;
- lock/rewrite analysis;
- rolling-version compatibility;
- backup/restore/PITR compatibility.

## Hard stops

PAY-1 fails if it:

- changes any Alembic revision;
- modifies an existing runtime money-moving implementation;
- imports/wires the canonical PAY-1 module into API/worker/provider code;
- enables live provider credentials;
- enables live checkout/webhook money authority;
- activates refund provider execution;
- weakens P1-P10/PAY-0 migration/security gates;
- authorizes merge, release or deployment.

## Required markers

```text
PAY1_CANONICAL_FINANCIAL_MODEL=PASS
PAY1_STATE_MACHINE_SEPARATION=PASS
PAY1_MONEY_CONTRACT=PASS
PAY1_COMPATIBILITY_MAPPING=PASS
PAY1_MIGRATION_BASELINE=PASS
PAY1_PAY0_INHERITED=PASS
PAY1_LIVE_MONEY_MOVEMENT=DISABLED
PAY1_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
PAY1_FINAL=PASS
```

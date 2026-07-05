# PHASE 5A — VITARA FINANCE CORE + RAZORPAY CONTRACT FREEZE REPORT

## 3. Master Data Model

Finance Core owns the canonical finance master data required to issue invoices, calculate GST, reconcile payments, and generate audit-safe financial records. Product modules such as Doers and FreshBite must reference Finance Core master data instead of passing free-form tax, buyer, seller, or jurisdiction values directly into invoice calculation.

### `finance.billing_parties`

`finance.billing_parties` is the required buyer/customer tax identity and jurisdiction source for invoices.

Product modules must not pass free-form buyer tax jurisdiction directly into invoice calculation. They must either reference an existing billing party or create/update one through Finance Core before invoice creation.

| Column | Type | Required | Notes |
| ------ | ---: | -------: | ----- |
| `id` | uuid | yes | Primary key. |
| `legal_entity_id` | uuid | yes | Legal entity that owns this billing relationship. |
| `brand_id` | uuid | yes | Brand/product module this billing party belongs to, such as Doers or FreshBite. |
| `billing_name` | text | yes | Buyer/customer name printed on invoice. |
| `address_line1` | text | yes | Billing address line 1. |
| `address_line2` | text | no | Billing address line 2. |
| `city` | text | yes | Billing city. |
| `state` | text | yes | Billing state. |
| `pincode` | text | yes | Billing postal code. |
| `country` | text | yes | Billing country. |
| `gstin` | text | no | Nullable. Required only when buyer is valid GST-registered B2B customer. |
| `pan` | text | no | Nullable PAN for buyer identity/tax reporting support. |
| `place_of_supply_state_code` | text | yes for India GST invoices | Buyer place-of-supply state code used for GST jurisdiction. |
| `party_type` | enum | yes | `individual` or `business`. |
| `is_active` | boolean | yes | Inactive parties cannot be used for new invoice issue. |
| `created_at` | timestamptz | yes | Creation timestamp. |
| `updated_at` | timestamptz | yes | Last update timestamp. |

Recommended constraints:

- `party_type` must be one of `individual`, `business`.
- `gstin` must pass GSTIN format validation when present.
- `pan` must pass PAN format validation when present.
- `place_of_supply_state_code` is required for India GST invoices.
- A billing party used on an invoice must belong to the same `legal_entity_id` and `brand_id` as the invoice.
- Inactive billing parties cannot be used to issue new invoices.

Place of supply rule:

```text
supplier_state_code = seller GST registration state code
place_of_supply_state_code = billing party place of supply

same state       -> CGST + SGST
different state  -> IGST
```

B2B/B2C classification:

- Valid GSTIN present = B2B for GST reporting.
- GSTIN missing = B2C.
- GSTIN invalid = not B2B; Finance Core must either reject invoice issue or classify as B2C depending on configured strictness.
- `party_type = business` alone does not make the invoice B2B.
- PAN alone does not make the invoice B2B.
- Issued invoice must snapshot GSTIN and B2B/B2C classification.
- Later billing party changes must not mutate historical issued invoices.

## 4. Invoice Numbering and Statutory Sequence Contract

Invoice numbering is statutory and immutable once allocated.

GST invoice number format rules:

- Invoice number must be a consecutive serial number.
- Invoice number must be unique for the financial year.
- Invoice number must not exceed 16 characters.
- Configured series may contain alphabets, numerals, hyphen/dash, slash, or legally permitted configured combinations.
- Sequence configuration must be controlled by Finance Core and audit logged.

Rules:

1. An invoice number, once allocated, must never be reused, reassigned, recycled, or renumbered.
2. This applies even if the invoice is later cancelled, voided, corrected, or unpaid.
3. Cancelled/voided invoices retain their original invoice number.
4. Cancelled/voided invoices are marked with terminal status, not deleted.
5. Gaps in the visible invoice sequence are legally acceptable when every allocated number is accounted for.
6. "Avoid gaps where possible" does not mean reusing numbers.
7. Every allocated number must be explainable in the audit trail.

An explainable gap means the audit log can show:

- invoice number
- invoice ID
- issued/cancelled/voided status
- cancellation or void reason
- timestamp
- actor/system process
- linked original invoice reference when applicable
- audit event ID

Statutory correction rules:

```text
Cancelled invoice = same invoice number + cancelled status.
Replacement invoice = new invoice number.
Credit note = separate statutory number.
No reuse.
No silent deletion.
No renumbering.
```

## 5. Invoice Engine Contract

Every invoice creation request must include or resolve:

- `legal_entity_id`
- `brand_id`
- `billing_party_id`
- invoice type
- currency
- line items
- supply date
- idempotency key

Finance Core must load and validate `finance.billing_parties` before tax calculation.

Validation requirements:

- billing party exists
- billing party is active
- billing party belongs to the same legal entity
- billing party belongs to the same brand
- place of supply exists and is valid
- GSTIN/PAN are normalized and validated

At issue time, Finance Core must snapshot both seller registration data and buyer billing party data into the invoice.

Seller snapshot requirements:

- seller legal name
- seller GSTIN
- seller PAN
- seller registered address
- seller state code

Buyer billing party snapshot requirements:

- billing name
- address
- GSTIN
- PAN
- party_type
- place_of_supply_state_code
- GST reporting type: B2B or B2C

Historical invoices must not depend on mutable master data. Later changes to legal entity, seller GST registration, brand, or billing party records must not mutate issued invoice snapshots.

Line-item tax calculation must reference:

```text
seller_state_code = finance.tax_registrations.state_code
buyer_place_of_supply_state_code = finance.billing_parties.place_of_supply_state_code
```

For each invoice line, persist:

- taxable amount
- GST rate
- CGST rate/amount
- SGST rate/amount
- IGST rate/amount
- total tax amount
- line total amount
- place_of_supply_state_code snapshot
- tax_jurisdiction_type:
  - `intra_state`
  - `inter_state`

Rules:

- Product modules must not calculate CGST/SGST/IGST as source of truth.
- Finance Core must recalculate statutory taxes.
- Invalid GSTIN must never silently create a B2B invoice.
- Missing GSTIN means B2C reporting.
- Business without GSTIN is still B2C for GST reporting.
- PAN-only is B2C for GST reporting.
- Issued invoice classification is immutable unless corrected through a controlled finance workflow.

Invoice lifecycle states:

- `draft`
- `issued`
- `partially_paid`
- `paid`
- `overdue`
- `cancelled`
- `voided`
- `credited`

Lifecycle rules:

- Cancellation is terminal on the same invoice number.
- Voiding is terminal on the same invoice number.
- Cancelled/voided invoices are not deleted.
- Cancelled/voided invoice numbers are never reused.
- Replacement invoice gets a new invoice number.
- Credit note gets its own statutory number.
- Cancellation must record reason, timestamp, actor, and audit event.

## 9A. Finance-to-Product Outbox Events

Razorpay webhook must never directly activate subscriptions, unlock entitlements, or mutate product modules.

Webhook responsibility is only:

1. receive provider event
2. verify signature
3. validate provider payload server-side
4. reconcile Finance Core payment/order/invoice state
5. persist finance state
6. insert durable outbox event

Product modules such as Doers and FreshBite consume validated finance events later.

This uses the existing outbox/saga pattern already established in Gymflow branch management work, now applied to Finance Core event delivery. The purpose is reliable, durable, retryable, auditable cross-module state transfer.

Finance Core owns `finance.outbox_events`. Product modules must not freely mutate Finance Core records. Outbox state transitions must be owned by an approved Finance Core dispatcher, worker, or acknowledgement API.

Approved ownership model:

```text
Finance Core inserts pending event
Finance dispatcher atomically claims event
Finance dispatcher delivers event to product module
Product module records consumption in its own consumed-event table
Product module applies idempotent state change
Product module returns acknowledgement
Finance dispatcher or Finance Core ack API marks event processed
```

### `finance.outbox_events`

| Column | Type | Required | Notes |
| ------ | ---: | -------: | ----- |
| `id` | uuid | yes | Primary key and finance event identity. |
| `event_type` | text | yes | Examples: `payment_verified`, `invoice_issued`, `refund_completed`, `credit_note_issued`, `payment_failed`. |
| `payload` | jsonb | yes | Redacted event payload for product module consumption. |
| `brand_id` | uuid | yes | Brand/product namespace for event routing. |
| `linked_payment_id` | uuid | no | Nullable depending on event type. |
| `linked_invoice_id` | uuid | no | Nullable depending on event type. |
| `status` | enum | yes | `pending`, `processed`, `failed`. |
| `idempotency_key` | text | yes | Stable event idempotency key for consumer dedupe. |
| `retry_count` | integer | yes | Number of processing attempts. |
| `locked_at` | timestamptz | no | Claim timestamp for active processing attempt. |
| `locked_by` | text | no | Worker/dispatcher identity holding the claim. |
| `next_attempt_at` | timestamptz | no | Earliest time event may be retried. |
| `last_error` | text | no | Redacted last failure reason. |
| `created_at` | timestamptz | yes | Event creation timestamp. |
| `processed_at` | timestamptz | no | Timestamp when successfully processed. |

Polling/concurrency rules:

- Pending events must be claimed atomically using row-level locking, `SKIP LOCKED`, or equivalent.
- Two workers must not process the same pending event concurrently.
- Claimed events must record `locked_at` and `locked_by`.
- Failed/transient events must use `next_attempt_at` and `retry_count` before reprocessing.
- Stale locks must be recoverable by Finance Core policy.
- Product modules must not bypass claim/ack flow by directly updating finance tables.

Product module consumption flow:

```text
Finance Core inserts pending outbox event
Finance dispatcher claims pending event atomically
Product module checks its consumed-event table
Product module applies idempotent state change
Product module records consumed finance_event_id/idempotency_key
Product module acknowledges success
Finance Core marks event processed
```

| Event | Product action |
| ----- | -------------- |
| `payment_verified` | activate subscription, unlock entitlement, mark order paid |
| `invoice_issued` | store/display invoice reference |
| `refund_completed` | update refund/account/subscription state |
| `credit_note_issued` | adjust account/billing view |
| `payment_failed` | keep subscription pending, trigger retry/dunning |

Wrong pattern:

```text
Razorpay webhook -> directly activate subscription
```

Correct pattern:

```text
Razorpay webhook
 -> verify and validate
 -> record Finance Core state
 -> insert finance.outbox_events
 -> commit
 -> Finance dispatcher claims event
 -> product module consumes event
 -> product module idempotently activates subscription
 -> Finance Core marks event processed after acknowledgement
```

Replay and idempotent consumption rules:

- Delivery is at-least-once, not exactly-once.
- Product modules must maintain consumed-event records.
- Same `finance_event_id` or `idempotency_key` must be no-op on duplicate.
- Already-active subscription from same event must be no-op success.
- Entitlement changes must not apply twice.
- Failed events may be retried.
- Replay must not duplicate payments, invoices, subscriptions, or entitlements.

## 10A. Idempotency for Direct Finance Core Service Calls

Webhook idempotency alone is not enough. Product modules will directly call Finance Core during normal business flows.

Every mutating Finance Core service method must require a caller-supplied `idempotency_key`.

Applies to:

- `create_invoice`
- `record_payment`
- `allocate_payment`
- `issue_refund`
- `issue_credit_note`
- `post_ledger_entry`

Read-only methods do not require idempotency keys.

Caller-supplied key format:

```text
{module}:{operation}:{business_reference}:{attempt_scope}
```

Examples:

```text
doers:create_invoice:subscription_123:billing_cycle_2026_07
freshbite:record_payment:order_456:razorpay_payment_abc
doers:issue_refund:invoice_789:refund_request_001
```

Rules:

- Key must be stable across retries of the same logical operation.
- Key must be unique across different operations.
- Key must not contain secrets.
- Mutating call without key must be rejected.
- Finance Core stores request fingerprint and result reference.
- Idempotency records must not expire before all caller retry windows and queue replay windows are exhausted.

Minimum retention:

- Minimum retention is 24 hours for all mutating calls.
- Longer retention, financial-audit-safe retention, or no automatic expiry until archival is recommended for invoices, refunds, credit notes, and ledger entries.
- Finance operations must not use short idempotency windows that could allow duplicate statutory or ledger side effects after delayed retries.

### `finance.idempotency_keys`

| Column | Type | Required | Notes |
| ------ | ---: | -------: | ----- |
| `id` | uuid | yes | Primary key. |
| `idempotency_key` | text | yes | Caller-supplied idempotency key. |
| `caller_module` | text | yes | Product module, such as `doers` or `freshbite`. |
| `operation` | text | yes | Mutating Finance Core operation name. |
| `payload_hash` | text | yes | Hash of canonicalized request payload. |
| `status` | enum | yes | `processing`, `completed`, `failed`. |
| `result_type` | text | no | Type of created/returned result. |
| `result_id` | uuid | no | ID of created/returned result. |
| `response_snapshot` | jsonb | no | Safe response snapshot for successful replay. |
| `error_snapshot` | jsonb | no | Safe deterministic error snapshot. |
| `expires_at` | timestamptz | yes | Retention expiry timestamp for idempotency replay protection. |
| `created_at` | timestamptz | yes | Creation timestamp. |
| `updated_at` | timestamptz | yes | Last update timestamp. |

Recommended uniqueness:

```text
UNIQUE (caller_module, operation, idempotency_key)
```

Retry behavior:

1. Canonicalize request payload.
2. Hash payload.
3. Look up key by caller module, operation, idempotency key.
4. If no record exists, create `processing`.
5. If completed with same hash, return original result.
6. If processing with same hash, return safe in-progress/retry response or block according to policy.
7. If deterministic failed with same hash, return original deterministic error where appropriate.
8. If same key exists with different payload hash, return conflict error.

Expected behavior:

```text
same key + same payload = return original result, no duplicate side effect
same key + different payload = 409 conflict, no side effect
missing key = reject mutating request
```

Conflict error contract:

```text
HTTP 409 Conflict
error_code = IDEMPOTENCY_KEY_PAYLOAD_MISMATCH
message = This idempotency key was already used with a different request payload.
```

Why this matters for Doers and FreshBite:

Product modules may retry because of:

- network timeout
- frontend retry
- backend redeploy
- worker restart
- database failover
- queue redelivery
- product module crash after Finance Core committed but before response was received

Without idempotency:

- duplicate invoice
- duplicate payment
- duplicate refund
- duplicate ledger entry

With idempotency:

- retry returns original result
- no duplicate side effects
- caller can safely recover from uncertain mid-request failures

# PHASE 5B — VITARA FINANCE CORE IMPLEMENTATION PLAN

## 1. Purpose and Boundaries

Phase 5B implements the Vitara Finance Core foundation defined by the frozen Phase 5A contract. This plan is implementation guidance only; actual code, migrations, and commits require separate approval.

Core outcomes:

- Create Finance Core persistence for billing parties, invoices, invoice lines, idempotency, and outbox delivery.
- Make Finance Core the source of truth for buyer tax jurisdiction, seller/buyer invoice snapshots, GST line split, invoice numbering, and finance-to-product events.
- Preserve the Razorpay boundary: webhooks validate and persist finance state only; they must not directly activate subscriptions or mutate product modules.
- Require product modules such as Doers and FreshBite to consume finance events idempotently.

Non-goals for Phase 5B:

- No real Razorpay SDK integration unless explicitly authorized in a later Razorpay adapter phase.
- No direct subscription activation from provider webhooks.
- No frontend payment behavior changes unless explicitly authorized.
- No production enforcement changes.

## 2. Implementation Phases and Approval Gates

### Phase 5B.1 — Schema Foundation

Checklist:

- Add `finance` schema if it does not already exist.
- Add `finance.billing_parties`.
- Add invoice core tables for invoices, invoice lines, invoice numbering/audit, and statutory snapshots.
- Add `finance.idempotency_keys`.
- Add `finance.outbox_events`.
- Add product-consumption support contract, either as product-owned consumed-event tables or approved Finance Core acknowledgement API requirements.
- Add constraints for GSTIN/PAN shape, party type, invoice status, outbox status, idempotency status, and invoice number uniqueness.

Likely files:

- Alembic migration under `alembic/versions/`.
- Finance ORM models under an `app/finance/` or equivalent Finance Core package.
- Model registration imports where the application currently registers ORM metadata.

Approval gate:

- Migration diff reviewed before implementation.
- No application behavior enabled by default.
- No provider SDK or webhook route added in this phase.

### Phase 5B.2 — Master Data and Validation Services

Checklist:

- Implement Finance Core service boundary for creating/updating/reading billing parties.
- Normalize and validate GSTIN and PAN.
- Enforce `legal_entity_id` and `brand_id` ownership.
- Reject inactive billing parties for invoice issue.
- Require `place_of_supply_state_code` for India GST invoices.
- Classify B2B only when a valid GSTIN is present.

Likely files:

- Finance schemas/DTOs.
- Finance repositories.
- Finance service layer.
- Unit tests for GSTIN, PAN, B2B/B2C classification, and brand/legal-entity ownership.

Approval gate:

- Validation behavior reviewed with GST/audit expectations.
- Product modules still cannot pass free-form tax jurisdiction directly to invoice calculation.

### Phase 5B.3 — Invoice Engine and Numbering

Checklist:

- Implement invoice creation behind Finance Core service methods.
- Require `legal_entity_id`, `brand_id`, `billing_party_id`, invoice type, currency, line items, supply date, and idempotency key.
- Allocate invoice numbers as consecutive serial numbers, unique for financial year, not exceeding 16 characters.
- Never reuse, recycle, reassign, or renumber allocated invoice numbers.
- Preserve cancelled/voided invoices with terminal status and original invoice number.
- Use a new invoice number for replacement invoices.
- Use separate statutory numbering for credit notes.
- Snapshot seller legal name, seller GSTIN, seller PAN, seller registered address, and seller state code.
- Snapshot buyer billing name, address, GSTIN, PAN, party type, place-of-supply state code, and B2B/B2C classification.
- Calculate invoice line GST split using seller state and buyer place of supply:
  - same state: CGST + SGST
  - different state: IGST
- Persist taxable amount, GST rate, CGST/SGST/IGST rates and amounts, total tax, line total, place-of-supply snapshot, and jurisdiction type.

Likely files:

- Finance invoice service.
- Finance numbering service.
- Finance tax calculation service.
- Finance audit/event service.
- Invoice repository and read models.

Approval gate:

- GST audit invariants reviewed before enabling invoice issue from product modules.
- Tests prove cancelled/voided numbers are not reused.

### Phase 5B.4 — Direct Finance Core Idempotency

Checklist:

- Require caller-supplied `idempotency_key` for every mutating Finance Core method:
  - `create_invoice`
  - `record_payment`
  - `allocate_payment`
  - `issue_refund`
  - `issue_credit_note`
  - `post_ledger_entry`
- Implement canonical payload hashing.
- Add `UNIQUE (caller_module, operation, idempotency_key)`.
- Require `expires_at`.
- Enforce minimum 24-hour retention for all mutating calls.
- Prefer financial-audit-safe long retention or no automatic expiry until archival for invoices, refunds, credit notes, and ledger entries.
- Return original result for same key + same payload.
- Return `409 IDEMPOTENCY_KEY_PAYLOAD_MISMATCH` for same key + different payload.
- Reject mutating calls without an idempotency key.

Likely files:

- Finance idempotency repository.
- Finance idempotency middleware/helper for service calls.
- Finance service wrappers for mutating operations.
- Tests for retry, in-progress, completed replay, deterministic failure replay, and payload mismatch.

Approval gate:

- Idempotency behavior reviewed against queue replay, worker restart, network timeout, and database failover scenarios.

### Phase 5B.5 — Finance-to-Product Outbox

Checklist:

- Implement `finance.outbox_events` with pending/processed/failed status.
- Include event type, payload, brand ID, linked payment ID, linked invoice ID, idempotency key, retry count, lock fields, next attempt time, last error, created timestamp, and processed timestamp.
- Claim pending events atomically using row-level locking, `SKIP LOCKED`, or equivalent.
- Prevent concurrent processing of the same pending event by multiple workers.
- Implement stale-lock recovery policy.
- Ensure Finance Core owns outbox state transitions.
- Require product modules to maintain consumed-event records and treat duplicate `finance_event_id` or idempotency key as no-op success.
- Mark finance outbox events processed only through Finance dispatcher or approved Finance Core acknowledgement API.

Likely files:

- Finance outbox repository.
- Finance dispatcher/worker.
- Finance acknowledgement service/API, if chosen.
- Product-module consumption contract tests.

Approval gate:

- Product-module consumption model approved before wiring to Doers or FreshBite.
- No product module may directly mutate Finance Core outbox tables.

### Phase 5B.6 — Razorpay Webhook Validation Boundary

Checklist:

- Define a boundary component for Razorpay webhook validation without activating subscriptions directly.
- Verify raw payload signature before parsing or processing.
- Deduplicate provider event IDs.
- Validate provider payload server-side.
- Reconcile Finance Core payment/order/invoice state.
- Persist durable evidence/audit records.
- Insert `finance.outbox_events` only after finance state is valid and committed.
- Ensure webhook failures never create product entitlements directly.

Likely files:

- Provider boundary interface.
- Razorpay webhook verification adapter placeholder or later-phase integration point.
- Finance payment reconciliation service.
- Finance evidence/audit service.
- Webhook tests using fixture payloads and invalid signatures.

Approval gate:

- Real Razorpay secrets, SDK calls, live webhook route exposure, and production behavior remain disabled until a later explicit provider-integration approval.

## 3. Database Tables and Constraints

Required tables:

- `finance.billing_parties`
- `finance.invoices`
- `finance.invoice_lines`
- `finance.invoice_number_allocations` or equivalent numbering/audit table
- `finance.idempotency_keys`
- `finance.outbox_events`

Recommended supporting tables:

- `finance.legal_entities`
- `finance.tax_registrations`
- `finance.credit_notes`
- `finance.payments`
- `finance.refunds`
- `finance.audit_events`
- product-owned consumed finance event tables, for example `platform_billing.consumed_finance_events` or module-specific equivalent

Key constraints:

- Billing party `party_type` enum: `individual`, `business`.
- GSTIN format validation when present.
- PAN format validation when present.
- Required place of supply for India GST invoices.
- Billing party legal entity and brand must match invoice legal entity and brand.
- Invoice number unique per financial year and not longer than 16 characters.
- Invoice number cannot be reused after cancellation, void, correction, or non-payment.
- Invoice lifecycle status enum includes `draft`, `issued`, `partially_paid`, `paid`, `overdue`, `cancelled`, `voided`, `credited`.
- Invoice line jurisdiction enum includes `intra_state`, `inter_state`.
- `finance.idempotency_keys` unique on `(caller_module, operation, idempotency_key)`.
- `finance.idempotency_keys.expires_at` required.
- `finance.outbox_events.status` enum includes `pending`, `processed`, `failed`.

## 4. Service Boundaries

Finance Core owns:

- Billing party identity and buyer tax jurisdiction.
- Seller tax registration lookup and seller invoice snapshots.
- GST calculation source of truth.
- Invoice numbering allocation and immutable invoice lifecycle.
- Idempotency for mutating Finance Core service calls.
- Payment/refund/credit-note finance records.
- Finance-to-product outbox insertion, claiming, retry, and acknowledgement state.
- Razorpay webhook validation boundary and finance-state reconciliation.

Product modules own:

- Business intent to request invoices/payments.
- Stable caller-supplied idempotency keys.
- Product-specific consumed finance event records.
- Idempotent product state changes after validated finance events.
- Subscription, entitlement, order, or access changes after consuming finance events.

Forbidden boundary crossings:

- Product modules must not pass free-form buyer tax jurisdiction into invoice calculation.
- Product modules must not calculate GST as source of truth.
- Razorpay webhook handlers must not directly activate subscriptions, unlock entitlements, or mutate product modules.
- Product modules must not directly update `finance.outbox_events`.
- Browser/client code must not control amount, currency, provider, tenant, organization, provider customer, or tax jurisdiction.

## 5. Test Plan

Schema and migration tests:

- Create all required finance tables and constraints.
- Verify downgrade/re-upgrade if the implementation phase authorizes migration tests.
- Verify no unrelated platform billing or product tables are changed unless explicitly authorized.

Billing party tests:

- Valid individual B2C billing party.
- Valid business B2B billing party with GSTIN.
- Business without GSTIN classified as B2C.
- PAN-only classified as B2C.
- Invalid GSTIN rejected or classified according to configured strictness.
- Inactive billing party rejected for invoice issue.
- Cross-brand or cross-legal-entity billing party rejected.

Invoice numbering tests:

- Allocated numbers are unique per financial year.
- Numbers do not exceed 16 characters.
- Cancelled/voided invoices retain original number.
- Replacement invoice receives a new number.
- Credit note receives a separate statutory number.
- Explainable gap audit fields exist.
- No silent deletion or renumbering path exists.

Invoice engine and GST tests:

- Same seller state and buyer place of supply creates CGST + SGST split.
- Different seller state and buyer place of supply creates IGST split.
- Line tax amounts, total tax, line total, jurisdiction type, and place-of-supply snapshot persist.
- Seller snapshot persists and is unaffected by later master data changes.
- Buyer snapshot persists and is unaffected by later billing party changes.
- Product-supplied tax amounts are ignored/recalculated or rejected as non-authoritative.

Idempotency tests:

- Missing idempotency key rejected for mutating calls.
- Same key + same payload returns original result with no duplicate side effect.
- Same key + different payload returns `409 IDEMPOTENCY_KEY_PAYLOAD_MISMATCH`.
- Completed invoice/refund/ledger calls replay safely.
- In-progress calls return safe retry/in-progress response or block according to policy.
- `expires_at` required and retention policy enforced.

Outbox tests:

- Finance state commit inserts pending outbox event.
- Atomic claim uses row-level locking / `SKIP LOCKED` or equivalent.
- Two workers cannot process the same pending event concurrently.
- Product duplicate consumption is no-op success.
- Failed event increments retry count and schedules `next_attempt_at`.
- Stale lock recovery makes event retryable.
- Finance Core marks processed only after approved acknowledgement.

Razorpay boundary tests:

- Invalid signature rejected before event processing.
- Duplicate provider event deduped.
- Unknown event safely ignored/audited.
- Valid payment event reconciles Finance Core state and inserts outbox event.
- Webhook does not directly activate subscription or entitlement.
- Provider payload mismatch does not create product state.

GST audit tests:

- Issued invoices reconstruct from snapshots without mutable master data.
- B2B/B2C classification is immutable after issue.
- Cancellation reason, timestamp, actor, and audit event are recorded.
- Invoice number audit explains every allocated number.
- Logs and evidence redact secrets and sensitive provider data.

## 6. Rollback and Recovery Strategy

Database rollback:

- Use expand-only migrations where possible.
- Do not drop finance tables in production rollback; disable usage via feature flags and preserve audit records.
- If a migration must be reverted in non-production, verify no issued finance documents exist before destructive cleanup.

Runtime rollback:

- Disable Finance Core write entrypoints through feature flags.
- Stop Finance dispatcher workers before schema rollback or data repair.
- Keep read-only invoice/audit access available where possible.
- Preserve `finance.idempotency_keys` and `finance.outbox_events` for retry safety.

Data recovery:

- Reconcile idempotency records before retrying uncertain operations.
- Reprocess failed/pending outbox events through claim/ack flow.
- Never reissue invoice numbers to fill gaps.
- Correct issued finance documents only through cancellation, replacement invoice, credit note, or controlled finance workflow.

Provider recovery:

- Webhook replay must dedupe by provider event ID and internal idempotency key.
- Delayed Razorpay events must reconcile Finance Core state before product modules consume events.
- Provider outage must keep checkout/payment state pending or failed without granting product entitlements directly.

## 7. Approval Gates

Gate A — Schema design approval:

- Finance table list reviewed.
- Invoice numbering constraints reviewed.
- GST audit snapshot fields reviewed.
- Idempotency retention policy reviewed.

Gate B — Service boundary approval:

- Finance-owned and product-owned responsibilities reviewed.
- No direct webhook-to-subscription activation path confirmed.
- Product-module consumed-event strategy selected.

Gate C — Migration approval:

- Migration file reviewed before running against shared databases.
- Rollback/recovery plan reviewed.
- No production behavior enabled by migration alone.

Gate D — Test approval:

- Schema, GST, idempotency, outbox, webhook-boundary, and audit tests pass.
- Cross-module product consumption tests pass before wiring Doers/FreshBite.

Gate E — Rollout approval:

- Feature flags default off.
- Finance dispatcher disabled until approved.
- Razorpay real provider mode disabled until separate provider integration approval.
- Production pilot requires separate sign-off.

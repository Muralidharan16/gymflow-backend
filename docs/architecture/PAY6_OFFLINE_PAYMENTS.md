# PAY-6 Offline Payments

**Certified PAY-5 predecessor:** \`1247f357c68246ce442e75ff0a43470524440e9f\`  
**PAY-5 tree:** \`016ee5baa80b594cb75ef28f499fb42bc38fca8c\`  
**Alembic predecessor:** \`zp07d8e9f0a50\`  
**PAY-6 head:** \`zq07d8e9f0a51\`

PAY-6 makes externally collected money a first-class Finance Core flow without
turning a browser or legacy payment table into financial authority.

## Supported methods

The bounded methods are:

- cash;
- bank transfer;
- cheque.

Every request requires an invoice, exact amount/currency, a structured reference
and a SHA-256 proof digest. There is no free-form \`mark paid\` operation.

The proof digest represents externally retained collection evidence. PAY-6 stores
the digest, not arbitrary sensitive proof bodies.

## Maker/checker

Preparation and decision are separate authoritative operations.

A preparer must be an authenticated organization owner/admin. Approval or
rejection requires a different authenticated actor UUID. PostgreSQL enforces
this separation even when the HTTP layer is bypassed.

The original decision actor is also bound into terminal idempotent replay.
Another checker cannot replay another actor's approval/rejection key.

## Monetary command authority

Prepare, approve and reject use the certified PAY-3
\`finance.monetary_commands\` store. PAY-6 does not bind \`app_runtime\` to the
reserved PAY-2 Finance capability roles. Instead, bounded reduced-owner helpers
write the same canonical command table while preserving tenant, request-hash,
business-reference and actor-hash fencing.

## Approval transaction

Approval locks and re-validates the prepared request and invoice. It recalculates
the current invoice outstanding amount immediately before creating money truth.

Only after those checks does it create a \`finance.payments\` row:

- \`provider_code = manual\`;
- status \`captured\`;
- exact prepared amount/currency;
- deterministic structured provider reference derived from tenant/method/reference.

A manual payment event is recorded and the payment is applied through the
already-certified \`app_secure.apply_finance_confirmed_payment\` capability.
Therefore offline money follows the same allocation, ledger and Finance outbox
authority as confirmed provider money.

A fully paid invoice emits \`finance.invoice.paid\`. A partial payment emits only
\`finance.invoice.partially_paid\`. PAY-5 therefore activates entitlement only
from full Finance truth with no offline-specific bypass.

## Rejection and correction

Rejection is terminal and creates no Finance payment. It requires a machine-safe
reason code and immutable audit evidence.

PAY-6 does not permit editing or deleting approved financial history. Later
correction/refund/credit phases must use their own compensating financial
records; they must never mutate the historical offline approval in place.

## Legacy payments

The historical \`public.payments\` / \`PaymentService\` flow is not Finance
authority and is not used by PAY-6. PAY-15 owns eventual legacy retirement.

## Database security

\`app_runtime\` receives EXECUTE only on prepare/approve/reject capabilities and
no direct Finance table DML. The two PAY-6 evidence tables use ENABLE + FORCE RLS.

PAY-6 tracks every predecessor-absent \`app_security_owner\` column privilege in
\`app_private.pay6_offline_payment_acl_delta\`, allowing empty downgrade to
restore the exact PAY-5 ACL posture.

## Migration policy

PAY-6 is additive. It adds two Finance evidence relations and bounded functions.
It performs no existing Finance data rewrite/backfill.

Populated downgrade fails closed when any offline-payment request exists. Empty
predecessor → PAY-6 → predecessor → PAY-6 lifecycle must preserve predecessor
relations and ACLs.

## Terminal markers

\`\`\`text
PAY6_OFFLINE_PAYMENT_AUTHORITY=PASS
PAY6_PROOF_REFERENCE_REQUIRED=PASS
PAY6_MAKER_CHECKER=PASS
PAY6_IDEMPOTENT_RECORDING=PASS
PAY6_INVOICE_RECONCILIATION=PASS
PAY6_IMMUTABLE_AUDIT=PASS
PAY6_DIRECT_DML_DENIAL=PASS
PAY6_MIGRATION_LIFECYCLE=PASS
PAY6_PAY5_INHERITED=PASS
PAY6_LIVE_MONEY_MOVEMENT=DISABLED
PAY6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
PAY6_FINAL=PASS
\`\`\`

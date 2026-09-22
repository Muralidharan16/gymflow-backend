# PAY-19 — Backup, Restore, PITR and Accounting Reconstruction

PAY-19 is stacked directly on frozen PAY-18
`5de9b1f0997c4e5a6df5af7a2acf5ba1f1df1eee`. It adds no money-changing
authority and no new database migration. The Alembic head remains
`zz37d8e9f0a63`.

## Recovery authority

PostgreSQL remains the durable financial authority. A backup, WAL archive, or
restored cluster is recovery evidence until integrity verification succeeds.
Provider state is never reconstructed by guessing from local projections:
ambiguous external effects must return through the certified provider
reconciliation boundary.

PAY-19 uses synthetic CI financial data only. Restore/PITR clusters have no live
provider credentials and do not authorize production traffic.

## Source financial shape

The recovery source is built through existing certified services/capabilities,
not handwritten substitute state. It contains:

- an issued numbered invoice and invoice-series cursor;
- one durable checkout provider operation;
- one authenticated captured-payment event;
- one explicit PAY-9 payment application and allocation;
- a balanced posted payment-allocation ledger entry;
- durable idempotency and Finance outbox evidence;
- a member-subscription checkout binding;
- a durable refund intent and materialized refund execution command.

The source is fingerprinted by
`scripts/ci/pay19_financial_fingerprint.sql`.

## Backup and restore

PAY-19 must create a PostgreSQL 16 custom-format logical backup, record its
SHA-256 digest, prove its catalog is readable, restore it to a fresh database,
and require an identical financial fingerprint.

The same source cluster must archive WAL and produce a
`pg_basebackup --format=plain --wal-method=stream` physical backup with SHA-256
manifest checksums. `pg_verifybackup` must succeed. A separate physical clone
must start independently and match the source financial fingerprint.

## PITR destructive boundary

After the verified physical base exists, PAY-19 creates a recovery sentinel and
commits `pre_target`, then creates the named restore point
`pay19_financial_target`. After the target it commits `post_target` and
deliberately corrupts synthetic Finance state: the invoice-series cursor is
advanced incorrectly, a payment-application record is deleted, and the refund
execution command is deleted.

The source then archives the destructive WAL. A fresh cluster is reconstructed
from the pre-target base and WAL using the named restore point. Certification
requires:

- `pre_target` exists;
- `post_target` does not exist;
- exact Alembic head is preserved;
- the PITR financial fingerprint equals the pre-loss source fingerprint;
- Finance table, invoice, ledger, provider-reference, refund-command,
  idempotency, outbox, subscription-binding and payment-application invariants
  all remain valid.

Synthetic CI recovery also measures RPO/RTO against the existing conservative
P9 rehearsal budgets of 10 seconds and 120 seconds. These are certification
budgets, not production SLAs.

## Duplicate-prevention proof after recovery

On the PITR database, PAY-19 replays the exact original checkout and payment
application commands. The provider client must receive **zero** new requests,
and provider-operation, application, allocation, ledger and outbox counts must
not increase.

The existing refund execution obligation is materialized again through its
worker capability and must return the existing command rather than create a
second refund command.

A new draft/issue operation is then allowed only to allocate the next legal
invoice number, `VS/2425/00002`; the restored `VS/2425/00001` may not be
reused.

## Provider reconciliation after PITR

After reconstruction and duplicate-prevention checks, the recovered database
must run the PAY-17 provider-success/process-death reconciliation proof. A
provider success with lost local acknowledgement must converge to the one
external provider object and must not submit a second provider operation.

## Gate

```text
PAY19_BACKUP=PASS
PAY19_RESTORE=PASS
PAY19_PITR=PASS
PAY19_FINANCE_TABLE_INTEGRITY=PASS
PAY19_INVOICE_NUMBERING_INTEGRITY=PASS
PAY19_LEDGER_BALANCE_INTEGRITY=PASS
PAY19_PROVIDER_REFERENCE_INTEGRITY=PASS
PAY19_REFUND_COMMAND_INTEGRITY=PASS
PAY19_IDEMPOTENCY_INTEGRITY=PASS
PAY19_OUTBOX_INTEGRITY=PASS
PAY19_SUBSCRIPTION_BINDING_INTEGRITY=PASS
PAY19_NO_DUPLICATE_PROVIDER_OPERATION=PASS
PAY19_NO_REUSED_INVOICE_NUMBER=PASS
PAY19_NO_REPEATED_REFUND=PASS
PAY19_NO_REPEATED_PAYMENT_APPLICATION=PASS
PAY19_RECONCILIATION_AFTER_PITR=PASS
PAY19_FINANCIAL_RECOVERY=PASS
```

Live provider access, production credentials, live money movement, production
runtime binding, merge, release and deployment remain disabled/not authorized.

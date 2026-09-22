# PAY-18 Runbook — Settlement Mismatch

## Alert and ownership

Alert: `DoersPay18SettlementMismatch`  
Owner: `finance-reliability`  
Failure mode: `settlement_mismatch`

This runbook is operational guidance only. PostgreSQL durable financial state,
provider evidence, PAY-16 audit evidence, and certified state machines remain
authoritative.

## Customer impact

Provider settlement or bank evidence does not agree with local captured payments, refunds, fees, disputes, or expected treasury totals, so accounting closure cannot safely complete.

## Evidence to preserve

Preserve the PAY-14 closure run, local/provider/settlement evidence manifests, reconciliation item and mismatch category, any accounting incident, bank/provider source evidence, and sanitized Finance correlation.

## Diagnosis

1. Identify the unresolved PAY-14 reconciliation item through approved accounting tooling and note its mismatch category.
2. Verify the closure period, provider environment/account, and evidence manifest hashes are the expected immutable inputs.
3. Compare local, provider, and settlement-side evidence without altering any financial record.
4. Classify amount, currency, status, missing settlement, duplicate provider object, fee, or refund mismatch.
5. Check whether related refunds, disputes, or chargebacks explain the difference before treating it as provider loss.
6. Check for database/provider ingestion gaps or duplicate evidence ingestion that could create a false mismatch.

## Safe first actions

- Keep automatic financial mutation disabled as required by PAY-14.
- Attach new authoritative evidence through the certified reconciliation/accounting workflow and move resolution state only through its allowed transitions.
- Open or update the accounting/security incident when the PAY-14 safe outcome requires human review.

## Forbidden actions

- Never post a balancing journal or edit money automatically just to close the mismatch.
- Never rewrite evidence hashes, settlement references, or reconciliation keys.
- Never close the accounting run while unresolved reconciliation items remain.

## Escalation

Escalate every unresolved monetary mismatch to Finance accounting; escalate to the provider/bank for missing or contradictory settlement evidence and to security for unexplained duplicate/unknown provider objects.

## Recovery verification

Verify mismatch_category is cleared or the item is resolved with authoritative resolution evidence, accounting incidents are resolved appropriately, closure counters reconcile, no automatic financial mutation occurred, and the closure run becomes ready/closed only through PAY-14 controls.

Recovery does not authorize merge, release, deployment, production credentials,
live-provider enablement, or direct financial-state mutation.

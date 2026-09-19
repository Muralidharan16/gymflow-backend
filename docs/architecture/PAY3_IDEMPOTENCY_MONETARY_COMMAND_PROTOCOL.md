# PAY-3 Idempotency and Monetary Command Protocol

**Certified PAY-2 predecessor:** `f229e655ffd22500697d69fb6504ef48ecc827f1`  
**PAY-2 tree:** `33bae8741c16925681414bf58ff0be3133c98391`  
**Alembic predecessor:** `zl07d8e9f0a46`  
**PAY-3 head:** `zm07d8e9f0a47`

PAY-3 introduces a durable logical-command journal without enabling live money.

## Why this exists

Existing `finance.idempotency_keys` remains valid for current Finance Core calls.
It has a bounded operational expiry and is tied to today's `app_runtime`
capabilities. PAY-3 does not rewrite it.

The new `finance.monetary_commands` relation is persistent evidence for a
logical money-changing command across API retries, queue redelivery, worker
restart, database failover and unknown provider outcomes.

## Invariants

```text
same tenant + scope + key + same request
    -> replay original command/result

same tenant + scope + key + different request
    -> conflict, zero new side effect

processing
    -> succeeded
    -> failed_deterministic
    -> unknown

unknown
    -> succeeded
    -> failed_deterministic

succeeded / failed_deterministic
    -> immutable terminal truth
```

`unknown` is intentionally non-terminal. It means an external effect may have
happened but authoritative evidence is insufficient. A replacement money effect
must not be issued merely because a local call timed out.

## Privacy and security

The command journal stores:
- tenant;
- operation scope;
- safe idempotency key;
- canonical request SHA-256;
- safe business reference;
- correlation UUID;
- actor type;
- SHA-256 of actor reference.

Raw actor identity is not stored in this journal.

Business payloads are not stored. Floats are forbidden by the canonical hashing
contract.

The table has ENABLE+FORCE RLS. Runtime identities have no direct table DML.
PAY-3 creates SECURITY DEFINER functions owned by `app_security_owner`, revokes
PUBLIC EXECUTE, and deliberately does not yet grant production execution to
`finance_runtime`. PAY-4 must bind the exact runtime before activation.

## Retention

Monetary command evidence has no automatic TTL in PAY-3. Removal/archival is a
future explicit retention workflow, never silent expiry.

This prevents a delayed retry from becoming a new charge merely because a short
idempotency TTL elapsed.

## Migration safety

PAY-3 creates one new table, one index, one RLS policy and four functions.

It:
- rewrites no existing Finance table;
- backfills no existing data;
- changes no payment/refund/subscription/provider state;
- blocks downgrade once durable command evidence exists;
- permits empty downgrade/re-upgrade;
- keeps live money disabled.

## Terminal markers

```text
PAY3_MONETARY_COMMAND_PROTOCOL=PASS
PAY3_IDEMPOTENT_REPLAY=PASS
PAY3_UNKNOWN_OUTCOME_FENCING=PASS
PAY3_DIRECT_DML_DENIAL=PASS
PAY3_DURABLE_EVIDENCE=PASS
PAY3_MIGRATION_LIFECYCLE=PASS
PAY3_PAY2_INHERITED=PASS
PAY3_LIVE_MONEY_MOVEMENT=DISABLED
PAY3_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
PAY3_FINAL=PASS
```

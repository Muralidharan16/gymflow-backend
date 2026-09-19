# PAY-3 Idempotency and Monetary Command Protocol

**Certified PAY-2 predecessor:** `f229e655ffd22500697d69fb6504ef48ecc827f1`  
**PAY-2 tree:** `33bae8741c16925681414bf58ff0be3133c98391`  
**Alembic predecessor:** `zl07d8e9f0a46`  
**PAY-3 initial revision:** `zm07d8e9f0a47`  
**PAY-3 head:** `zn07d8e9f0a48`

PAY-3 introduces a durable logical-command journal without enabling live money.
The already-pushed `zm07...` predecessor is not rewritten; `zn07...` is an
additive repair for ambiguity evidence, actor identity and crash/concurrency proof.

## Why this exists

Existing `finance.idempotency_keys` remains valid for current Finance Core calls.
It has a bounded operational expiry and is tied to today's `app_runtime`
capabilities. PAY-3 does not rewrite it.

The new `finance.monetary_commands` relation is persistent evidence for a
logical money-changing command across API retries, queue redelivery, worker
restart, database failover and unknown provider outcomes.

## Invariants

```text
same tenant + scope + key + same request + same originating actor
    -> replay original command/result

same tenant + scope + key + different request/actor
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

When a command becomes `unknown`, PAY-3 durably records `ambiguity_code` and
`unknown_at`. Those fields survive later reconciliation to `succeeded` or
`failed_deterministic`, so the ambiguity is still explainable after terminal
resolution.

## Concurrency and crash semantics

The database unique constraint is the race fence for concurrent reservations.
Certification must prove one insert and N-1 replays for the same logical command.
It also proves:

```text
crash/connection loss before commit -> reservation rolls back; retry can reserve
commit succeeds then caller dies     -> retry returns original command
```

No in-memory, Redis or Celery state is part of this authority.

## Privacy and security

The command journal stores tenant, operation scope, safe idempotency key,
canonical request SHA-256, safe business reference, correlation UUID, actor type,
and SHA-256 of actor reference. Raw actor identity and business payloads are not
stored. Floats are forbidden by the canonical hashing contract.

The originating actor identity is immutable for a logical command. A retry that
reuses the same tenant/scope/key/request while changing actor type or actor hash
is rejected.

The table has ENABLE+FORCE RLS. Runtime identities have no direct table DML.
PAY-3 creates SECURITY DEFINER functions owned by `app_security_owner`, revokes
PUBLIC EXECUTE, and deliberately does not yet grant production execution to
`finance_runtime`. PAY-4 must bind the exact runtime before activation.

## Retention

Monetary command evidence has no automatic TTL in PAY-3. Removal/archival is a
future explicit retention workflow, never silent expiry. This prevents a delayed
retry from becoming a new charge merely because a short idempotency TTL elapsed.

## Migration safety

PAY-3 creates one new command table in `zm07...`; `zn07...` adds only nullable
ambiguity-evidence columns, constraints, and function-body hardening. Existing
Finance tables are not rewritten or backfilled. The repair downgrade fails closed
if ambiguity evidence exists. The full PAY-3 downgrade remains blocked whenever
durable command evidence exists. Empty downgrade/re-upgrade remains required.

## Terminal markers

```text
PAY3_MONETARY_COMMAND_PROTOCOL=PASS
PAY3_IDEMPOTENT_REPLAY=PASS
PAY3_UNKNOWN_OUTCOME_FENCING=PASS
PAY3_CONCURRENCY_CRASH=PASS
PAY3_DIRECT_DML_DENIAL=PASS
PAY3_DURABLE_EVIDENCE=PASS
PAY3_MIGRATION_LIFECYCLE=PASS
PAY3_PAY2_INHERITED=PASS
PAY3_LIVE_MONEY_MOVEMENT=DISABLED
PAY3_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
PAY3_FINAL=PASS
```

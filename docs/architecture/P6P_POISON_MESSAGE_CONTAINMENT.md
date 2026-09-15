# P6-P Poison-Message Containment

Status: implementation/certification slice under the frozen P6 acceptance contract.

Base certified P6-W candidate: `2b4f37673b0b70668be0ec9af0d227e853670f38`.

## Scope

P6-P proves both poison classes frozen in `P6_ACCEPTANCE_MATRIX.md` without
changing production application code:

1. an unregistered Celery broker message with no durable business authority is
   discarded by a real prefork worker without worker death or hot redelivery;
2. a structurally valid durable lifecycle command is fault-injected to fail
   deterministically and retryably, then reaches the PostgreSQL dead-letter
   disposition after its bounded `max_attempts` budget.

The fault injection lives only in `scripts/ci/p6p_poison_fault_hooks.py` and is
loaded by the P6-P worker through an explicit CI `--include`. Production Celery
configuration does not import it.

## Decisive runtime

The workflow uses PostgreSQL 16 plus authenticated TLS Redis primary/replica
with AOF everysec, periodic RDB, `noeviction`, and `vm.overcommit_memory=1`.
The worker is a real production-profile prefork worker using only reduced
`worker_runtime` database authority with `NOBYPASSRLS`.

The runtime proves:

- the unregistered broker task is consumed/discarded, its dedicated queue
  drains, the same worker PID remains alive, and its unique task identifier
  does not reappear after the queue is empty;
- the valid durable poison fails once, returns to `pending` under the production
  exponential backoff, fails again after backoff, and reaches `dead_lettered`
  exactly at `max_attempts=2`;
- a following malformed durable row in the same claimed batch reaches its own
  explicit terminal disposition, proving the first failure does not abort batch
  progress;
- repeated later poller invocations do not change either terminal PostgreSQL
  row, so terminal poison cannot form a hot loop; and
- refund-provider execution remains deferred/fail-closed.

## Required markers

- `P6P_UNREGISTERED_BROKER_MESSAGE_DISCARDED=PASS`
- `P6P_WORKER_FLEET_SURVIVES_POISON=PASS`
- `P6P_VALID_DURABLE_POISON_RETRY_BOUNDED=PASS`
- `P6P_TERMINAL_POISON_NOT_REDISPATCHED=PASS`
- `P6P_BATCH_PROGRESS_AFTER_POISON=PASS`
- `P6P_NO_FALSE_SUCCESS=PASS`
- `P6_POISON_MESSAGE_CONTAINED=PASS`
- `P6_NO_LOST_DURABLE_BUSINESS_WORK=PASS`
- `P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`

P6-P does not authorize merge, release, tag, deployment, live money movement,
refund-provider activation, or P6-S/P6-F completion.

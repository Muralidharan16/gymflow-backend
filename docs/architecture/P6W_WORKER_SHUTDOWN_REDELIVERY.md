# P6-W Worker Shutdown and Late-Ack Redelivery

Status: implementation/certification slice under the frozen P6 acceptance contract.

## Scope

P6-W proves two worker-delivery properties without moving durable business
authority out of PostgreSQL:

1. a real Celery prefork worker receiving `SIGTERM` in its main process while a
   durable lifecycle task is active performs a warm shutdown without losing or
   partially applying the PostgreSQL obligation; and
2. Celery late acknowledgement plus `task_reject_on_worker_lost` redelivers the
   same task identity across both the before-commit and
   after-commit/before-ack crash boundaries, while fenced PostgreSQL state
   converges to one terminal business effect.

The slice does not activate refund-provider execution, does not widen runtime
database privileges, and does not treat Redis result/broker state as business
authority.

## Decisive runtime

The P6-W workflow uses:

- PostgreSQL 16 with independent `app_runtime` and `worker_runtime` login roles,
  both `NOBYPASSRLS`;
- a real Redis 7 primary/replica topology with TLS, password authentication,
  AOF `appendfsync everysec`, periodic RDB, `noeviction`, and
  `vm.overcommit_memory=1`;
- a real Celery 5.6.3 prefork worker in production profile;
- one durable `branch_outbox_events` lifecycle obligation created by reduced
  application authority; and
- CI-only fault hooks loaded through Celery `--include`.

### Warm SIGTERM

The worker first commits the durable outbox lease. A CI-only hook pauses before
the lifecycle processor executes. The controller sends `SIGTERM` to the main
Celery PID only, proves the process remains alive in warm shutdown while the
child is active, observes the warm-shutdown log, releases the child, and then
requires a clean worker exit plus one terminal PostgreSQL disposition. A new
worker is started afterward and must observe no second business effect.

### Late-ack redelivery

The already-isolated P5-W2 child fault hook is reused as a process primitive but
the evidence is freshly rerun under P6 TLS Redis and P6 runtime identities.

- Before commit: the prefork child is killed after the durable lease claim and
  before the processor transaction. Redis redelivers the same task ID to
  another child. The live lease prevents a second claim. After lease expiry, a
  replacement worker reclaims the PostgreSQL obligation and reaches one
  terminal state.
- After commit / before broker acknowledgement: the prefork child is killed
  immediately after the terminal database transaction commits but before the
  Celery task can return. Redis redelivers the same task ID to another child;
  the already-terminal PostgreSQL row prevents a duplicate effect.

## Required markers

The decisive workflow emits:

- `P6W_MAIN_PROCESS_SIGTERM=PASS`
- `P6W_WARM_SHUTDOWN_OBSERVED=PASS`
- `P6W_BEFORE_COMMIT_REDELIVERY=PASS`
- `P6W_AFTER_COMMIT_PRE_ACK_REDELIVERY=PASS`
- `P6W_SINGLE_AUTHORITATIVE_EFFECT=PASS`
- `P6_WORKER_SIGTERM_SAFE=PASS`
- `P6_LATE_ACK_REDELIVERY_SAFE=PASS`
- `P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`

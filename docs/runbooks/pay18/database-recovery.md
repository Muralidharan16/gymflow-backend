# PAY-18 Runbook — Finance Database Recovery

## Alert and ownership

Alert: `DoersPay18DatabaseRecovery`  
Owner: `data-reliability`  
Failure mode: `database_recovery`

This runbook is operational guidance only. PostgreSQL durable financial state,
provider evidence, PAY-16 audit evidence, and certified state machines remain
authoritative.

## Customer impact

Finance runtime lost or timed out database connectivity. Transactions may have rolled back, committed before acknowledgement, or left durable commands requiring lease expiry/reconciliation.

## Evidence to preserve

Preserve PostgreSQL failover/restart/TLS evidence, runtime-principal connection evidence, transaction/lock errors, durable operation and lease state, provider ambiguity evidence, and sanitized Finance correlation.

## Diagnosis

1. Confirm PostgreSQL health, failover/restart timeline, TLS endpoint, and DNS/network path independently of the application.
2. Verify the Finance runtime is reconnecting with the certified reduced database principal and correct database/environment.
3. Determine whether failures happened before commit, after commit acknowledgement loss, or during provider I/O.
4. Inspect durable commands/leases and idempotency records for in-flight work; let database-clock leases govern reclaim.
5. Check payment/refund unknown metrics and reconciliation backlog for external effects whose DB acknowledgement may have been lost.
6. Check deadlocks/pool timeouts separately from hard disconnects so the recovery action matches the failure mode.

## Safe first actions

- Restore the database/network/TLS dependency and allow connection pools to recover using certified configuration.
- Use idempotent replay, lease reclaim, or reconciliation according to the durable command state.
- Verify backups/PITR independently if the incident involved storage corruption or failover uncertainty.

## Forbidden actions

- Never point Finance runtime at an unapproved database or privileged migration credential.
- Never assume an error means rollback when commit acknowledgement was lost.
- Never reset leases, statuses, idempotency keys, or provider references manually to force recovery.

## Escalation

Escalate immediately for production Finance DB disconnects during money-changing operations, repeated failover, suspected data loss/corruption, or inability to prove commit outcome. Engage provider reliability if external effects overlap the DB outage.

## Recovery verification

Verify certified runtime-principal connection, PostgreSQL health, migrations/head integrity, no open ambiguous commands beyond expected reconciliation, all committed work remains durable, rolled-back work is safely replayable, and backup/restore evidence is current where required.

Recovery does not authorize merge, release, deployment, production credentials,
live-provider enablement, or direct financial-state mutation.

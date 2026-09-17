# P9-R — PostgreSQL recovery compatibility repair

## Root cause

The P9-R full restore path was healthy, but named-target PITR startup failed before sentinel or application checks. The PostgreSQL recovery log proved the failure was caused by the recovery instance starting with a lower recovery-sensitive capacity than the source primary. In the observed failure, the source primary recorded `max_connections = 100` while the PITR clone was configured with `max_connections = 50`.

PostgreSQL targeted recovery requires recovery-sensitive shared-memory settings to be at least compatible with the primary whose WAL is being replayed. A fixed reduced restore value is therefore not a valid recovery configuration.

## Permanent repair

P9-R now captures the source-primary values for all five recovery-sensitive settings used by this certification:

- `max_connections`
- `max_prepared_transactions`
- `max_locks_per_transaction`
- `max_wal_senders`
- `max_worker_processes`

The exact source vector is recorded as evidence and exported only within the disposable CI job. Both the independent full-restore clone and the PITR clone use those captured values. After startup, each clone re-reads the five settings and must byte-match the source evidence before the restore can count as successful.

The named PITR target, WAL archive source, `recovery.signal`, `recovery_target_action = 'promote'`, private Unix-socket boundary, application provider-egress firewall, reduced database identities, and frozen RPO/RTO budgets are unchanged.

## Failure observability

If PITR startup fails, P9-R now preserves and prints PostgreSQL's server startup log as `pitr-start-failure.log` before failing the job. Recovery startup can no longer fail with only the outer `pg_ctl` message.

## Regression authority

`tests/test_p9r_recovery_compatibility_contracts.py` statically requires source-setting capture, propagation to both restore configurations, exact source/restore comparisons, removal of the hard-coded lower `max_connections` value, preservation of PITR startup diagnostics, and preservation of the named-target recovery semantics.

The runtime workflow remains the decisive proof: P9-R certifies only if full restore, PITR, sentinel boundary, fingerprint equality, application readiness, provider isolation, and measured RPO/RTO all pass on the same exact candidate SHA.

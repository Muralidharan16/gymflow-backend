# PAY-2 Financial Authority and Database Security

## PAY-2A — Dedicated Finance identity isolation

PAY-2 begins from certified PAY-1 SHA `ba8d77d0e50907fd435d358697d9721a63bad64b`.

PAY-2A changes only the externally bootstrapped PostgreSQL capability contract.
Alembic is still forbidden to create, repair, alter, or grant cluster roles.

The following NOLOGIN capability groups become canonical:

- `finance_runtime` — synchronous tenant-bound Finance commands through approved `app_secure` capabilities.
- `finance_read_runtime` — approved Finance read models only.
- `payment_worker_runtime` — asynchronous provider/payment processing.
- `refund_runtime` — execution of already-authorized refund obligations; cannot approve refunds.
- `finance_reconciliation_runtime` — payment/refund/settlement reconciliation; cannot grant entitlement.
- `finance_maintenance_runtime` — dead-letter/lease/watchdog maintenance; cannot manufacture financial success.

Every role is NOLOGIN, NOINHERIT, NOSUPERUSER, NOCREATEDB, NOCREATEROLE,
NOREPLICATION and NOBYPASSRLS. None may own schema objects. None may be
reachable from `migration_owner` or from another peer runtime capability.

PAY-2A deliberately creates no deployment-login binding. PAY-2C will later
bind distinct deployment logins only after PAY-2B has installed exact database
capabilities.

## PAY-2B migration admission rules

PAY-2B is not allowed to start until PAY-2A passes on a fresh PostgreSQL 16
cluster.

The first PAY-2 migration must:

1. revise exactly `zk07d8e9f0a45`;
2. require `migration_owner` as session/current user;
3. require exact reduced attributes for all Finance capability roles;
4. prove `migration_owner` cannot MEMBER/USAGE/SET any Finance runtime role;
5. create no PostgreSQL roles;
6. create no LOGIN;
7. grant no direct broad Finance-table DML to runtime roles;
8. use bounded `SECURITY DEFINER` functions owned by `app_security_owner`;
9. set fixed `search_path=pg_catalog,public,finance` and `row_security=on`;
10. revoke PUBLIC function execution;
11. preserve FORCE RLS and existing finance evidence;
12. have a deterministic downgrade that restores the exact predecessor ACL/function topology;
13. block downgrade rather than destroy new financial evidence where reversibility is unsafe;
14. pass populated forward/downgrade/re-upgrade, ACL/RLS/ownership, lock/rewrite,
    rolling compatibility and PITR-oriented migration proofs.

## Hard stop

PAY-2 does not authorize live payment execution, production provider access,
refund provider execution, merge, release or deployment.

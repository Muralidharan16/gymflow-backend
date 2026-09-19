# PAY-2 Financial Authority and Database Security

PAY-2 is the first payment-program phase allowed to add an Alembic revision.

## Certified predecessor

- PAY-1 SHA: `ba8d77d0e50907fd435d358697d9721a63bad64b`
- PAY-1 tree: `cd9500217b8230df2c0777b405b3d462b83e99ac`
- predecessor Alembic head: `zk07d8e9f0a45`
- PAY-2 head: `zl07d8e9f0a46`

## Authority topology

PAY-2 introduces six cluster-managed NOLOGIN capability roles:

- `finance_runtime`
- `finance_payment_runtime`
- `finance_refund_runtime`
- `finance_reconciliation_runtime`
- `finance_read_runtime`
- `finance_maintenance_runtime`

They are deliberately inert in PAY-2. They receive no raw Finance table SELECT,
INSERT, UPDATE, DELETE or TRUNCATE privilege, own no object, have no membership
edge to migration/security owner and are not deployment logins yet.

The runtime identity contract records all six as
`reserved_unbound_capabilities`. Bound runtime capabilities plus reserved
capabilities must exactly equal the protected peer-capability graph. A reserved
Finance capability is rejected if it appears in any deployment login's direct
capability set.

Future phases must first remove the exact capability from the reserved set,
introduce a separately attested deployment binding, and then grant only exact
`app_secure` capabilities.

## Existing-cluster rollout

The normal cluster bootstrap remains fresh-cluster/create-only.

PAY-2 therefore adds a separate one-shot infrastructure expansion:

```text
verify exact pre-PAY-2 role graph
        ↓
create only six PAY-2 NOLOGIN roles
        ↓
apply only their governed role settings
        ↓
verify full canonical role + identity graph
        ↓
Alembic PAY-2 may start
```

Alembic never creates, alters or repairs managed roles.

## Immutable financial evidence

PAY-2 adds database triggers protecting:

- `finance.audit_events`
- `finance.payment_events`
- `finance.ledger_entry_lines`
- `finance.tax_records`
- `finance.credit_note_lines`

Application/runtime UPDATE or DELETE is rejected even if an ACL is accidentally
broadened later.

Posted `finance.ledger_entries` are also immutable.

`migration_owner` is the only explicit trigger bypass because that identity
already owns schema migration authority and is never a runtime identity.

## Ownership governance

The canonical object-ownership manifest now includes the Finance schema, every
current Finance table and the PAY-2 trigger functions. Runtime capability roles
are forbidden owners.

## Migration characteristics

PAY-2:

- creates no business-data table;
- performs no backfill;
- rewrites no table;
- changes no money state;
- changes no subscription state;
- changes no provider state;
- preserves populated Finance rows through downgrade/re-upgrade;
- keeps live money disabled;
- keeps refund-provider execution fail-closed.

## Terminal markers

```text
PAY2_CLUSTER_ROLE_AUTHORITY=PASS
PAY2_FINANCE_OBJECT_OWNERSHIP=PASS
PAY2_IMMUTABLE_HISTORY=PASS
PAY2_DIRECT_DML_DENIAL=PASS
PAY2_MIGRATION_LIFECYCLE=PASS
PAY2_ZERO_TABLE_REWRITE=PASS
PAY2_PAY1_INHERITED=PASS
PAY2_LIVE_MONEY_MOVEMENT=DISABLED
PAY2_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
PAY2_FINAL=PASS
```

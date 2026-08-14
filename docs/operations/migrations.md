# Production migration runbook

This runbook defines the production-safe path for applying the DOERS Alembic lineage. It complements the automated migration, identity, downgrade/restore, adversarial, and data-preservation gates; it does not replace them.

## Ownership boundaries

Infrastructure owns cluster-level PostgreSQL capabilities and extensions. Alembic owns application schema evolution. Runtime application processes do not provision roles, extensions, databases, or partitions that belong to the migration/infrastructure boundary.

The migration connection must use the reduced `migration_owner` LOGIN. `migration_owner` is not an API, auth, worker, or maintenance runtime identity and must not receive those capabilities.

## Pre-deployment hard gate

Before changing a production database:

1. Freeze the exact application candidate to be deployed.
2. Require the complete hardening workflow matrix for that unchanged candidate to be green.
3. Verify the repository Alembic graph has exactly one root, one head, no unresolved `down_revision`, and no unreachable revision:

   ```bash
   python -s scripts/verify_alembic_graph.py
   ```

4. Run the migration semantics/risk gate:

   ```bash
   python -s scripts/migration_semantics_gate.py
   ```

5. Provision/verify the canonical external PostgreSQL role contract using `security/cluster_role_bootstrap/` and the corresponding verification scripts.
6. Verify the P2C identity graph before Alembic mutation:

   ```bash
   python -s scripts/verify_cluster_role_bootstrap.py
   python -s scripts/verify_cluster_identity_graph.py
   ```

7. Ensure infrastructure-owned extensions required by the lineage already exist. In CI the canonical provisioning helper is `scripts/ci/provision_infrastructure_extensions.sh`; production infrastructure should perform the equivalent privileged operation outside the application migration role.
8. Confirm backup/restore capability and the operational recovery point appropriate for the release. Do not rely on an untested SQL downgrade as the only recovery mechanism.

If any precondition fails, stop. Do not compensate with temporary SUPERUSER, BYPASSRLS, broad grants, `CASCADE`, or a second migration lineage.

## Apply HEAD

Run Alembic only through the reduced migration identity:

```bash
python -s -m alembic -c alembic.ini upgrade head
python -s -m alembic -c alembic.ini current --check-heads
```

Then immediately re-run the role and identity graph verification:

```bash
python -s scripts/verify_cluster_role_bootstrap.py
python -s scripts/verify_cluster_identity_graph.py
```

The post-HEAD result must still match the canonical cluster contract. A migration that silently changes runtime membership, dangerous role attributes, or role settings is a failed deployment even if Alembic reports success.

## Application rollout

After HEAD is verified:

1. Start each production process with only the database secrets allowed by `security/runtime_identity/process_profiles.v1.json`.
2. API/auth, ordinary worker, lifecycle maintenance, and beat remain separate process profiles.
3. Runtime principal attestation must pass before a process serves traffic or consumes work.
4. Beat/control processes receive no database credential.
5. Keep RLS enabled/forced on governed tenant data. Tenant context must remain transaction-local.

See `docs/operations/runtime-processes.md` and `docs/operations/database-identities.md`.

## Failure and rollback policy

Do not automatically run `alembic downgrade` because an application rollout fails. First classify the failure:

- **Application-only failure:** roll back the application candidate while keeping a backward-compatible schema when the release contract permits it.
- **Migration failed before commit / transactional DDL rollback:** verify the database state and Alembic revision before retrying.
- **Migration completed but schema/data must be reversed:** use only the downgrade/restore path already proven by the migration lifecycle and data-preservation gates for that lineage.
- **Potential data loss or irreversible semantic change:** prefer the tested restore/recovery procedure over improvising a destructive downgrade.
- **Identity/RLS drift:** stop runtime rollout, restore the canonical role/settings/membership contract, and investigate the migration/root cause. Do not weaken security to recover service.

Every recovery action must preserve auditability and tenant isolation.

## Forbidden shortcuts

Production migration work must not:

- run as an application runtime login;
- grant SUPERUSER, CREATEDB, CREATEROLE, REPLICATION, or BYPASSRLS to make a migration pass;
- convert NOLOGIN capability roles into LOGIN roles;
- disable/relax RLS to bypass a failing test or migration;
- use broad `GRANT ALL` as a compatibility bridge;
- use `DROP ... CASCADE`/`TRUNCATE ... CASCADE` as a cleanup workaround unless an explicitly reviewed migration contract requires it;
- create extensions from normal runtime code;
- edit historical migrations merely to make a fresh database pass when a forward corrective migration is the safe compatibility path;
- merge or deploy based on one green workflow while another required hardening lane is red.

## Evidence to retain

For each release retain, at minimum:

- application Git SHA;
- Alembic head/revision evidence;
- complete required workflow conclusions;
- PostgreSQL server/extension versions from certification;
- cluster-role/bootstrap verification output;
- identity-graph verification output;
- migration data-preservation and adversarial-safety evidence;
- resolved dependency evidence for the certified test environment.

P2E's `P2E Production Contract Consolidation` workflow uploads the reproducibility evidence artifact used by the hardening process.

import pytest
from sqlalchemy import UniqueConstraint, text

from app.models.outbox import TransactionalOutbox


@pytest.mark.asyncio
async def test_audit_key_registry_bootstrap(db_session):
    """The migration-owned key registry is readable, not writable, at runtime."""
    result = await db_session.execute(
        text(
            """
            SELECT key_version, kms_key_alias, algorithm,
                   digest_algorithm, signature_algorithm, is_active
            FROM public.audit_key_registry
            WHERE key_version = 1
            """
        )
    )
    key = result.fetchone()

    assert key is not None
    assert key[0] == 1
    assert key[1] == "local/audit-signing-key-v1"
    assert key[2] == "aes-256-gcm"
    assert key[3] == "sha-256"
    assert key[4] == "hmac-sha-256"
    assert key[5] is True

    privileges = (
        await db_session.execute(
            text(
                """
                SELECT
                    pg_catalog.has_table_privilege(
                        current_user,
                        'public.audit_key_registry',
                        'SELECT'
                    ),
                    pg_catalog.has_table_privilege(
                        current_user,
                        'public.audit_key_registry',
                        'INSERT'
                    ),
                    pg_catalog.has_table_privilege(
                        current_user,
                        'public.audit_key_registry',
                        'UPDATE'
                    ),
                    pg_catalog.has_table_privilege(
                        current_user,
                        'public.audit_key_registry',
                        'DELETE'
                    )
                """
            )
        )
    ).one()
    assert privileges == (True, False, False, False)


@pytest.mark.asyncio
async def test_transactional_outbox_model(db_session):
    """Verify ORM/database parity without bypassing the outbox write boundary.

    API runtime is intentionally unable to INSERT directly into the FORCE-RLS
    outbox. Producers must use the database-owned enqueue capabilities. This
    test therefore checks the catalog contract and ORM metadata instead of
    manufacturing a privileged write path solely for a model test.
    """
    constraints = {
        constraint.name: constraint
        for constraint in TransactionalOutbox.__table__.constraints
        if constraint.name is not None
    }
    dedupe = constraints["uq_outbox_dedupe"]
    assert isinstance(dedupe, UniqueConstraint)
    assert [column.name for column in dedupe.columns] == [
        "event_type",
        "dedupe_key",
    ]
    assert TransactionalOutbox.__table__.c.tenant_id.nullable is False
    assert TransactionalOutbox.__table__.c.correlation_id.nullable is False

    posture = (
        await db_session.execute(
            text(
                """
                SELECT
                    relation.relrowsecurity AS rls_enabled,
                    relation.relforcerowsecurity AS rls_forced,
                    pg_catalog.has_table_privilege(
                        current_user,
                        relation.oid,
                        'INSERT'
                    ) AS runtime_can_insert,
                    constraint_data.contype::text AS constraint_type,
                    constraint_data.convalidated AS constraint_validated,
                    ARRAY(
                        SELECT attribute_data.attname::text
                        FROM pg_catalog.unnest(
                            constraint_data.conkey
                        ) WITH ORDINALITY AS key_column(attnum, position)
                        JOIN pg_catalog.pg_attribute AS attribute_data
                          ON attribute_data.attrelid = constraint_data.conrelid
                         AND attribute_data.attnum = key_column.attnum
                        ORDER BY key_column.position
                    ) AS constraint_columns
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_constraint AS constraint_data
                  ON constraint_data.conrelid = relation.oid
                 AND constraint_data.conname = 'uq_outbox_dedupe'
                WHERE namespace.nspname = 'public'
                  AND relation.relname = 'transactional_outbox'
                """
            )
        )
    ).one()

    assert posture.rls_enabled is True
    assert posture.rls_forced is True
    assert posture.runtime_can_insert is False
    assert posture.constraint_type == "u"
    assert posture.constraint_validated is True
    assert tuple(posture.constraint_columns) == ("event_type", "dedupe_key")


@pytest.mark.asyncio
async def test_rls_policies_staff_roles(db_session):
    """The canonical staff-role table must retain enforced RLS at runtime."""
    posture = (
        await db_session.execute(
            text(
                """
                SELECT
                    relation.relrowsecurity,
                    relation.relforcerowsecurity,
                    pg_catalog.has_table_privilege(
                        current_user,
                        relation.oid,
                        'SELECT'
                    ) AS runtime_can_select,
                    (
                        SELECT count(*)
                        FROM pg_catalog.pg_policy AS policy
                        WHERE policy.polrelid = relation.oid
                    ) AS policy_count
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND relation.relname = 'branch_staff_roles'
                """
            )
        )
    ).one()

    assert posture.relrowsecurity is True
    assert posture.relforcerowsecurity is True
    assert posture.runtime_can_select is True
    assert posture.policy_count >= 1


@pytest.mark.asyncio
async def test_ensure_future_partition_function(db_session):
    """Test Phase 18 - Partition Automation Function"""
    result = await db_session.execute(
        text(
            """
            SELECT namespace.nspname, procedure.proname, procedure.prosecdef
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname = 'app_private'
              AND procedure.proname = 'ensure_future_partition'
            """
        )
    )
    row = result.fetchone()
    assert row is not None
    assert row[0] == "app_private"
    assert row[1] == "ensure_future_partition"
    assert row[2] is True

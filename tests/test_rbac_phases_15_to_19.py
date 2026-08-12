import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

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
async def test_transactional_outbox_model(admin_db_session):
    """Verify the legacy outbox model and dedupe constraint in isolation.

    The table has no ordinary app-runtime write contract, so this schema/model
    regression deliberately uses the explicit privileged test-harness session
    rather than widening production ACLs. A unique key removes destructive
    shared-state cleanup.
    """
    dedupe_key = f"rbac-phase19-{uuid.uuid4()}"
    event_type = "test.rbac.phase19"

    outbox_event = TransactionalOutbox(
        event_type=event_type,
        payload={"foo": "bar"},
        dedupe_key=dedupe_key,
    )
    admin_db_session.add(outbox_event)
    await admin_db_session.commit()

    result = await admin_db_session.execute(
        text(
            """
            SELECT delivery_attempts
            FROM public.transactional_outbox
            WHERE event_type = :event_type
              AND dedupe_key = :dedupe_key
            """
        ),
        {"event_type": event_type, "dedupe_key": dedupe_key},
    )
    assert result.scalar_one() == 0

    admin_db_session.add(
        TransactionalOutbox(
            event_type=event_type,
            payload={"duplicate": True},
            dedupe_key=dedupe_key,
        )
    )
    with pytest.raises(IntegrityError):
        await admin_db_session.commit()
    await admin_db_session.rollback()


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
                    ),
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
    assert posture.has_table_privilege is True
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

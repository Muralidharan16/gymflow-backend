from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import update_session_context


_TEST_RUNNER = "test_runner"
_RELATION = "public.org_branch_state"
_POLICY = "pytest_org_branch_state_cleanup"
_FORBIDDEN_CAPABILITIES = (
    "app_runtime",
    "app_user",
    "auth_runtime",
    "worker_runtime",
    "lifecycle_maintenance_runtime",
)


async def delete_org_branch_state_fixture(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
) -> None:
    """Delete one test tenant's branch state without weakening production RLS.

    ``org_branch_state`` is FORCE RLS. Its production DELETE policy is scoped to
    ``app_runtime``, while the ordinary runtime intentionally has no DELETE ACL.
    The reduced migration owner therefore cannot use ownership as an RLS bypass
    during teardown, and granting a production identity DELETE merely for tests
    would invalidate the security boundary.

    ``test_runner`` is an external test-only role. This helper lends it the
    minimum DELETE + ``org_id`` read surface and a tenant-bound DELETE policy
    inside the current PostgreSQL transaction. PostgreSQL's transactional DDL
    makes the capability private to this uncommitted teardown transaction; the
    helper also explicitly drops/revokes it before the caller commits.
    """

    await session.execute(text("RESET ROLE"))

    identity = (
        await session.execute(
            text(
                """
                SELECT
                    current_database()::text AS database_name,
                    session_user::text AS session_name,
                    current_user::text AS current_name,
                    migration_role.rolsuper AS migration_superuser,
                    migration_role.rolinherit AS migration_inherit,
                    migration_role.rolcreatedb AS migration_createdb,
                    migration_role.rolcreaterole AS migration_createrole,
                    migration_role.rolreplication AS migration_replication,
                    migration_role.rolbypassrls AS migration_bypassrls,
                    runner_role.rolsuper AS runner_superuser,
                    runner_role.rolinherit AS runner_inherit,
                    runner_role.rolcreatedb AS runner_createdb,
                    runner_role.rolcreaterole AS runner_createrole,
                    runner_role.rolreplication AS runner_replication,
                    runner_role.rolbypassrls AS runner_bypassrls,
                    pg_catalog.pg_has_role(
                        session_user,
                        :runner_role,
                        'SET'
                    ) AS can_set_runner,
                    pg_catalog.has_table_privilege(
                        :runner_role,
                        :relation,
                        'DELETE'
                    ) AS runner_preexisting_delete,
                    pg_catalog.has_column_privilege(
                        :runner_role,
                        :relation,
                        'org_id',
                        'SELECT'
                    ) AS runner_preexisting_org_read
                FROM pg_catalog.pg_roles AS migration_role
                CROSS JOIN pg_catalog.pg_roles AS runner_role
                WHERE migration_role.rolname = session_user
                  AND runner_role.rolname = :runner_role
                """
            ),
            {"runner_role": _TEST_RUNNER, "relation": _RELATION},
        )
    ).mappings().one_or_none()

    if identity is None:
        raise RuntimeError("pytest cleanup requires migration_owner and test_runner")
    if "test" not in str(identity["database_name"]).lower():
        raise RuntimeError(
            "Refusing forced-RLS fixture cleanup outside a disposable test database"
        )
    if (
        identity["session_name"] != "migration_owner"
        or identity["current_name"] != "migration_owner"
    ):
        raise RuntimeError(
            "forced-RLS fixture cleanup must start as reduced migration_owner"
        )
    if any(
        bool(identity[name])
        for name in (
            "migration_superuser",
            "migration_inherit",
            "migration_createdb",
            "migration_createrole",
            "migration_replication",
            "migration_bypassrls",
            "runner_superuser",
            "runner_inherit",
            "runner_createdb",
            "runner_createrole",
            "runner_replication",
            "runner_bypassrls",
        )
    ):
        raise RuntimeError("pytest cleanup identity posture is over-privileged")
    if not bool(identity["can_set_runner"]):
        raise RuntimeError("migration_owner lacks the bounded SET test_runner edge")
    if bool(identity["runner_preexisting_delete"]):
        raise RuntimeError("test_runner unexpectedly already has branch-state DELETE")
    if bool(identity["runner_preexisting_org_read"]):
        raise RuntimeError("test_runner unexpectedly already reads branch-state org_id")

    for forbidden_role in _FORBIDDEN_CAPABILITIES:
        leaked = (
            await session.execute(
                text(
                    """
                    SELECT
                        pg_catalog.pg_has_role(:runner_role, :forbidden_role, 'MEMBER')
                        OR pg_catalog.pg_has_role(:runner_role, :forbidden_role, 'SET')
                    """
                ),
                {
                    "runner_role": _TEST_RUNNER,
                    "forbidden_role": forbidden_role,
                },
            )
        ).scalar_one()
        if bool(leaked):
            raise RuntimeError(
                f"test_runner leaked into production capability {forbidden_role}"
            )

    policy_collision = (
        await session.execute(
            text(
                """
                SELECT 1
                FROM pg_catalog.pg_policy
                WHERE polrelid = CAST(:relation AS regclass)
                  AND polname = :policy_name
                """
            ),
            {"relation": _RELATION, "policy_name": _POLICY},
        )
    ).scalar_one_or_none()
    if policy_collision is not None:
        raise RuntimeError(f"unexpected pre-existing pytest cleanup policy: {_POLICY}")

    await update_session_context(
        session,
        principal_id=str(actor_id),
        principal_type="legacy_gym_owner",
        org_id=str(org_id),
        trace_id="pytest-forced-rls-cleanup",
        role="superadmin",
    )

    await session.execute(
        text("GRANT DELETE ON TABLE public.org_branch_state TO test_runner")
    )
    await session.execute(
        text("GRANT SELECT (org_id) ON TABLE public.org_branch_state TO test_runner")
    )
    await session.execute(
        text(
            """
            CREATE POLICY pytest_org_branch_state_cleanup
            ON public.org_branch_state
            FOR DELETE TO test_runner
            USING (
                org_id = NULLIF(
                    current_setting('app.current_org_id', true),
                    ''
                )::uuid
                AND current_setting('app.current_role', true) = 'superadmin'
            )
            """
        )
    )

    await session.execute(text("SET LOCAL ROLE test_runner"))
    runner_context = (
        await session.execute(
            text(
                """
                SELECT
                    session_user::text,
                    current_user::text,
                    current_setting('app.current_org_id', true),
                    current_setting('app.current_role', true)
                """
            )
        )
    ).one()
    if tuple(runner_context) != (
        "migration_owner",
        _TEST_RUNNER,
        str(org_id),
        "superadmin",
    ):
        raise RuntimeError(
            f"pytest cleanup role/context drifted: {tuple(runner_context)!r}"
        )

    await session.execute(
        text("DELETE FROM public.org_branch_state WHERE org_id = :org_id"),
        {"org_id": org_id},
    )

    await session.execute(text("RESET ROLE"))
    current_name = (
        await session.execute(text("SELECT current_user::text"))
    ).scalar_one()
    if current_name != "migration_owner":
        raise RuntimeError("pytest cleanup failed to restore migration_owner")

    await session.execute(
        text("DROP POLICY pytest_org_branch_state_cleanup ON public.org_branch_state")
    )
    await session.execute(
        text("REVOKE SELECT (org_id) ON TABLE public.org_branch_state FROM test_runner")
    )
    await session.execute(
        text("REVOKE DELETE ON TABLE public.org_branch_state FROM test_runner")
    )

    postcondition = (
        await session.execute(
            text(
                """
                SELECT
                    pg_catalog.has_table_privilege(
                        :runner_role,
                        :relation,
                        'DELETE'
                    ) AS has_delete,
                    pg_catalog.has_column_privilege(
                        :runner_role,
                        :relation,
                        'org_id',
                        'SELECT'
                    ) AS has_org_read,
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_policy
                        WHERE polrelid = CAST(:relation AS regclass)
                          AND polname = :policy_name
                    ) AS policy_exists
                """
            ),
            {
                "runner_role": _TEST_RUNNER,
                "relation": _RELATION,
                "policy_name": _POLICY,
            },
        )
    ).mappings().one()
    if (
        bool(postcondition["has_delete"])
        or bool(postcondition["has_org_read"])
        or bool(postcondition["policy_exists"])
    ):
        raise RuntimeError(
            "pytest cleanup capability was not fully removed before commit"
        )

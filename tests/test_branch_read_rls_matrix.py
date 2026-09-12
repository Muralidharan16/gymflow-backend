from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal, update_session_context
from app.models.enums import StaffRole
from app.models.organization import Organization
from app.models.org_branch import OrgBranch, OrgBranchState
from app.models.staff import GymOwner
from conftest import AdminTestSessionLocal, AuthTestSessionLocal, assert_test_database


STATUSES = (
    "active",
    "temporarily_closed",
    "under_renovation",
    "compliance_suspended",
    "permanently_closed",
)

EXPECTED_BY_ROLE = {
    "owner": set(STATUSES),
    "admin": set(STATUSES),
    "manager": {"active", "temporarily_closed", "under_renovation"},
    "trainer": {"active"},
    "compliance": set(STATUSES),
    "superadmin": set(STATUSES),
}


async def _set_context(session, org_id: uuid.UUID, actor_id: uuid.UUID, role: str) -> None:
    await update_session_context(
        session,
        principal_id=str(actor_id),
        principal_type="legacy_gym_owner",
        org_id=str(org_id),
        trace_id="branch-read-rls-matrix",
        role=role,
    )


async def _seed_matrix_branch_state_without_returning(
    session,
    *,
    branch_id: uuid.UUID,
    org_id: uuid.UUID,
    lifecycle_status: str,
    status_reason: str | None,
    search_epoch_ulid: str,
) -> None:
    """Seed synthetic lifecycle state without widening auth RETURNING visibility.

    The read matrix needs states that are not onboarding's canonical initial
    active/primary state. Auth already owns the tenant-bound INSERT capability,
    but C87 deliberately limits auth SELECT/RETURNING visibility to that one
    canonical onboarding shape. Fixture setup therefore inserts these synthetic
    rows without RETURNING instead of manufacturing broader SELECT/RLS rights.
    """
    await session.execute(
        text(
            """
            INSERT INTO public.org_branch_state (
                branch_id,
                org_id,
                branch_status,
                status,
                is_primary,
                is_operational,
                status_reason,
                transition_source,
                watchdog_recovery_count,
                search_visibility_version,
                search_epoch_ulid
            ) VALUES (
                :branch_id,
                :org_id,
                'active',
                :lifecycle_status,
                FALSE,
                TRUE,
                :status_reason,
                'api',
                0,
                1,
                :search_epoch_ulid
            )
            """
        ),
        {
            "branch_id": branch_id,
            "org_id": org_id,
            "lifecycle_status": lifecycle_status,
            "status_reason": status_reason,
            "search_epoch_ulid": search_epoch_ulid,
        },
    )


@pytest_asyncio.fixture
async def branch_read_matrix_fixture():
    """Seed one UUID-isolated tenant matrix in the disposable CI database.

    Branch roots intentionally have no production hard-delete capability. The
    fixture therefore leaves its UUID-scoped rows for database teardown instead
    of manufacturing test-only grants, temporary RLS policies, SET ROLE edges,
    or hidden cascades that do not exist in production.

    Branch roots are flushed one at a time so SQLAlchemy cannot turn the fixture
    into a multi-row INSERT ... RETURNING that asks auth for unrelated branch-id
    read authority. Synthetic lifecycle states use the existing bounded auth
    INSERT policy without RETURNING; all matrix assertions still execute through
    the reduced ordinary application runtime identity.
    """
    assert AuthTestSessionLocal is not None

    org_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    branch_ids: dict[str, uuid.UUID] = {}

    async with AdminTestSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        await assert_test_database(session)
        session.add(
            Organization(
                id=org_id,
                name=f"Branch Read Matrix {org_id.hex[:12]}",
                max_branches=10,
            )
        )
        await session.flush()
        await _set_context(session, org_id, owner_id, "owner")
        session.add(
            GymOwner(
                id=owner_id,
                org_id=org_id,
                name="Matrix Owner",
                email=f"matrix-{uuid.uuid4().hex[:8]}@test.com",
                password_hash="hash",
                role=StaffRole.owner,
                is_active=True,
                is_verified=True,
            )
        )
        await session.commit()

    async with AuthTestSessionLocal() as session:
        await assert_test_database(session)
        await _set_context(session, org_id, owner_id, "owner")

        for index, lifecycle_status in enumerate(STATUSES, start=1):
            branch_id = uuid.uuid4()
            branch_ids[lifecycle_status] = branch_id
            session.add(
                OrgBranch(
                    id=branch_id,
                    org_id=org_id,
                    branch_name=f"Matrix {lifecycle_status}",
                    branch_code=f"MX-{index}",
                    internal_slug=f"mx-{index}",
                    created_by=owner_id,
                )
            )

            # Flush each root separately. A batch flush adds the PK as an
            # insertmanyvalues RETURNING sentinel, which is intentionally outside
            # the narrow C57 auth branch-returning contract.
            await session.flush()

            await _seed_matrix_branch_state_without_returning(
                session,
                branch_id=branch_id,
                org_id=org_id,
                lifecycle_status=lifecycle_status,
                status_reason=(
                    "Matrix terminal status"
                    if lifecycle_status in {"compliance_suspended", "permanently_closed"}
                    else None
                ),
                search_epoch_ulid=f"01AN4V07BY79KA1307SR1XF3{index}",
            )

        await session.commit()

    yield {"org_id": org_id, "owner_id": owner_id, "branch_ids": branch_ids}


@pytest.mark.asyncio
@pytest.mark.parametrize("role", tuple(EXPECTED_BY_ROLE))
async def test_branch_state_visibility_matches_application_matrix(
    branch_read_matrix_fixture,
    role: str,
) -> None:
    org_id = branch_read_matrix_fixture["org_id"]
    owner_id = branch_read_matrix_fixture["owner_id"]

    async with AsyncSessionLocal() as session:
        await _set_context(session, org_id, owner_id, role)
        result = await session.execute(
            select(OrgBranchState.status).where(OrgBranchState.org_id == org_id)
        )
        visible = set(result.scalars().all())

    assert visible == EXPECTED_BY_ROLE[role]


@pytest.mark.asyncio
async def test_branch_state_matrix_still_denies_cross_tenant_reads(
    branch_read_matrix_fixture,
) -> None:
    org_id = branch_read_matrix_fixture["org_id"]
    owner_id = branch_read_matrix_fixture["owner_id"]
    foreign_org_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        await _set_context(session, foreign_org_id, owner_id, "owner")
        result = await session.execute(
            select(OrgBranchState.branch_id).where(OrgBranchState.org_id == org_id)
        )
        assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_security_invoker_view_is_readable_but_not_cross_tenant(
    branch_read_matrix_fixture,
) -> None:
    org_id = branch_read_matrix_fixture["org_id"]
    owner_id = branch_read_matrix_fixture["owner_id"]

    async with AsyncSessionLocal() as session:
        await _set_context(session, org_id, owner_id, "owner")
        visible = await session.execute(
            text(
                """
                SELECT id
                  FROM public.v_active_org_branches
                 WHERE org_id = :org_id
                """
            ),
            {"org_id": org_id},
        )
        assert len(visible.fetchall()) == len(STATUSES)

        foreign_org_id = uuid.uuid4()
        await _set_context(session, foreign_org_id, owner_id, "owner")
        hidden = await session.execute(
            text(
                """
                SELECT id
                  FROM public.v_active_org_branches
                 WHERE org_id = :org_id
                """
            ),
            {"org_id": org_id},
        )
        assert hidden.fetchall() == []

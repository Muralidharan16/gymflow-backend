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


@pytest_asyncio.fixture
async def branch_read_matrix_fixture():
    """Seed one UUID-isolated tenant matrix in the disposable CI database.

    Branch roots intentionally have no production hard-delete capability. The
    fixture therefore leaves its UUID-scoped rows for database teardown instead
    of manufacturing test-only grants, temporary RLS policies, SET ROLE edges,
    or hidden cascades that do not exist in production.
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
            branch = OrgBranch(
                id=branch_id,
                org_id=org_id,
                branch_name=f"Matrix {lifecycle_status}",
                branch_code=f"MX-{index}",
                internal_slug=f"mx-{index}",
                created_by=owner_id,
            )
            branch.state = OrgBranchState(
                branch_id=branch_id,
                org_id=org_id,
                branch_status="active",
                status=lifecycle_status,
                status_reason=(
                    "Matrix terminal status"
                    if lifecycle_status in {"compliance_suspended", "permanently_closed"}
                    else None
                ),
                search_epoch_ulid=f"01AN4V07BY79KA1307SR1XF3{index}",
            )
            session.add(branch)

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

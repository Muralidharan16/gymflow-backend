import pytest
import uuid
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.exc import StaleDataError

from app.models.organization import Organization
from app.models.staff import GymOwner
from app.models.enums import StaffRole
from app.models.org_branch import OrgBranch, OrgBranchState
from app.repositories.branch_repo import BranchRepository
from app.core.database import AsyncSessionLocal, update_session_context
from conftest import (
    AdminTestSessionLocal,
    AuthTestSessionLocal,
    assert_test_database,
)


async def set_tenant_context(
    session,
    org_id: str,
    user_id: str,
    role: str = "owner",
) -> None:
    """Apply the same typed tenant/principal context used by production.

    The reduced runtime login already exists before pytest starts. Tests must not
    create ad-hoc PostgreSQL roles, SET ROLE, or grant themselves table access.
    Context is transaction-local and is re-applied after commits by the normal
    Session.info/after_begin production mechanism.
    """
    await update_session_context(
        session,
        principal_id=user_id,
        principal_type="legacy_gym_owner",
        org_id=org_id,
        trace_id="branch-management-test",
        role=role,
    )


async def create_branch_via_bounded_bootstrap(
    *,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    branch: OrgBranch,
) -> None:
    """Create a branch only through the bounded auth/bootstrap DB identity."""
    assert AuthTestSessionLocal is not None, (
        "branch creation tests require the dedicated bounded auth test identity"
    )
    async with AuthTestSessionLocal() as session:
        await assert_test_database(session)
        await set_tenant_context(session, str(org_id), str(owner_id), "owner")
        repo = BranchRepository(session)
        await repo.create(branch)
        await session.commit()


async def cleanup_branch_management_fixture(
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Remove only this fixture's rows without CASCADE or runtime privilege drift."""
    async with AdminTestSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        await assert_test_database(session)
        await set_tenant_context(session, str(org_id), str(actor_id), "superadmin")

        # Delete in explicit FK order. Do not use the shared TRUNCATE ... CASCADE
        # helper: branch_contacts and other protected relations intentionally do
        # not grant broad destructive privileges to the migration/test identity.
        await session.execute(
            text("DELETE FROM public.org_branch_state WHERE org_id = :org_id"),
            {"org_id": org_id},
        )
        await session.execute(
            text("DELETE FROM public.org_branches WHERE org_id = :org_id"),
            {"org_id": org_id},
        )
        await session.execute(
            text("DELETE FROM public.gym_owners WHERE org_id = :org_id"),
            {"org_id": org_id},
        )
        await session.execute(
            text("DELETE FROM public.organizations WHERE id = :org_id"),
            {"org_id": org_id},
        )
        await session.commit()


@pytest_asyncio.fixture
async def test_setup():
    """Seed tenant/staff prerequisites administratively, then test as runtime."""
    org_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    trainer_id = uuid.uuid4()

    # Tenant-root and legacy identity fixture creation are intentionally outside
    # the ordinary runtime contract. The disposable admin identity is used only
    # for prerequisites and receives explicit tenant context before tenant rows.
    async with AdminTestSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        await assert_test_database(session)

        org = Organization(id=org_id, name="Test Enterprise Org", max_branches=2)
        session.add(org)
        await session.flush()

        await set_tenant_context(session, str(org_id), str(owner_id), "owner")

        owner = GymOwner(
            id=owner_id,
            org_id=org_id,
            name="Test Owner",
            email=f"owner-{uuid.uuid4().hex[:8]}@test.com",
            password_hash="hash",
            role=StaffRole.owner,
            is_active=True,
            is_verified=True,
        )
        admin = GymOwner(
            id=admin_id,
            org_id=org_id,
            name="Test Admin",
            email=f"admin-{uuid.uuid4().hex[:8]}@test.com",
            password_hash="hash",
            role=StaffRole.admin,
            is_active=True,
            is_verified=True,
        )
        trainer = GymOwner(
            id=trainer_id,
            org_id=org_id,
            name="Test Trainer",
            email=f"trainer-{uuid.uuid4().hex[:8]}@test.com",
            password_hash="hash",
            role=StaffRole.trainer,
            is_active=True,
            is_verified=True,
        )
        session.add_all([owner, admin, trainer])
        await session.commit()

    yield {
        "org_id": org_id,
        "owner_id": owner_id,
        "admin_id": admin_id,
        "trainer_id": trainer_id,
    }

    await cleanup_branch_management_fixture(org_id, owner_id)


@pytest.mark.asyncio
async def test_create_and_fetch_active_branch(test_setup):
    org_id = test_setup["org_id"]
    owner_id = test_setup["owner_id"]

    branch_id = uuid.uuid4()
    branch = OrgBranch(
        id=branch_id,
        org_id=org_id,
        branch_name="San Francisco HQ",
        branch_code="SF-01",
        internal_slug="sf-01",
        timezone="America/Los_Angeles",
        currency_code="USD",
        created_by=owner_id,
    )
    branch.state = OrgBranchState(
        branch_id=branch_id,
        org_id=org_id,
        branch_status="active",
        is_primary=True,
        search_epoch_ulid="01AN4V07BY79KA1307SR1XF31A",
    )
    await create_branch_via_bounded_bootstrap(
        org_id=org_id,
        owner_id=owner_id,
        branch=branch,
    )

    # Ordinary application reads stay on the reduced runtime identity.
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(owner_id), "owner")
        repo = BranchRepository(session)

        active_branch = await repo.get_active_by_id(branch_id, org_id)
        assert active_branch is not None
        assert active_branch.branch_name == "San Francisco HQ"
        assert active_branch.branch_status == "active"
        assert active_branch.is_primary is True

        active_list = await repo.list_active(org_id)
        assert len(active_list) == 1
        assert active_list[0].id == branch_id


@pytest.mark.asyncio
async def test_rls_isolation_across_tenants(test_setup):
    org_id = test_setup["org_id"]
    owner_id = test_setup["owner_id"]

    branch_id = uuid.uuid4()
    branch = OrgBranch(
        id=branch_id,
        org_id=org_id,
        branch_name="Branch Org A",
        branch_code="A-01",
        internal_slug="a-01",
    )
    branch.state = OrgBranchState(
        branch_id=branch_id,
        org_id=org_id,
        branch_status="active",
        search_epoch_ulid="01AN4V07BY79KA1307SR1XF31A",
    )
    await create_branch_via_bounded_bootstrap(
        org_id=org_id,
        owner_id=owner_id,
        branch=branch,
    )

    # A different tenant context must not observe Org A through either the
    # security-invoker view or the base table.
    other_org_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        await set_tenant_context(
            session,
            str(other_org_id),
            str(uuid.uuid4()),
            "owner",
        )
        repo = BranchRepository(session)

        visible_branch = await repo.get_active_by_id(branch_id, org_id)
        assert visible_branch is None

        direct_branch = (
            await session.execute(select(OrgBranch).where(OrgBranch.id == branch_id))
        ).scalar_one_or_none()
        assert direct_branch is None

        active_list = await repo.list_active(org_id)
        assert len(active_list) == 0


@pytest.mark.asyncio
async def test_enforce_max_branches_trigger(test_setup):
    org_id = test_setup["org_id"]
    owner_id = test_setup["owner_id"]
    assert AuthTestSessionLocal is not None

    # Branch insertion is a bounded bootstrap capability. Exercise the database
    # max-branch invariant through that real write-capable identity, never by
    # granting ALL to the ordinary runtime.
    async with AuthTestSessionLocal() as session:
        await assert_test_database(session)
        await set_tenant_context(session, str(org_id), str(owner_id), "owner")
        repo = BranchRepository(session)

        b1_id = uuid.uuid4()
        b1 = OrgBranch(
            id=b1_id,
            org_id=org_id,
            branch_name="Branch 1",
            branch_code="B1",
            internal_slug="b1",
        )
        b1.state = OrgBranchState(
            branch_id=b1_id,
            org_id=org_id,
            branch_status="active",
            search_epoch_ulid="01AN4V07BY79KA1307SR1XF31A",
        )
        await repo.create(b1)

        b2_id = uuid.uuid4()
        b2 = OrgBranch(
            id=b2_id,
            org_id=org_id,
            branch_name="Branch 2",
            branch_code="B2",
            internal_slug="b2",
        )
        b2.state = OrgBranchState(
            branch_id=b2_id,
            org_id=org_id,
            branch_status="active",
            search_epoch_ulid="01AN4V07BY79KA1307SR1XF31B",
        )
        await repo.create(b2)
        await session.commit()

        b3_id = uuid.uuid4()
        b3 = OrgBranch(
            id=b3_id,
            org_id=org_id,
            branch_name="Branch 3",
            branch_code="B3",
            internal_slug="b3",
        )
        b3.state = OrgBranchState(
            branch_id=b3_id,
            org_id=org_id,
            branch_status="active",
            search_epoch_ulid="01AN4V07BY79KA1307SR1XF31C",
        )
        session.add(b3)
        with pytest.raises(DBAPIError) as exc_info:
            await session.commit()
        assert "maximum branch limit" in str(exc_info.value)


@pytest.mark.asyncio
async def test_prevent_critical_branch_deletion_trigger(test_setup):
    org_id = test_setup["org_id"]
    owner_id = test_setup["owner_id"]

    b1_id = uuid.uuid4()
    b1 = OrgBranch(
        id=b1_id,
        org_id=org_id,
        branch_name="Only Branch",
        branch_code="OB-01",
        internal_slug="ob-01",
    )
    b1.state = OrgBranchState(
        branch_id=b1_id,
        org_id=org_id,
        branch_status="active",
        is_primary=True,
        search_epoch_ulid="01AN4V07BY79KA1307SR1XF31A",
    )
    await create_branch_via_bounded_bootstrap(
        org_id=org_id,
        owner_id=owner_id,
        branch=b1,
    )

    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(owner_id), "owner")
        repo = BranchRepository(session)

        with pytest.raises(DBAPIError) as exc_info:
            await repo.soft_delete(
                b1_id,
                org_id,
                owner_id,
                reason="Soft deleting sole branch",
            )
        assert (
            "Cannot delete the primary branch" in str(exc_info.value)
            or "last branch" in str(exc_info.value)
        )


@pytest.mark.asyncio
async def test_rbac_privileges_on_branch_actions(test_setup):
    org_id = test_setup["org_id"]
    owner_id = test_setup["owner_id"]
    admin_id = test_setup["admin_id"]
    trainer_id = test_setup["trainer_id"]
    assert AuthTestSessionLocal is not None

    # Seed two branches through the bounded branch-creation identity.
    async with AuthTestSessionLocal() as session:
        await assert_test_database(session)
        await set_tenant_context(session, str(org_id), str(owner_id), "owner")
        repo = BranchRepository(session)

        b1_id = uuid.uuid4()
        b1 = OrgBranch(
            id=b1_id,
            org_id=org_id,
            branch_name="Branch One",
            branch_code="B01",
            internal_slug="b01",
        )
        b1.state = OrgBranchState(
            branch_id=b1_id,
            org_id=org_id,
            branch_status="active",
            is_primary=True,
            search_epoch_ulid="01AN4V07BY79KA1307SR1XF31A",
        )

        b2_id = uuid.uuid4()
        b2 = OrgBranch(
            id=b2_id,
            org_id=org_id,
            branch_name="Branch Two",
            branch_code="B02",
            internal_slug="b02",
        )
        b2.state = OrgBranchState(
            branch_id=b2_id,
            org_id=org_id,
            branch_status="active",
            is_primary=False,
            search_epoch_ulid="01AN4V07BY79KA1307SR1XF31B",
        )

        await repo.create(b1)
        await repo.create(b2)
        await session.commit()

    # Forced RLS rejects trainer/admin UPDATE visibility before the legacy RBAC
    # trigger can run. SQLAlchemy therefore reports a zero-row protected update
    # as StaleDataError. This is the expected least-privilege boundary: do not
    # widen p_branch_update merely to recover legacy trigger-specific messages.
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(trainer_id), "trainer")
        repo = BranchRepository(session)
        with pytest.raises(StaleDataError):
            await repo.soft_delete(
                b2_id,
                org_id,
                trainer_id,
                reason="Trainer delete attempt",
            )

    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(admin_id), "admin")
        repo = BranchRepository(session)
        with pytest.raises(StaleDataError):
            await repo.soft_delete(
                b2_id,
                org_id,
                admin_id,
                reason="Admin delete attempt",
            )

    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(owner_id), "owner")
        repo = BranchRepository(session)
        success = await repo.soft_delete(
            b2_id,
            org_id,
            owner_id,
            reason="Decommissioning branch",
        )
        assert success is True
        await session.commit()

import pytest
import uuid
import pytest_asyncio
from datetime import datetime, timezone
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, InternalError

from app.models.organization import Organization
from app.models.staff import GymOwner
from app.models.enums import StaffRole
from app.models.org_branch import OrgBranch, OrgBranchState, ActiveOrgBranch, BranchAuditLog
from app.repositories.branch_repo import BranchRepository
from app.core.database import AsyncSessionLocal


async def set_tenant_context(session, org_id: str, user_id: str):
    # Set role to a non-superuser to enforce row-level security
    await session.execute(text("SET ROLE test_rls_role"))
    await session.execute(text("SELECT pg_catalog.set_config('app.current_org_id', :oid, true)"), {"oid": org_id})
    await session.execute(text("SELECT pg_catalog.set_config('app.current_user_id', :uid, true)"), {"uid": user_id})


@pytest_asyncio.fixture
async def test_setup():
    """Set up organization, owner, and branch for testing."""
    async with AsyncSessionLocal() as session:
        # Create non-superuser role for testing RLS if not exists
        await session.execute(text("""
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'test_rls_role') THEN
                CREATE ROLE test_rls_role;
              END IF;
            END
            $$;
        """))
        await session.execute(text("GRANT USAGE ON SCHEMA public TO test_rls_role"))
        await session.execute(text("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO test_rls_role"))
        await session.execute(text("GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO test_rls_role"))
        await session.commit()

    async with AsyncSessionLocal() as session:
        # Create organization
        org_id = uuid.uuid4()
        org = Organization(id=org_id, name="Test Enterprise Org", max_branches=2)
        session.add(org)

        # Create staff members
        owner_id = uuid.uuid4()
        owner = GymOwner(
            id=owner_id,
            org_id=org_id,
            name="Test Owner",
            email="owner@test.com",
            password_hash="hash",
            role=StaffRole.owner,
            is_active=True,
            is_verified=True
        )
        
        admin_id = uuid.uuid4()
        admin = GymOwner(
            id=admin_id,
            org_id=org_id,
            name="Test Admin",
            email="admin@test.com",
            password_hash="hash",
            role=StaffRole.admin,
            is_active=True,
            is_verified=True
        )
        
        trainer_id = uuid.uuid4()
        trainer = GymOwner(
            id=trainer_id,
            org_id=org_id,
            name="Test Trainer",
            email="trainer@test.com",
            password_hash="hash",
            role=StaffRole.trainer,
            is_active=True,
            is_verified=True
        )
        
        session.add_all([owner, admin, trainer])
        await session.commit()
        
        yield {
            "org_id": org_id,
            "owner_id": owner_id,
            "admin_id": admin_id,
            "trainer_id": trainer_id
        }

    # Cleanup
    async with AsyncSessionLocal() as clean_session:
        # Ensure role is reset to superuser before running cleanup commands
        await clean_session.execute(text("RESET ROLE"))
        await clean_session.execute(text("DELETE FROM branch_audit_log WHERE org_id = :oid"), {"oid": org_id})
        await clean_session.execute(text("DELETE FROM org_branch_state WHERE org_id = :oid"), {"oid": org_id})
        await clean_session.execute(text("DELETE FROM org_branches WHERE org_id = :oid"), {"oid": org_id})
        await clean_session.execute(text("DELETE FROM gym_owners WHERE org_id = :oid"), {"oid": org_id})
        await clean_session.execute(text("DELETE FROM organizations WHERE id = :oid"), {"oid": org_id})
        await clean_session.commit()


@pytest.mark.asyncio
async def test_create_and_fetch_active_branch(test_setup):
    org_id = test_setup["org_id"]
    owner_id = test_setup["owner_id"]

    async with AsyncSessionLocal() as session:
        # Set tenant context to owner
        await set_tenant_context(session, str(org_id), str(owner_id))

        repo = BranchRepository(session)
        
        # Create Branch
        branch_id = uuid.uuid4()
        branch = OrgBranch(
            id=branch_id,
            org_id=org_id,
            branch_name="San Francisco HQ",
            branch_code="SF-01",
            internal_slug="sf-01",
            timezone="America/Los_Angeles",
            currency_code="USD",
            created_by=owner_id
        )
        
        state = OrgBranchState(
            branch_id=branch_id,
            org_id=org_id,
            branch_status="active",
            is_primary=True,
            search_epoch_ulid="01AN4V07BY79KA1307SR1XF31A"
        )
        branch.state = state
        
        await repo.create(branch)
        await session.commit()

    # Verify RLS isolated view selects the branch
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(owner_id))
        repo = BranchRepository(session)
        
        active_branch = await repo.get_active_by_id(branch_id, org_id)
        assert active_branch is not None
        assert active_branch.branch_name == "San Francisco HQ"
        assert active_branch.branch_status == "active"
        assert active_branch.is_primary is True
        
        # Test listing active branches
        active_list = await repo.list_active(org_id)
        assert len(active_list) == 1
        assert active_list[0].id == branch_id


@pytest.mark.asyncio
async def test_rls_isolation_across_tenants(test_setup):
    org_id = test_setup["org_id"]
    owner_id = test_setup["owner_id"]

    # Create a branch in org A
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(owner_id))
        repo = BranchRepository(session)
        branch_id = uuid.uuid4()
        branch = OrgBranch(
            id=branch_id,
            org_id=org_id,
            branch_name="Branch Org A",
            branch_code="A-01",
            internal_slug="a-01"
        )
        state = OrgBranchState(
            branch_id=branch_id,
            org_id=org_id,
            branch_status="active",
            search_epoch_ulid="01AN4V07BY79KA1307SR1XF31A"
        )
        branch.state = state
        await repo.create(branch)
        await session.commit()

    # Query with another tenant's org context
    other_org_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(other_org_id), str(uuid.uuid4()))
        repo = BranchRepository(session)
        
        # 1. Should not find the branch from Org A via the view
        branch = await repo.get_active_by_id(branch_id, org_id)
        assert branch is None
        
        # 2. Should not find the branch from Org A via direct table access
        direct_branch = (await session.execute(
            select(OrgBranch).where(OrgBranch.id == branch_id)
        )).scalar_one_or_none()
        assert direct_branch is None

        # 3. List should return empty
        active_list = await repo.list_active(org_id)
        assert len(active_list) == 0


@pytest.mark.asyncio
async def test_enforce_max_branches_trigger(test_setup):
    org_id = test_setup["org_id"]
    owner_id = test_setup["owner_id"]

    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(owner_id))
        repo = BranchRepository(session)
        
        # Create Branch 1
        b1_id = uuid.uuid4()
        b1 = OrgBranch(id=b1_id, org_id=org_id, branch_name="Branch 1", branch_code="B1", internal_slug="b1")
        b1.state = OrgBranchState(branch_id=b1_id, org_id=org_id, branch_status="active", search_epoch_ulid="01AN4V07BY79KA1307SR1XF31A")
        await repo.create(b1)
        
        # Create Branch 2
        b2_id = uuid.uuid4()
        b2 = OrgBranch(id=b2_id, org_id=org_id, branch_name="Branch 2", branch_code="B2", internal_slug="b2")
        b2.state = OrgBranchState(branch_id=b2_id, org_id=org_id, branch_status="active", search_epoch_ulid="01AN4V07BY79KA1307SR1XF31B")
        await repo.create(b2)
        
        await session.commit()

    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(owner_id))
        
        # Branch 3 (exceeds max_branches=2 limit of organizations table)
        b3_id = uuid.uuid4()
        b3 = OrgBranch(id=b3_id, org_id=org_id, branch_name="Branch 3", branch_code="B3", internal_slug="b3")
        b3.state = OrgBranchState(branch_id=b3_id, org_id=org_id, branch_status="active", search_epoch_ulid="01AN4V07BY79KA1307SR1XF31C")
        
        session.add(b3)
        with pytest.raises(DBAPIError) as exc_info:
            await session.commit()
        assert "maximum branch limit" in str(exc_info.value)


@pytest.mark.asyncio
async def test_prevent_critical_branch_deletion_trigger(test_setup):
    org_id = test_setup["org_id"]
    owner_id = test_setup["owner_id"]

    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(owner_id))
        repo = BranchRepository(session)
        
        # Create single primary branch
        b1_id = uuid.uuid4()
        b1 = OrgBranch(id=b1_id, org_id=org_id, branch_name="Only Branch", branch_code="OB-01", internal_slug="ob-01")
        b1.state = OrgBranchState(branch_id=b1_id, org_id=org_id, branch_status="active", is_primary=True, search_epoch_ulid="01AN4V07BY79KA1307SR1XF31A")
        await repo.create(b1)
        await session.commit()

    # Attempt to soft-delete the last / primary branch
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(owner_id))
        repo = BranchRepository(session)
        
        with pytest.raises(DBAPIError) as exc_info:
            await repo.soft_delete(b1_id, org_id, owner_id, reason="Soft deleting sole branch")
        assert ("Cannot delete the primary branch" in str(exc_info.value)) or ("last branch" in str(exc_info.value))


@pytest.mark.asyncio
async def test_rbac_privileges_on_branch_actions(test_setup):
    org_id = test_setup["org_id"]
    owner_id = test_setup["owner_id"]
    admin_id = test_setup["admin_id"]
    trainer_id = test_setup["trainer_id"]

    # 1. Create two branches so last-branch check passes
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(owner_id))
        repo = BranchRepository(session)
        
        b1_id = uuid.uuid4()
        b1 = OrgBranch(id=b1_id, org_id=org_id, branch_name="Branch One", branch_code="B01", internal_slug="b01")
        b1.state = OrgBranchState(branch_id=b1_id, org_id=org_id, branch_status="active", is_primary=True, search_epoch_ulid="01AN4V07BY79KA1307SR1XF31A")
        
        b2_id = uuid.uuid4()
        b2 = OrgBranch(id=b2_id, org_id=org_id, branch_name="Branch Two", branch_code="B02", internal_slug="b02")
        b2.state = OrgBranchState(branch_id=b2_id, org_id=org_id, branch_status="active", is_primary=False, search_epoch_ulid="01AN4V07BY79KA1307SR1XF31B")
        
        await repo.create(b1)
        await repo.create(b2)
        await session.commit()

    # 2. Test soft deletion privileges: Trainer (no access) -> fail
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(trainer_id))
        repo = BranchRepository(session)
        with pytest.raises(DBAPIError) as exc_info:
            await repo.soft_delete(b2_id, org_id, trainer_id, reason="Trainer delete attempt")
        assert "Insufficient privileges" in str(exc_info.value)

    # 3. Test soft deletion privileges: Admin (only owner can soft delete) -> fail
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(admin_id))
        repo = BranchRepository(session)
        with pytest.raises(DBAPIError) as exc_info:
            await repo.soft_delete(b2_id, org_id, admin_id, reason="Admin delete attempt")
        assert "only owners can soft-delete" in str(exc_info.value)

    # 4. Test soft deletion privileges: Owner -> Success
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, str(org_id), str(owner_id))
        repo = BranchRepository(session)
        success = await repo.soft_delete(b2_id, org_id, owner_id, reason="Decommissioning branch")
        assert success is True
        await session.commit()


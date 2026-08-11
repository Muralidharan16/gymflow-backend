import pytest
import uuid
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock
from sqlalchemy import select, text

from app.models.organization import Organization
from app.models.staff import GymOwner
from app.models.enums import StaffRole
from app.models.org_branch import OrgBranch, OrgBranchState
from app.models.branch_lifecycle import BranchLifecycleEvent
from app.services.branch_lifecycle_service import BranchLifecycleService
from app.core.database import AsyncSessionLocal, update_session_context
from conftest import AdminTestSessionLocal, assert_test_database


async def set_db_session_context(session, org_id: str, user_id: str, role: str):
    """Install production-equivalent context now and across later transactions.

    Lifecycle services legitimately commit within a request/session. The test
    fixture can also begin a transaction before switching into a tenant actor
    context. ``update_session_context`` covers both cases: it stores verified
    context on ``Session.info`` so the ``after_begin`` hook re-applies it after
    commits, and it immediately applies the same transaction-local GUCs when a
    transaction is already active. No test-only RLS bypass is involved.
    """

    await update_session_context(
        session,
        principal_id=user_id,
        principal_type="legacy_gym_owner",
        org_id=org_id,
        trace_id="branch-lifecycle-test",
        role=role,
    )


async def cleanup_lifecycle_fixture(
    org_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """Remove only lifecycle-fixture state through the guarded admin identity.

    branch_status_history is intentionally append-only for DELETE/UPDATE, so the
    test harness truncates only the four lifecycle-owned child surfaces and does
    so without CASCADE. Tenant/root rows are then deleted explicitly in FK order.
    FORCE-RLS tables are cleaned with explicit tenant context instead of relying
    on owner identity as an implicit RLS bypass. This avoids granting TRUNCATE on
    unrelated tables and keeps destructive capability outside runtime.
    """
    async with AdminTestSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        await assert_test_database(session)
        await set_db_session_context(
            session, str(org_id), str(actor_id), "superadmin"
        )

        await session.execute(
            text(
                """
                TRUNCATE TABLE
                    public.branch_watchdog_alerts,
                    public.branch_lifecycle_events,
                    public.branch_outbox_events,
                    public.branch_status_history
                """
            )
        )
        await session.execute(
            text("DELETE FROM public.org_branch_state WHERE org_id = :org_id"),
            {"org_id": org_id},
        )
        await session.execute(
            text("DELETE FROM public.org_branches WHERE org_id = :org_id"),
            {"org_id": org_id},
        )
        await session.execute(
            text("DELETE FROM public.organization_users WHERE org_id = :org_id"),
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
async def lifecycle_setup(auth_db_session):
    """Seed lifecycle prerequisites through the same bounded identities as production.

    Tenant-root and actor records are administrative fixture evidence. First
    branch/state creation is an auth/bootstrap capability under FORCE RLS, so it
    must use the dedicated bounded auth identity with explicit tenant and typed
    principal context. Lifecycle behavior itself continues to run through the
    reduced application runtime identity below.
    """
    org_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    trainer_id = uuid.uuid4()
    b1_id = uuid.uuid4()
    b2_id = uuid.uuid4()

    async with AdminTestSessionLocal() as session:
        org = Organization(id=org_id, name="Lifecycle Test Org", max_branches=5)
        session.add(org)
        await session.flush()

        await set_db_session_context(session, str(org_id), str(owner_id), "owner")

        owner = GymOwner(
            id=owner_id,
            org_id=org_id,
            name="Lifecycle Owner",
            email=f"owner_{uuid.uuid4().hex[:6]}@test.com",
            password_hash="hash",
            role=StaffRole.owner,
            is_active=True,
            is_verified=True,
        )
        admin = GymOwner(
            id=admin_id,
            org_id=org_id,
            name="Lifecycle Admin",
            email=f"admin_{uuid.uuid4().hex[:6]}@test.com",
            password_hash="hash",
            role=StaffRole.admin,
            is_active=True,
            is_verified=True,
        )
        trainer = GymOwner(
            id=trainer_id,
            org_id=org_id,
            name="Lifecycle Trainer",
            email=f"trainer_{uuid.uuid4().hex[:6]}@test.com",
            password_hash="hash",
            role=StaffRole.trainer,
            is_active=True,
            is_verified=True,
        )
        session.add_all([owner, admin, trainer])
        await session.flush()

        from app.models.organization_user import OrganizationUser

        org_owner = OrganizationUser(
            id=owner_id,
            org_id=org_id,
            name="Lifecycle Owner User",
            email=owner.email,
            password_hash="hash",
            is_active=True,
            is_verified=True,
        )
        org_admin = OrganizationUser(
            id=admin_id,
            org_id=org_id,
            name="Lifecycle Admin User",
            email=admin.email,
            password_hash="hash",
            is_active=True,
            is_verified=True,
        )
        org_trainer = OrganizationUser(
            id=trainer_id,
            org_id=org_id,
            name="Lifecycle Trainer User",
            email=trainer.email,
            password_hash="hash",
            is_active=True,
            is_verified=True,
        )
        session.add_all([org_owner, org_admin, org_trainer])
        await session.commit()

    await set_db_session_context(
        auth_db_session, str(org_id), str(owner_id), "owner"
    )

    b1 = OrgBranch(
        id=b1_id,
        org_id=org_id,
        branch_name="Branch HQ",
        branch_code=f"HQ-{uuid.uuid4().hex[:4]}",
        internal_slug=f"hq-{uuid.uuid4().hex[:4]}",
        timezone="America/Los_Angeles",
        currency_code="USD",
        created_by=owner_id,
    )
    s1 = OrgBranchState(
        branch_id=b1_id,
        org_id=org_id,
        status="active",
        is_operational=True,
        search_epoch_ulid="01AN4V07BY79KA1307SR1XF31A",
    )
    b1.state = s1

    b2 = OrgBranch(
        id=b2_id,
        org_id=org_id,
        branch_name="Branch East",
        branch_code=f"EA-{uuid.uuid4().hex[:4]}",
        internal_slug=f"ea-{uuid.uuid4().hex[:4]}",
        timezone="America/New_York",
        currency_code="USD",
        created_by=owner_id,
    )
    s2 = OrgBranchState(
        branch_id=b2_id,
        org_id=org_id,
        status="active",
        is_operational=True,
        search_epoch_ulid="01AN4V07BY79KA1307SR1XF31B",
    )
    b2.state = s2

    auth_db_session.add_all([b1, b2])
    await auth_db_session.commit()

    yield {
        "org_id": org_id,
        "owner_id": owner_id,
        "admin_id": admin_id,
        "trainer_id": trainer_id,
        "branch1_id": b1_id,
        "branch2_id": b2_id,
    }

    await cleanup_lifecycle_fixture(org_id, owner_id)


@pytest.mark.asyncio
async def test_initiate_transition_unauthorized_role(lifecycle_setup):
    org_id = lifecycle_setup["org_id"]
    trainer_id = lifecycle_setup["trainer_id"]
    branch1_id = lifecycle_setup["branch1_id"]

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(trainer_id), "trainer")
        service = BranchLifecycleService(session)

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await service.initiate_transition(
                branch_id=branch1_id,
                org_id=org_id,
                to_status="temporarily_closed",
                actor_id=trainer_id,
                actor_role="trainer",
            )
        assert exc_info.value.status_code == 403
        assert "not authorized" in exc_info.value.detail


@pytest.mark.asyncio
async def test_initiate_transition_missing_reason_on_terminal(lifecycle_setup):
    org_id = lifecycle_setup["org_id"]
    owner_id = lifecycle_setup["owner_id"]
    branch1_id = lifecycle_setup["branch1_id"]

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        service = BranchLifecycleService(session)

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await service.initiate_transition(
                branch_id=branch1_id,
                org_id=org_id,
                to_status="permanently_closed",
                actor_id=owner_id,
                actor_role="owner",
            )
        assert exc_info.value.status_code == 400
        assert "status reason is required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_initiate_transition_last_active_branch_guard(lifecycle_setup):
    org_id = lifecycle_setup["org_id"]
    owner_id = lifecycle_setup["owner_id"]
    branch1_id = lifecycle_setup["branch1_id"]
    branch2_id = lifecycle_setup["branch2_id"]

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        service = BranchLifecycleService(session)

        await service.initiate_transition(
            branch_id=branch1_id,
            org_id=org_id,
            to_status="temporarily_closed",
            actor_id=owner_id,
            actor_role="owner",
            reason="Maintenance",
        )

        stmt = select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        res = await session.execute(stmt)
        state1 = res.scalar_one()
        assert state1.is_operational is False

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await service.initiate_transition(
                branch_id=branch2_id,
                org_id=org_id,
                to_status="permanently_closed",
                actor_id=owner_id,
                actor_role="owner",
                reason="Closing",
            )
        assert exc_info.value.status_code == 409
        assert "last operational branch" in exc_info.value.detail


@pytest.mark.asyncio
async def test_saga_happy_path_and_failure(lifecycle_setup, monkeypatch):
    org_id = lifecycle_setup["org_id"]
    owner_id = lifecycle_setup["owner_id"]
    branch1_id = lifecycle_setup["branch1_id"]

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        service = BranchLifecycleService(session)

        correlation_id = await service.initiate_transition(
            branch_id=branch1_id,
            org_id=org_id,
            to_status="temporarily_closed",
            actor_id=owner_id,
            actor_role="owner",
            reason="Temp",
        )

        await service.execute_saga_cascade(
            branch_id=branch1_id,
            org_id=org_id,
            from_status="active",
            to_status="temporarily_closed",
            correlation_id=correlation_id,
            actor_id=owner_id,
        )

        res = await session.execute(
            select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        )
        state = res.scalar_one()
        assert state.status == "temporarily_closed"
        assert state.is_operational is False
        assert state.lifecycle_transition_in_progress is False
        assert state.saga_last_checkpoint is None
        assert state.saga_compensation_strategy is None

        # Reactivate the fixture, then inject a real Transaction-B dependency
        # failure. Production has no simulate_failure hook: the normal exception
        # path must roll back the failed transaction and execute compensation.
        state.status = "active"
        state.is_operational = True
        state.lifecycle_transition_in_progress = False
        state.saga_last_checkpoint = None
        state.saga_compensation_strategy = None
        await session.commit()

        failed_correlation_id = await service.initiate_transition(
            branch_id=branch1_id,
            org_id=org_id,
            to_status="temporarily_closed",
            actor_id=owner_id,
            actor_role="owner",
            reason="Fail Path",
        )

        monkeypatch.setattr(
            service,
            "_update_checkpoint",
            AsyncMock(side_effect=RuntimeError("forced refund checkpoint failure")),
        )

        await service.execute_saga_cascade(
            branch_id=branch1_id,
            org_id=org_id,
            from_status="active",
            to_status="temporarily_closed",
            correlation_id=failed_correlation_id,
            actor_id=owner_id,
        )

        res2 = await session.execute(
            select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        )
        compensated = res2.scalar_one()
        assert compensated.status == "active"
        assert compensated.is_operational is True
        assert compensated.lifecycle_transition_in_progress is False
        assert compensated.saga_last_checkpoint is None
        assert compensated.saga_compensation_strategy is None

        event_res = await session.execute(
            select(BranchLifecycleEvent).where(
                BranchLifecycleEvent.branch_id == branch1_id,
                BranchLifecycleEvent.correlation_id == failed_correlation_id,
                BranchLifecycleEvent.event_type == "compensation_completed",
            )
        )
        assert event_res.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_watchdog_and_reconcile(lifecycle_setup):
    org_id = lifecycle_setup["org_id"]
    owner_id = lifecycle_setup["owner_id"]
    branch1_id = lifecycle_setup["branch1_id"]

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        service = BranchLifecycleService(session)

        await service.initiate_transition(
            branch_id=branch1_id,
            org_id=org_id,
            to_status="temporarily_closed",
            actor_id=owner_id,
            actor_role="owner",
            reason="Watchdog",
        )

        state_res = await session.execute(
            select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        )
        state = state_res.scalar_one()
        assert state.lifecycle_transition_in_progress is True

        state.status_changed_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.commit()

        await service.run_watchdog_sweep()

        recovered_res = await session.execute(
            select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        )
        recovered = recovered_res.scalar_one()
        assert recovered.status == "active"
        assert recovered.is_operational is True
        assert recovered.lifecycle_transition_in_progress is False
        assert recovered.saga_last_checkpoint is None
        assert recovered.saga_compensation_strategy is None
        assert recovered.watchdog_recovery_count >= 1
        assert recovered.watchdog_recovered_at is not None

        recovered.search_last_synced_at = datetime.now(timezone.utc) - timedelta(days=2)
        recovered.reconciliation_claimed_by = None
        recovered.reconciliation_claimed_at = None
        await session.commit()

        reconciled = await service.run_reconciliation_sweep()
        assert reconciled >= 1

        reconciled_res = await session.execute(
            select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        )
        reconciled_state = reconciled_res.scalar_one()
        # Reconciliation is intentionally set-based SQL and production sessions
        # keep expire_on_commit=False. Explicitly refresh the already-loaded ORM
        # identity before asserting the persisted row rather than confusing a
        # caller-local identity-map cache with a failed database update.
        await session.refresh(reconciled_state)
        assert reconciled_state.reconciliation_claimed_by is None
        assert reconciled_state.reconciliation_claimed_at is None
        assert reconciled_state.search_sync_failed_at is None
        assert reconciled_state.search_last_synced_at is not None
        assert reconciled_state.search_visibility_version >= 2
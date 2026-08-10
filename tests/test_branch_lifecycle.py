import pytest
import uuid
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, text

from app.models.organization import Organization
from app.models.staff import GymOwner
from app.models.enums import StaffRole
from app.models.org_branch import OrgBranch, OrgBranchState
from app.models.branch_lifecycle import (
    BranchStatusHistory,
    BranchLifecycleEvent,
    BranchOutboxEvent,
    BranchWatchdogAlert,
)
from app.services.branch_lifecycle_service import BranchLifecycleService
from app.core.database import AsyncSessionLocal
from conftest import AdminTestSessionLocal, assert_test_database


async def set_db_session_context(session, org_id: str, user_id: str, role: str):
    await session.execute(
        text("SELECT pg_catalog.set_config('app.current_org_id', :oid, true)"),
        {"oid": org_id},
    )
    await session.execute(
        text("SELECT pg_catalog.set_config('app.current_user_id', :uid, true)"),
        {"uid": user_id},
    )
    await session.execute(
        text("SELECT pg_catalog.set_config('app.current_role', :role, true)"),
        {"role": role},
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
async def lifecycle_setup():
    """Seed lifecycle prerequisites administratively; exercise behavior as runtime."""
    org_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    trainer_id = uuid.uuid4()
    b1_id = uuid.uuid4()
    b2_id = uuid.uuid4()

    # Tenant-root creation is deliberately outside the ordinary runtime contract.
    # Use the guarded test-admin identity only for fixture prerequisites. Once the
    # tenant exists, install the same tenant/user GUCs required by forced-RLS
    # production writes before seeding tenant-scoped rows.
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
        await session.flush()

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

        session.add_all([b1, b2])
        await session.commit()

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
                reason="",
            )
        assert exc_info.value.status_code == 400
        assert "status reason is required for this transition" in exc_info.value.detail


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
        )
        stmt = select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        res = await session.execute(stmt)
        state1 = res.scalar_one()
        state1.lifecycle_transition_in_progress = False
        state1.is_operational = False
        await session.commit()

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        service = BranchLifecycleService(session)

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await service.initiate_transition(
                branch_id=branch2_id,
                org_id=org_id,
                to_status="temporarily_closed",
                actor_id=owner_id,
                actor_role="owner",
            )
        assert exc_info.value.status_code == 409
        assert "Cannot deactivate the last operational branch" in exc_info.value.detail


@pytest.mark.asyncio
async def test_successful_transition_flow_a_and_b(lifecycle_setup):
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
        )
        assert correlation_id is not None

        stmt = select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        res = await session.execute(stmt)
        state = res.scalar_one()
        assert state.status == "temporarily_closed"
        assert state.lifecycle_transition_in_progress is True

        stmt_ev = select(BranchLifecycleEvent).where(
            BranchLifecycleEvent.correlation_id == correlation_id
        )
        res_ev = await session.execute(stmt_ev)
        events = res_ev.scalars().all()
        assert len(events) == 1
        assert events[0].event_type == "transition_initiated"
        assert events[0].step_sequence == 1

        stmt_hist = select(BranchStatusHistory).where(
            BranchStatusHistory.correlation_id == correlation_id
        )
        res_hist = await session.execute(stmt_hist)
        hists = res_hist.scalars().all()
        assert len(hists) == 1
        assert hists[0].to_status == "temporarily_closed"

        stmt_out = select(BranchOutboxEvent).where(
            BranchOutboxEvent.correlation_id == correlation_id
        )
        res_out = await session.execute(stmt_out)
        outs = res_out.scalars().all()
        assert len(outs) == 1
        assert outs[0].event_type == "branch.search_deindex"

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        service = BranchLifecycleService(session)

        await service.execute_saga_cascade(
            branch_id=branch1_id,
            org_id=org_id,
            from_status="active",
            to_status="temporarily_closed",
            correlation_id=correlation_id,
            actor_id=owner_id,
        )

        stmt = select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        res = await session.execute(stmt)
        state = res.scalar_one()
        assert state.lifecycle_transition_in_progress is False
        assert state.saga_last_checkpoint is None


@pytest.mark.asyncio
async def test_watchdog_sla_and_recovery(lifecycle_setup):
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
        )

        stmt = select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        res = await session.execute(stmt)
        state = res.scalar_one()
        state.status_changed_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        await session.commit()

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        service = BranchLifecycleService(session)
        await service.run_watchdog_sweep()

        stmt_alert = select(BranchWatchdogAlert).where(
            BranchWatchdogAlert.branch_id == branch1_id,
            BranchWatchdogAlert.alert_type == "freeze_threshold_15m",
        )
        res_alert = await session.execute(stmt_alert)
        alert = res_alert.scalar_one_or_none()
        assert alert is not None
        assert alert.resolved_at is None

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        stmt = select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        res = await session.execute(stmt)
        state = res.scalar_one()
        state.status_changed_at = datetime.now(timezone.utc) - timedelta(minutes=50)
        await session.commit()

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        service = BranchLifecycleService(session)
        await service.run_watchdog_sweep()

        stmt = select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        res = await session.execute(stmt)
        state = res.scalar_one()
        assert state.lifecycle_transition_in_progress is False
        assert state.status == "active"
        assert state.watchdog_recovery_count == 1


@pytest.mark.asyncio
async def test_reconciliation_sweep(lifecycle_setup):
    org_id = lifecycle_setup["org_id"]
    owner_id = lifecycle_setup["owner_id"]
    branch1_id = lifecycle_setup["branch1_id"]

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        stmt = select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        res = await session.execute(stmt)
        state = res.scalar_one()
        state.search_last_synced_at = datetime.now(timezone.utc) - timedelta(hours=30)
        await session.commit()

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        service = BranchLifecycleService(session)
        synced_count = await service.run_reconciliation_sweep()
        assert synced_count >= 1

        stmt = select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        res = await session.execute(stmt)
        state = res.scalar_one()
        assert state.search_last_synced_at > datetime.now(timezone.utc) - timedelta(minutes=1)
        assert state.reconciliation_claimed_by is None
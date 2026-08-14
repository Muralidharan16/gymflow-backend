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
    BranchLifecycleEvent,
    BranchOutboxEvent,
    BranchWatchdogAlert,
)
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


async def set_maintenance_session_context(session):
    """Install the exact lifecycle-maintenance context used by background tasks."""
    await update_session_context(
        session,
        trace_id="branch-lifecycle-maintenance-test",
        role="lifecycle_maintenance",
        internal_maintenance="lifecycle",
    )


async def cleanup_lifecycle_fixture() -> None:
    """Clear only lifecycle-owned append/queue surfaces in the disposable DB.

    Production deliberately exposes no hard-delete capability for branch roots or
    branch state. Test teardown must not invent one through temporary grants, RLS
    weakening, owner bypass tricks, or CASCADE. Every fixture uses fresh UUIDs,
    and the CI database itself is disposable, so tenant/branch roots remain until
    the job database is destroyed. Only lifecycle-owned append/queue relations,
    which cannot be tenant-deleted through production identities, are truncated
    by the guarded database owner between lifecycle scenarios.
    """
    async with AdminTestSessionLocal() as session:
        await session.execute(text("RESET ROLE"))
        await assert_test_database(session)
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
        await session.commit()


@pytest_asyncio.fixture
async def lifecycle_setup(auth_db_session):
    """Seed lifecycle prerequisites through the same bounded identities as production.

    Tenant-root and actor records are administrative fixture evidence. Branch/state
    bootstrap is executed through auth_runtime using the canonical first-branch
    shape required by the production onboarding boundary. Once a branch exists,
    ordinary state mutation is performed through app_runtime. This preserves the
    production split instead of widening either identity for test convenience.
    Lifecycle behavior itself continues to run through the reduced application
    runtime identity below.
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
        is_primary=True,
        is_operational=True,
        search_epoch_ulid="01AN4V07BY79KA1307SR1XF31A",
    )
    b1.state = s1
    auth_db_session.add(b1)
    await auth_db_session.commit()

    # Auth owns canonical bootstrap INSERT. Ordinary app runtime owns subsequent
    # lifecycle state mutation. Demote branch one before bootstrapping branch two
    # so the fixture never relies on two simultaneous primary branches.
    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        first_state = (
            await session.execute(
                select(OrgBranchState).where(OrgBranchState.branch_id == b1_id)
            )
        ).scalar_one()
        first_state.is_primary = False
        await session.commit()

    await set_db_session_context(
        auth_db_session, str(org_id), str(owner_id), "owner"
    )

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
        is_primary=True,
        is_operational=True,
        search_epoch_ulid="01AN4V07BY79KA1307SR1XF31B",
    )
    b2.state = s2
    auth_db_session.add(b2)
    await auth_db_session.commit()

    yield {
        "org_id": org_id,
        "owner_id": owner_id,
        "admin_id": admin_id,
        "trainer_id": trainer_id,
        "branch1_id": b1_id,
        "branch2_id": b2_id,
    }

    await cleanup_lifecycle_fixture()


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
async def test_saga_happy_path_and_transaction_b_failure_is_retry_safe(
    lifecycle_setup, monkeypatch
):
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

        # Return the fixture to its origin, then create a new Transaction A.
        # Transaction A is independently committed by initiate_transition and
        # must survive any later Transaction-B rollback so the durable parent can
        # retry safely.
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

        original_record_checkpoint = service._record_checkpoint
        injected = False

        async def fail_after_checkpoint_flush(*args, **kwargs):
            nonlocal injected
            await original_record_checkpoint(*args, **kwargs)
            if not injected:
                injected = True
                raise RuntimeError("forced transaction-b failure after checkpoint flush")

        monkeypatch.setattr(service, "_record_checkpoint", fail_after_checkpoint_flush)

        with pytest.raises(
            RuntimeError,
            match="forced transaction-b failure after checkpoint flush",
        ):
            await service.execute_saga_cascade(
                branch_id=branch1_id,
                org_id=org_id,
                from_status="active",
                to_status="temporarily_closed",
                correlation_id=failed_correlation_id,
                actor_id=owner_id,
            )

        # The production worker closes/rolls back this failed session before it
        # releases the durable parent for retry. Model that transaction boundary
        # explicitly here; no in-method synchronous compensation is expected.
        await session.rollback()

        retry_state = (
            await session.execute(
                select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
            )
        ).scalar_one()
        assert retry_state.status == "temporarily_closed"
        assert retry_state.is_operational is False
        assert retry_state.lifecycle_transition_in_progress is True
        assert retry_state.saga_last_checkpoint is None
        assert retry_state.saga_compensation_strategy == "rollback_to_origin"

        event_types = set(
            (
                await session.execute(
                    select(BranchLifecycleEvent.event_type).where(
                        BranchLifecycleEvent.branch_id == branch1_id,
                        BranchLifecycleEvent.correlation_id == failed_correlation_id,
                    )
                )
            ).scalars().all()
        )
        assert "status_change_initiated" in event_types
        assert "transaction_b_started" not in event_types
        assert "saga_database_completed" not in event_types
        assert "compensation_completed" not in event_types

        outbox_types = set(
            (
                await session.execute(
                    select(BranchOutboxEvent.event_type).where(
                        BranchOutboxEvent.branch_id == branch1_id,
                        BranchOutboxEvent.correlation_id == failed_correlation_id,
                    )
                )
            ).scalars().all()
        )
        assert outbox_types == {"branch.search_deindex", "branch.lifecycle_saga"}
        assert "branch.refund_required" not in outbox_types
        assert "branch.member_notification" not in outbox_types


@pytest.mark.asyncio
async def test_watchdog_alerts_without_compensating_retryable_saga(
    lifecycle_setup, maintenance_db_session
):
    org_id = lifecycle_setup["org_id"]
    owner_id = lifecycle_setup["owner_id"]
    branch1_id = lifecycle_setup["branch1_id"]

    # Transaction A remains an ordinary tenant/API capability.
    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        service = BranchLifecycleService(session)

        correlation_id = await service.initiate_transition(
            branch_id=branch1_id,
            org_id=org_id,
            to_status="temporarily_closed",
            actor_id=owner_id,
            actor_role="owner",
            reason="Watchdog",
        )

        state = (
            await session.execute(
                select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
            )
        ).scalar_one()
        assert state.lifecycle_transition_in_progress is True
        state.status_changed_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.commit()

    # Cross-tenant watchdog execution is maintenance-owned. The dedicated login
    # must also carry the transaction-local lifecycle context required by FORCE RLS.
    await set_maintenance_session_context(maintenance_db_session)
    maintenance_service = BranchLifecycleService(maintenance_db_session)

    # Repeated sweeps must alert idempotently and must never race the
    # retry/dead-letter worker by rolling back Transaction A themselves.
    await maintenance_service.run_watchdog_sweep()
    await maintenance_service.run_watchdog_sweep()

    frozen = (
        await maintenance_db_session.execute(
            select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        )
    ).scalar_one()
    assert frozen.status == "temporarily_closed"
    assert frozen.is_operational is False
    assert frozen.lifecycle_transition_in_progress is True
    assert frozen.saga_last_checkpoint is None
    assert frozen.saga_compensation_strategy == "rollback_to_origin"
    assert int(frozen.watchdog_recovery_count or 0) == 0
    assert frozen.watchdog_recovered_at is None

    alerts = (
        await maintenance_db_session.execute(
            select(BranchWatchdogAlert).where(
                BranchWatchdogAlert.branch_id == branch1_id,
                BranchWatchdogAlert.alert_type == "freeze_threshold_15m",
                BranchWatchdogAlert.resolved_at.is_(None),
            )
        )
    ).scalars().all()
    assert len(alerts) == 1

    # Outbox/lifecycle history are not maintenance inspection privileges. Verify
    # those invariants through the tenant-scoped application identity instead of
    # broadening the maintenance role just for test convenience.
    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        parent = (
            await session.execute(
                select(BranchOutboxEvent).where(
                    BranchOutboxEvent.branch_id == branch1_id,
                    BranchOutboxEvent.correlation_id == correlation_id,
                    BranchOutboxEvent.event_type == "branch.lifecycle_saga",
                )
            )
        ).scalar_one()
        assert parent.status == "pending"
        assert parent.attempt_count == 0
        assert parent.leased_by is None
        assert parent.leased_until is None

        compensation_events = (
            await session.execute(
                select(BranchLifecycleEvent.event_id).where(
                    BranchLifecycleEvent.branch_id == branch1_id,
                    BranchLifecycleEvent.correlation_id == correlation_id,
                    BranchLifecycleEvent.event_type == "compensation_completed",
                )
            )
        ).scalars().all()
        assert compensation_events == []


@pytest.mark.asyncio
async def test_reconciliation_sweep_releases_claim_and_advances_projection(
    lifecycle_setup, maintenance_db_session
):
    branch1_id = lifecycle_setup["branch1_id"]

    # Reconciliation markers are maintenance-owned state. Seed and execute the
    # scenario through the same dedicated identity used by the scheduled task.
    await set_maintenance_session_context(maintenance_db_session)
    service = BranchLifecycleService(maintenance_db_session)

    state = (
        await maintenance_db_session.execute(
            select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        )
    ).scalar_one()
    assert state.status == "active"
    assert state.lifecycle_transition_in_progress is False
    starting_version = state.search_visibility_version
    state.search_last_synced_at = datetime.now(timezone.utc) - timedelta(days=2)
    state.reconciliation_claimed_by = None
    state.reconciliation_claimed_at = None
    await maintenance_db_session.commit()

    reconciled = await service.run_reconciliation_sweep()
    assert reconciled >= 1

    reconciled_state = (
        await maintenance_db_session.execute(
            select(OrgBranchState).where(OrgBranchState.branch_id == branch1_id)
        )
    ).scalar_one()
    # Reconciliation is intentionally set-based SQL and production sessions
    # keep expire_on_commit=False. Explicitly refresh the already-loaded ORM
    # identity before asserting the persisted row rather than confusing a
    # caller-local identity-map cache with a failed database update.
    await maintenance_db_session.refresh(reconciled_state)
    assert reconciled_state.reconciliation_claimed_by is None
    assert reconciled_state.reconciliation_claimed_at is None
    assert reconciled_state.search_sync_failed_at is None
    assert reconciled_state.search_last_synced_at is not None
    assert reconciled_state.search_visibility_version > starting_version

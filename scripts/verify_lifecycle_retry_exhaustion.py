from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.database import (
    AsyncSessionLocal,
    update_session_context,
    worker_async_session_maker,
)
from app.models.branch_lifecycle import (
    BranchLifecycleEvent,
    BranchOutboxEvent,
    BranchStatusHistory,
)
from app.models.enums import StaffRole
from app.models.org_branch import OrgBranch, OrgBranchState
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.staff import GymOwner
from app.services.branch_lifecycle_service import BranchLifecycleService
from app.tasks.branch_outbox_poller import (
    _claim_events,
    _fail_event,
    _install_saga_context,
)


@dataclass(frozen=True)
class Scenario:
    org_id: uuid.UUID
    owner_id: uuid.UUID
    branch_id: uuid.UUID
    sibling_branch_id: uuid.UUID


def _required_url(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _validate_identity_urls() -> tuple[str, str, str, str]:
    migration_url = _required_url("MIGRATION_DATABASE_URL")
    app_url = _required_url("DATABASE_URL")
    auth_url = _required_url("AUTH_DATABASE_URL")
    worker_url = _required_url("WORKER_DATABASE_URL")

    parsed = [make_url(value) for value in (migration_url, app_url, auth_url, worker_url)]
    databases = {url.database for url in parsed}
    usernames = [url.username for url in parsed]
    if len(databases) != 1:
        raise RuntimeError(f"all compensation identities must target one database: {databases!r}")
    database_name = next(iter(databases)) or ""
    if "test" not in database_name.lower() and "boundary" not in database_name.lower():
        raise RuntimeError(f"refusing non-test compensation database: {database_name!r}")
    if None in usernames or len(set(usernames)) != 4:
        raise RuntimeError(f"compensation identities must be four distinct logins: {usernames!r}")
    return migration_url, app_url, auth_url, worker_url


async def _set_owner_context(session: AsyncSession, scenario: Scenario, trace: str) -> None:
    await update_session_context(
        session,
        principal_id=str(scenario.owner_id),
        principal_type="legacy_gym_owner",
        org_id=str(scenario.org_id),
        trace_id=trace,
        role="owner",
    )


async def _assert_login(session: AsyncSession, expected: str) -> None:
    observed = (await session.execute(text("SELECT current_user::text"))).scalar_one()
    if observed != expected:
        raise AssertionError(f"database identity drift: expected={expected!r}, observed={observed!r}")


async def _seed(
    admin_maker: async_sessionmaker[AsyncSession],
    auth_maker: async_sessionmaker[AsyncSession],
) -> Scenario:
    scenario = Scenario(
        org_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        sibling_branch_id=uuid.uuid4(),
    )

    async with admin_maker() as session:
        await _assert_login(session, "migration_owner")
        session.add(Organization(id=scenario.org_id, name="Retry Exhaustion Boundary", max_branches=5))
        await session.flush()
        await _set_owner_context(session, scenario, "retry-exhaustion-admin-seed")
        owner = GymOwner(
            id=scenario.owner_id,
            org_id=scenario.org_id,
            name="Retry Exhaustion Owner",
            email=f"retry_{uuid.uuid4().hex[:10]}@test.invalid",
            password_hash="hash",
            role=StaffRole.owner,
            is_active=True,
            is_verified=True,
        )
        session.add(owner)
        session.add(
            OrganizationUser(
                id=scenario.owner_id,
                org_id=scenario.org_id,
                name="Retry Exhaustion Owner",
                email=owner.email,
                password_hash="hash",
                is_active=True,
                is_verified=True,
            )
        )
        await session.commit()

    async with auth_maker() as session:
        await _assert_login(session, "auth_test_runtime")
        await _set_owner_context(session, scenario, "retry-exhaustion-auth-seed")
        branch = OrgBranch(
            id=scenario.branch_id,
            org_id=scenario.org_id,
            branch_name="Compensation Target",
            branch_code=f"RT-{uuid.uuid4().hex[:6]}",
            internal_slug=f"retry-target-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
            currency_code="USD",
            created_by=scenario.owner_id,
        )
        branch.state = OrgBranchState(
            branch_id=scenario.branch_id,
            org_id=scenario.org_id,
            status="active",
            is_operational=True,
            search_epoch_ulid="01AN4V07BY79KA1307SR1XF31C",
        )
        sibling = OrgBranch(
            id=scenario.sibling_branch_id,
            org_id=scenario.org_id,
            branch_name="Operational Sibling",
            branch_code=f"RS-{uuid.uuid4().hex[:6]}",
            internal_slug=f"retry-sibling-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
            currency_code="USD",
            created_by=scenario.owner_id,
        )
        sibling.state = OrgBranchState(
            branch_id=scenario.sibling_branch_id,
            org_id=scenario.org_id,
            status="active",
            is_operational=True,
            search_epoch_ulid="01AN4V07BY79KA1307SR1XF31D",
        )
        session.add_all([branch, sibling])
        await session.commit()

    return scenario


async def _initiate(scenario: Scenario) -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        await _assert_login(session, "app_test_runtime")
        await _set_owner_context(session, scenario, "retry-exhaustion-transaction-a")
        correlation_id = await BranchLifecycleService(session).initiate_transition(
            branch_id=scenario.branch_id,
            org_id=scenario.org_id,
            to_status="temporarily_closed",
            actor_id=scenario.owner_id,
            actor_role="owner",
            reason="Runtime retry exhaustion verification",
        )
        parent_id = (
            await session.execute(
                select(BranchOutboxEvent.outbox_id).where(
                    BranchOutboxEvent.branch_id == scenario.branch_id,
                    BranchOutboxEvent.correlation_id == correlation_id,
                    BranchOutboxEvent.event_type == "branch.lifecycle_saga",
                )
            )
        ).scalar_one()
        return correlation_id, parent_id


async def _assert_frozen_transaction_a(
    scenario: Scenario,
    *,
    parent_id: uuid.UUID,
    expected_lease: uuid.UUID,
) -> datetime:
    async with AsyncSessionLocal() as session:
        await _set_owner_context(session, scenario, "retry-exhaustion-frozen-proof")
        state = (
            await session.execute(
                select(OrgBranchState).where(OrgBranchState.branch_id == scenario.branch_id)
            )
        ).scalar_one()
        assert state.status == "temporarily_closed"
        assert state.is_operational is False
        assert state.lifecycle_transition_in_progress is True
        assert state.saga_last_checkpoint is None
        assert state.saga_compensation_strategy == "rollback_to_origin"
        assert state.status_changed_by == scenario.owner_id
        assert state.status_reason == "Runtime retry exhaustion verification"
        assert state.transition_source == "api"
        assert state.status_changed_at is not None
        transition_changed_at = state.status_changed_at

        parent = (
            await session.execute(
                select(BranchOutboxEvent).where(BranchOutboxEvent.outbox_id == parent_id)
            )
        ).scalar_one()
        assert parent.status == "processing"
        assert parent.leased_by == expected_lease
        assert parent.leased_until is not None
        return transition_changed_at


async def _assert_compensated_once(
    scenario: Scenario,
    *,
    correlation_id: uuid.UUID,
    parent_id: uuid.UUID,
    transition_changed_at: datetime,
) -> tuple[int, int, int]:
    async with AsyncSessionLocal() as session:
        await _set_owner_context(session, scenario, "retry-exhaustion-final-proof")
        state = (
            await session.execute(
                select(OrgBranchState).where(OrgBranchState.branch_id == scenario.branch_id)
            )
        ).scalar_one()
        assert state.status == "active"
        assert state.is_operational is True
        assert state.lifecycle_transition_in_progress is False
        assert state.saga_last_checkpoint is None
        assert state.saga_compensation_strategy is None
        assert state.status_changed_by == scenario.owner_id
        assert state.status_changed_at is not None
        assert state.status_changed_at > transition_changed_at
        assert state.status_reason == "Saga dead-letter compensation rollback"
        assert state.transition_source == "saga_compensation"

        parent = (
            await session.execute(
                select(BranchOutboxEvent).where(BranchOutboxEvent.outbox_id == parent_id)
            )
        ).scalar_one()
        assert parent.status == "dead_lettered"
        assert parent.attempt_count == parent.max_attempts
        assert parent.leased_by is None
        assert parent.leased_until is None
        assert "forced final transaction-b failure" in (parent.last_error or "")

        compensation_events = int(
            await session.scalar(
                select(func.count())
                .select_from(BranchLifecycleEvent)
                .where(
                    BranchLifecycleEvent.branch_id == scenario.branch_id,
                    BranchLifecycleEvent.correlation_id == correlation_id,
                    BranchLifecycleEvent.event_type == "compensation_completed",
                )
            )
            or 0
        )
        compensation_history = int(
            await session.scalar(
                select(func.count())
                .select_from(BranchStatusHistory)
                .where(
                    BranchStatusHistory.branch_id == scenario.branch_id,
                    BranchStatusHistory.correlation_id == correlation_id,
                    BranchStatusHistory.transition_source == "saga_compensation",
                    BranchStatusHistory.from_status == "temporarily_closed",
                    BranchStatusHistory.to_status == "active",
                )
            )
            or 0
        )
        compensation_search = int(
            await session.scalar(
                select(func.count())
                .select_from(BranchOutboxEvent)
                .where(
                    BranchOutboxEvent.branch_id == scenario.branch_id,
                    BranchOutboxEvent.correlation_id == correlation_id,
                    BranchOutboxEvent.event_type == "branch.search_index",
                    BranchOutboxEvent.payload["reason"].as_string()
                    == "saga_dead_letter_compensation",
                )
            )
            or 0
        )
        assert compensation_events == 1
        assert compensation_history == 1
        assert compensation_search == 1
        return compensation_events, compensation_history, compensation_search


async def main() -> None:
    migration_url, _, auth_url, _ = _validate_identity_urls()
    admin_engine = create_async_engine(migration_url, poolclass=NullPool, pool_pre_ping=True)
    auth_engine = create_async_engine(auth_url, poolclass=NullPool, pool_pre_ping=True)
    admin_maker = async_sessionmaker(admin_engine, class_=AsyncSession, expire_on_commit=False)
    auth_maker = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)

    try:
        scenario = await _seed(admin_maker, auth_maker)
        correlation_id, parent_id = await _initiate(scenario)

        worker_id = uuid.uuid4()
        claimed = await _claim_events(worker_id)
        saga = next(
            event
            for event in claimed
            if event["outbox_id"] == parent_id
            and event["event_type"] == "branch.lifecycle_saga"
        )

        async with worker_async_session_maker() as session:
            await _assert_login(session, "worker_test_runtime")
            await _install_saga_context(session, event=saga, worker_id=worker_id)
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE public.branch_outbox_events
                        SET attempt_count = max_attempts
                        WHERE outbox_id = :outbox_id
                          AND status = 'processing'
                          AND leased_by = :worker_id
                        RETURNING attempt_count, max_attempts
                        """
                    ),
                    {"outbox_id": parent_id, "worker_id": worker_id},
                )
            ).one()
            await session.commit()
        saga["attempt_count"] = int(row.attempt_count)
        saga["max_attempts"] = int(row.max_attempts)
        assert saga["attempt_count"] == saga["max_attempts"]

        wrong_worker = uuid.uuid4()
        wrong_outcome = await _fail_event(
            dict(saga),
            wrong_worker,
            RuntimeError("forced final transaction-b failure"),
            permanent=False,
        )
        assert wrong_outcome == "lease_lost"
        transition_changed_at = await _assert_frozen_transaction_a(
            scenario,
            parent_id=parent_id,
            expected_lease=worker_id,
        )

        outcome = await _fail_event(
            dict(saga),
            worker_id,
            RuntimeError("forced final transaction-b failure"),
            permanent=False,
        )
        assert outcome == "dead_lettered_compensated"
        before_replay = await _assert_compensated_once(
            scenario,
            correlation_id=correlation_id,
            parent_id=parent_id,
            transition_changed_at=transition_changed_at,
        )

        replay_outcome = await _fail_event(
            dict(saga),
            worker_id,
            RuntimeError("forced final transaction-b failure"),
            permanent=False,
        )
        assert replay_outcome == "lease_lost"
        after_replay = await _assert_compensated_once(
            scenario,
            correlation_id=correlation_id,
            parent_id=parent_id,
            transition_changed_at=transition_changed_at,
        )
        assert after_replay == before_replay == (1, 1, 1)

        print("PASS: lifecycle retry exhaustion compensation is lease-bound, atomic, metadata-consistent, and replay-safe")
    finally:
        await admin_engine.dispose()
        await auth_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

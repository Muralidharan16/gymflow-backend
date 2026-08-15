from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.deps import BranchAccessGuard, Staff
from app.models.branch_lifecycle import BranchStatusTransition
from app.routers.branch_lifecycle import list_branches
from app.services.branch_lifecycle_service import BranchLifecycleService
from test_branch_lifecycle import lifecycle_setup, set_db_session_context


EXPECTED_TRANSITIONS = {
    ("active", "temporarily_closed"): {"owner", "org_admin", "admin"},
    ("active", "under_renovation"): {"owner", "org_admin", "admin"},
    ("active", "compliance_suspended"): {"compliance", "superadmin"},
    ("active", "permanently_closed"): {"owner", "superadmin"},
    ("temporarily_closed", "active"): {"owner", "org_admin", "admin"},
    ("temporarily_closed", "permanently_closed"): {"owner", "superadmin"},
    ("under_renovation", "active"): {"owner", "org_admin", "admin"},
    ("compliance_suspended", "active"): {"compliance", "superadmin"},
    ("compliance_suspended", "permanently_closed"): {"compliance", "superadmin"},
}


@pytest.mark.asyncio
async def test_p3d_live_transition_catalog_matches_certified_matrix(lifecycle_setup) -> None:
    org_id = lifecycle_setup["org_id"]
    owner_id = lifecycle_setup["owner_id"]

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(owner_id), "owner")
        rows = (
            await session.execute(
                select(
                    BranchStatusTransition.from_status,
                    BranchStatusTransition.to_status,
                    BranchStatusTransition.allowed_roles,
                )
            )
        ).all()

    actual = {
        (row.from_status, row.to_status): set(row.allowed_roles)
        for row in rows
    }
    assert set(actual) == set(EXPECTED_TRANSITIONS)
    for edge, required_roles in EXPECTED_TRANSITIONS.items():
        assert required_roles.issubset(actual[edge]), edge


@pytest.mark.asyncio
async def test_p3d_canonical_admin_can_execute_admin_transition(lifecycle_setup) -> None:
    org_id = lifecycle_setup["org_id"]
    admin_id = lifecycle_setup["admin_id"]
    branch_id = lifecycle_setup["branch1_id"]

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(admin_id), "admin")
        correlation_id = await BranchLifecycleService(session).initiate_transition(
            branch_id=branch_id,
            org_id=org_id,
            to_status="temporarily_closed",
            actor_id=admin_id,
            actor_role="admin",
            reason="P3D canonical admin matrix proof",
        )
        assert isinstance(correlation_id, uuid.UUID)


@pytest.mark.asyncio
async def test_p3d_cross_tenant_transition_fails_closed(lifecycle_setup) -> None:
    owner_id = lifecycle_setup["owner_id"]
    branch_id = lifecycle_setup["branch1_id"]
    foreign_org_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(foreign_org_id), str(owner_id), "owner")
        with pytest.raises(HTTPException) as exc_info:
            await BranchLifecycleService(session).initiate_transition(
                branch_id=branch_id,
                org_id=foreign_org_id,
                to_status="temporarily_closed",
                actor_id=owner_id,
                actor_role="owner",
                reason="Foreign tenant must not mutate",
            )
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_p3d_branch_scope_does_not_depend_on_gym_id_claim(lifecycle_setup) -> None:
    org_id = lifecycle_setup["org_id"]
    trainer_id = lifecycle_setup["trainer_id"]
    branch_id = lifecycle_setup["branch1_id"]

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(trainer_id), "trainer")
        guard = BranchAccessGuard()

        missing_scope = Staff(
            id=trainer_id,
            org_id=org_id,
            gym_id=None,
            role="trainer",
            branch_ids=[],
        )
        with pytest.raises(HTTPException) as exc_info:
            await guard(branch_id=branch_id, staff=missing_scope, db=session)
        assert exc_info.value.status_code == 403
        assert "authorized scope" in exc_info.value.detail

        explicit_scope = Staff(
            id=trainer_id,
            org_id=org_id,
            gym_id=None,
            role="trainer",
            branch_ids=[str(branch_id)],
        )
        assert await guard(branch_id=branch_id, staff=explicit_scope, db=session) == explicit_scope


@pytest.mark.asyncio
async def test_p3d_branch_list_respects_signed_branch_scope(lifecycle_setup) -> None:
    org_id = lifecycle_setup["org_id"]
    trainer_id = lifecycle_setup["trainer_id"]
    branch1_id = lifecycle_setup["branch1_id"]
    branch2_id = lifecycle_setup["branch2_id"]

    async with AsyncSessionLocal() as session:
        await set_db_session_context(session, str(org_id), str(trainer_id), "trainer")
        scoped_staff = Staff(
            id=trainer_id,
            org_id=org_id,
            gym_id=None,
            role="trainer",
            branch_ids=[str(branch1_id)],
        )
        response = await list_branches(current_staff=scoped_staff, db=session)

    visible_ids = {uuid.UUID(row["id"]) for row in response["data"]}
    assert visible_ids == {branch1_id}
    assert branch2_id not in visible_ids


@pytest.mark.asyncio
async def test_p3d_concurrent_transition_has_one_winner_one_conflict(lifecycle_setup) -> None:
    org_id = lifecycle_setup["org_id"]
    owner_id = lifecycle_setup["owner_id"]
    admin_id = lifecycle_setup["admin_id"]
    branch_id = lifecycle_setup["branch1_id"]
    start = asyncio.Event()

    async def attempt(actor_id: uuid.UUID, actor_role: str):
        async with AsyncSessionLocal() as session:
            await set_db_session_context(session, str(org_id), str(actor_id), actor_role)
            service = BranchLifecycleService(session)
            await start.wait()
            try:
                value = await service.initiate_transition(
                    branch_id=branch_id,
                    org_id=org_id,
                    to_status="temporarily_closed",
                    actor_id=actor_id,
                    actor_role=actor_role,
                    reason=f"P3D race {actor_role}",
                )
                return ("winner", value)
            except HTTPException as exc:
                return ("http", exc.status_code)

    tasks = [
        asyncio.create_task(attempt(owner_id, "owner")),
        asyncio.create_task(attempt(admin_id, "admin")),
    ]
    start.set()
    results = await asyncio.gather(*tasks)

    winners = [value for kind, value in results if kind == "winner"]
    conflicts = [value for kind, value in results if kind == "http"]
    assert len(winners) == 1
    assert len(conflicts) == 1
    assert conflicts[0] == 409

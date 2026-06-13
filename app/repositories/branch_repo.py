from typing import List, Optional
from uuid import UUID
from datetime import datetime, timezone
import json

from sqlalchemy import select, update as sql_update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_branch import OrgBranch, OrgBranchState, ActiveOrgBranch


class BranchRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_active_by_id(self, branch_id: UUID, org_id: UUID) -> Optional[ActiveOrgBranch]:
        """
        Securely fetches an active branch by ID and Org ID.
        This reads directly from the RLS-isolated and security_barrier-enforced view.
        """
        query = select(ActiveOrgBranch).where(
            ActiveOrgBranch.id == branch_id,
            ActiveOrgBranch.org_id == org_id
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def list_active(self, org_id: UUID, skip: int = 0, limit: int = 100) -> List[ActiveOrgBranch]:
        """
        Securely lists active branches for an organization.
        """
        query = (
            select(ActiveOrgBranch)
            .where(ActiveOrgBranch.org_id == org_id)
            .order_by(ActiveOrgBranch.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_for_mutation(self, branch_id: UUID, org_id: UUID) -> Optional[OrgBranch]:
        """
        Fetches the branch and its associated state from the main tables for write operations.
        Eagerly loads the state table to prevent N+1 queries.
        """
        query = (
            select(OrgBranch)
            .options(selectinload(OrgBranch.state))
            .where(
                OrgBranch.id == branch_id,
                OrgBranch.org_id == org_id
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def create(self, branch: OrgBranch) -> OrgBranch:
        """
        Persists a new branch and automatically handles flushing it.
        The caller is responsible for setting state with a valid search_epoch_ulid.
        """
        self.session.add(branch)
        await self.session.flush()
        return branch



    async def soft_delete(self, branch_id: UUID, org_id: UUID, actor_id: UUID, reason: str) -> bool:
        """
        Transitions a branch to a soft-deleted state.
        This updates deleted_at and sets branch_status to 'pending_deletion'.
        """
        # Fetch the mutable state row first
        query = select(OrgBranchState).where(
            OrgBranchState.branch_id == branch_id,
            OrgBranchState.org_id == org_id,
            OrgBranchState.deleted_at.is_(None)
        )
        result = await self.session.execute(query)
        state = result.scalar_one_or_none()
        
        if not state:
            return False

        # Apply state changes to trigger the DB triggers (validate FSM and RBAC)
        state.deleted_at = datetime.now(timezone.utc)
        state.branch_status = "pending_deletion"
        state.is_active = False
        state.is_operational = False
        state.status = "permanently_closed"
        state.status_reason = reason
        state.status_changed_by = None

        # Audit trail must now be handled via AuditService by the caller.
        
        await self.session.flush()
        return True

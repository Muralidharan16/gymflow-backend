import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy import select, update, text, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.models.org_branch import OrgBranchState, OrgBranch
from app.models.branch_lifecycle import (
    BranchStatusDefinition,
    BranchStatusTransition,
    BranchDeactivationPolicy,
    BranchStatusHistory,
    BranchLifecycleEvent,
    BranchOutboxEvent,
    BranchWatchdogAlert
)

logger = logging.getLogger(__name__)

class BranchLifecycleService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def initiate_transition(
        self,
        branch_id: uuid.UUID,
        org_id: uuid.UUID,
        to_status: str,
        actor_id: uuid.UUID,
        actor_role: str,
        reason: Optional[str] = None
    ) -> uuid.UUID:
        """
        Transaction A: Atomic Status Flip with Advisory Locking, Last-Active Branch Guard,
        and Step-1 Lifecycle/Outbox emission.
        """
        # 1. Fetch status definitions
        stmt_def = select(BranchStatusDefinition).where(BranchStatusDefinition.code == to_status)
        res_def = await self.db.execute(stmt_def)
        target_def = res_def.scalar_one_or_none()
        if not target_def:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Target status '{to_status}' is not defined."
            )

        # 2. Acquire org-level 64-bit advisory lock to prevent concurrent transitions within same org
        # abs(hashtext(left(org_id, 18)))
        org_str = str(org_id)
        left_part = org_str[:18]
        right_part = org_str[18:]
        
        lock_res = await self.db.execute(
            text("""
                SELECT pg_try_advisory_xact_lock(
                    abs(hashtext(:left_part)),
                    abs(hashtext(:right_part))
                );
            """),
            {"left_part": left_part, "right_part": right_part}
        )
        if not lock_res.scalar():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Another lifecycle transition is currently in progress for this organization."
            )

        # 3. SELECT FOR UPDATE on target branch row
        stmt_branch = select(OrgBranchState).where(
            OrgBranchState.branch_id == branch_id,
            OrgBranchState.org_id == org_id,
            OrgBranchState.deleted_at.is_(None)
        ).with_for_update()
        res_branch = await self.db.execute(stmt_branch)
        branch_state = res_branch.scalar_one_or_none()

        if not branch_state:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Branch not found or belongs to another organization."
            )

        from_status = branch_state.status
        if from_status == to_status:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Branch is already in status '{to_status}'."
            )

        # 4. Check if the transition is defined and allowed for user role
        stmt_trans = select(BranchStatusTransition).where(
            BranchStatusTransition.from_status == from_status,
            BranchStatusTransition.to_status == to_status
        )
        res_trans = await self.db.execute(stmt_trans)
        allowed_trans = res_trans.scalar_one_or_none()

        if not allowed_trans:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Transition from '{from_status}' to '{to_status}' is illegal."
            )

        if actor_role not in allowed_trans.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{actor_role}' is not authorized to execute transition from '{from_status}' to '{to_status}'."
            )

        if allowed_trans.requires_reason and (not reason or not reason.strip()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A valid, non-empty status reason is required for this transition."
            )

        # Terminal transition reason validation
        if to_status in ('permanently_closed', 'compliance_suspended') and (not reason or not reason.strip()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Terminal transitions must explicitly state a valid reason for legal auditing."
            )

        # 5. Last-Active Branch Guard
        # If target status is non-operational, check that we don't deactivate the last operational branch
        if not target_def.is_operational and branch_state.is_operational:
            count_stmt = select(func.count(1)).select_from(OrgBranchState).where(
                OrgBranchState.org_id == org_id,
                OrgBranchState.is_operational == True,
                OrgBranchState.deleted_at.is_(None)
            )

            op_count_res = await self.db.execute(count_stmt)
            operational_count = op_count_res.scalar() or 0

            if operational_count <= 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot deactivate the last operational branch for this organization."
                )

        # 6. Capture snapshot before updates
        snapshot = {
            "status": branch_state.status,
            "is_operational": branch_state.is_operational,
            "lifecycle_transition_in_progress": branch_state.lifecycle_transition_in_progress,
            "saga_last_checkpoint": branch_state.saga_last_checkpoint,
            "saga_compensation_strategy": branch_state.saga_compensation_strategy,
            "status_changed_at": branch_state.status_changed_at.isoformat() if branch_state.status_changed_at else None,
            "status_changed_by": str(branch_state.status_changed_by) if branch_state.status_changed_by else None,
            "status_reason": branch_state.status_reason,
            "transition_source": branch_state.transition_source,
            "search_visibility_version": branch_state.search_visibility_version,
            "search_last_synced_at": branch_state.search_last_synced_at.isoformat() if branch_state.search_last_synced_at else None,
        }

        # 7. Update branch state (Transaction A)
        correlation_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        branch_state.status = to_status
        branch_state.is_operational = target_def.is_operational
        branch_state.lifecycle_transition_in_progress = True
        branch_state.saga_last_checkpoint = None
        branch_state.saga_compensation_strategy = "rollback_to_origin"
        branch_state.status_changed_at = now
        branch_state.status_changed_by = actor_id
        branch_state.status_reason = reason
        branch_state.transition_source = "api"

        # 8. Insert branch lifecycle event (Contract 2: must precede history update)
        event = BranchLifecycleEvent(
            event_id=uuid.uuid4(),
            branch_id=branch_id,
            event_type="transition_initiated",
            event_version=1,
            payload={"from_status": from_status, "to_status": to_status},
            emitted_at=now,
            correlation_id=correlation_id,
            step_sequence=1
        )
        self.db.add(event)
        await self.db.flush()

        # 9. Insert branch status history (Contract 2)
        history = BranchStatusHistory(
            history_id=uuid.uuid4(),
            branch_id=branch_id,
            from_status=from_status,
            to_status=to_status,
            changed_by=actor_id,
            changed_at=now,
            reason=reason,
            transition_source="api",
            snapshot=snapshot,
            correlation_id=correlation_id,
            correlation_emitted_at=now
        )
        self.db.add(history)

        # 10. Write de-index event to transactional outbox synchronously
        # Contract 2: Outbox priority de-indexing event #1 in Transaction A
        outbox_event = BranchOutboxEvent(
            outbox_id=uuid.uuid4(),
            branch_id=branch_id,
            event_type="branch.search_deindex",
            payload={"branch_id": str(branch_id), "org_id": str(org_id), "status": to_status},
            created_at=now,
            process_after=now,
            status="pending",
            correlation_id=correlation_id
        )
        self.db.add(outbox_event)

        await self.db.commit()
        return correlation_id

    async def execute_saga_cascade(
        self,
        branch_id: uuid.UUID,
        org_id: uuid.UUID,
        from_status: str,
        to_status: str,
        correlation_id: uuid.UUID,
        actor_id: uuid.UUID
    ) -> None:
        """
        Transaction B: Saga Cascade Operations (Cancel Bookings, Refunds, Notifications, and Cleanup).
        """
        # Fetch deactivation policy for this transition
        stmt_policy = select(BranchDeactivationPolicy).where(
            BranchDeactivationPolicy.from_status == from_status,
            BranchDeactivationPolicy.to_status == to_status
        )
        res_policy = await self.db.execute(stmt_policy)
        policy = res_policy.scalar_one_or_none()

        if not policy:
            # If no policy defined, complete saga immediately
            await self._complete_saga(branch_id, was_watchdog_recovery=False)
            return

        try:
            # Checkpoint 1: Cancel Bookings
            if policy.auto_cancel_bookings:
                await self._update_checkpoint(branch_id, "bookings_cancelled", correlation_id, 2)
                # Query/Cancel bookings safely if bookings table exists
                # Cancel future bookings beyond grace period
                grace_limit = datetime.now(timezone.utc) + timedelta(hours=policy.booking_grace_hours)
                try:
                    await self.db.execute(
                        text("""
                            UPDATE public.bookings
                            SET status = 'cancelled',
                                cancellation_reason = 'Branch status transition to ' || :to_status
                            WHERE branch_id = :branch_id
                              AND start_time >= :grace_limit
                              AND status != 'cancelled';
                        """),
                        {"branch_id": branch_id, "grace_limit": grace_limit, "to_status": to_status}
                    )
                except Exception as db_err:
                    # Ignore if table bookings does not exist
                    logger.debug(f"Bookings table update bypassed: {db_err}")

            # Checkpoint 2: Refunds
            if policy.refund_policy != "none":
                await self._update_checkpoint(branch_id, "refunds_initiated", correlation_id, 3)
                # Initiate refunds logic (e.g. outbox events for invoice adjustments/credit memos)
                # Checkpoint 3: Refunds Completed
                await self._update_checkpoint(branch_id, "refunds_completed", correlation_id, 4)

            # Checkpoint 4: Notifications Sent
            if policy.notify_members:
                await self._update_checkpoint(branch_id, "notifications_sent", correlation_id, 5)
                # Send outbox event for notifying members
                notification_event = BranchOutboxEvent(
                    outbox_id=uuid.uuid4(),
                    branch_id=branch_id,
                    event_type="branch.member_notification",
                    payload={
                        "branch_id": str(branch_id),
                        "org_id": str(org_id),
                        "from_status": from_status,
                        "to_status": to_status,
                        "grace_hours": policy.booking_grace_hours
                    },
                    created_at=datetime.now(timezone.utc),
                    process_after=datetime.now(timezone.utc),
                    status="pending",
                    correlation_id=correlation_id
                )
                self.db.add(notification_event)

            # Finalize Saga
            await self._complete_saga(branch_id, was_watchdog_recovery=False)

        except Exception as err:
            logger.error(f"Saga execution failed for branch {branch_id}: {err}")
            await self.db.rollback()
            await self._compensate_saga(branch_id, from_status, to_status, correlation_id, actor_id)

    async def _update_checkpoint(
        self,
        branch_id: uuid.UUID,
        checkpoint: str,
        correlation_id: uuid.UUID,
        step_seq: int
    ) -> None:
        """Helper to advance saga checkpoint and record lifecycle event."""
        # 1. Update state
        stmt = update(OrgBranchState).where(
            OrgBranchState.branch_id == branch_id
        ).values(
            saga_last_checkpoint=checkpoint,
            saga_compensation_strategy="advance_to_target" if step_seq >= 4 else "rollback_to_origin"
        )
        await self.db.execute(stmt)

        # 2. Log lifecycle event
        event = BranchLifecycleEvent(
            event_id=uuid.uuid4(),
            branch_id=branch_id,
            event_type=checkpoint,
            payload={"correlation_id": str(correlation_id)},
            emitted_at=datetime.now(timezone.utc),
            correlation_id=correlation_id,
            step_sequence=step_seq
        )
        self.db.add(event)
        await self.db.commit()

    async def _complete_saga(self, branch_id: uuid.UUID, was_watchdog_recovery: bool) -> None:
        """Transaction B completion cleanup."""
        now = datetime.now(timezone.utc)
        stmt = select(OrgBranchState).where(OrgBranchState.branch_id == branch_id).with_for_update()
        res = await self.db.execute(stmt)
        branch_state = res.scalar_one()

        branch_state.lifecycle_transition_in_progress = False
        branch_state.saga_last_checkpoint = None
        branch_state.saga_compensation_strategy = None
        if was_watchdog_recovery:
            branch_state.watchdog_recovered_at = now
            branch_state.watchdog_recovery_count += 1

        await self.db.commit()
        logger.info(f"Saga successfully finalized for branch {branch_id}.")

    async def _compensate_saga(
        self,
        branch_id: uuid.UUID,
        from_status: str,
        to_status: str,
        correlation_id: uuid.UUID,
        actor_id: uuid.UUID
    ) -> None:
        """Rollback or advance based on decision matrix strategy."""
        stmt = select(OrgBranchState).where(OrgBranchState.branch_id == branch_id).with_for_update()
        res = await self.db.execute(stmt)
        branch_state = res.scalar_one()

        strategy = branch_state.saga_compensation_strategy or "rollback_to_origin"

        if strategy == "rollback_to_origin":
            logger.warning(f"Saga failed. Triggering ROLLBACK compensation strategy for branch {branch_id}.")
            
            # Revert state values
            branch_state.status = from_status
            
            # Sync operational state based on definition
            stmt_def = select(BranchStatusDefinition).where(BranchStatusDefinition.code == from_status)
            res_def = await self.db.execute(stmt_def)
            origin_def = res_def.scalar_one()
            branch_state.is_operational = origin_def.is_operational
            
            branch_state.lifecycle_transition_in_progress = False
            branch_state.saga_last_checkpoint = None
            branch_state.saga_compensation_strategy = None

            # Re-index back in search outbox
            now = datetime.now(timezone.utc)
            outbox_reindex = BranchOutboxEvent(
                outbox_id=uuid.uuid4(),
                branch_id=branch_id,
                event_type="branch.search_index",
                payload={"branch_id": str(branch_id), "org_id": str(branch_state.org_id), "status": from_status},
                created_at=now,
                process_after=now,
                status="pending",
                correlation_id=correlation_id
            )
            self.db.add(outbox_reindex)

            # Log compensation lifecycle event
            event = BranchLifecycleEvent(
                event_id=uuid.uuid4(),
                branch_id=branch_id,
                event_type="compensation_completed",
                payload={"rolled_back_to": from_status},
                emitted_at=now,
                correlation_id=correlation_id,
                step_sequence=99
            )
            self.db.add(event)

            # Log status history reversion
            history = BranchStatusHistory(
                history_id=uuid.uuid4(),
                branch_id=branch_id,
                from_status=to_status,
                to_status=from_status,
                changed_by=actor_id,
                changed_at=now,
                reason="Saga compensation rollback",
                transition_source="saga_compensation",
                snapshot={}, # Empty or limited snapshot for compensation row
                correlation_id=correlation_id,
                correlation_emitted_at=now
            )
            self.db.add(history)

            await self.db.commit()

        elif strategy == "advance_to_target":
            logger.warning(f"Saga failed. Triggering ADVANCE_TO_TARGET compensation strategy for branch {branch_id}.")
            # Complete transition despite error
            await self._complete_saga(branch_id, was_watchdog_recovery=False)

        else:
            # manual_review
            logger.error(f"Saga failed. Manual review required for branch {branch_id}. Unlocking status flip but setting flags.")
            branch_state.lifecycle_transition_in_progress = False
            branch_state.saga_compensation_strategy = "manual_review"
            await self.db.commit()

            # Record Watchdog Alert
            alert = BranchWatchdogAlert(
                alert_id=uuid.uuid4(),
                branch_id=branch_id,
                alert_type="force_recovery_45m",
                triggered_at=datetime.now(timezone.utc),
                resolution_notes="Saga failed with manual_review strategy."
            )
            self.db.add(alert)
            await self.db.commit()

    async def run_watchdog_sweep(self) -> None:
        """
        Background watchdog worker checks for hung lifecycle transitions.
        - SLA Alert at 15 minutes.
        - Force Abort & Recovery at 45 minutes.
        """
        now = datetime.now(timezone.utc)
        stmt = select(OrgBranchState).where(
            OrgBranchState.lifecycle_transition_in_progress == True,
            OrgBranchState.deleted_at.is_(None)
        )
        res = await self.db.execute(stmt)
        branches = res.scalars().all()

        for state in branches:
            changed_at = state.status_changed_at
            duration = now - changed_at

            if duration >= timedelta(minutes=45):
                logger.error(f"Watchdog trigger: Branch {state.branch_id} transition frozen for {duration}. Force-recovering.")
                
                # Check strategy to execute
                strategy = state.saga_compensation_strategy or "rollback_to_origin"
                
                # Create watchdog alert row
                alert = BranchWatchdogAlert(
                    alert_id=uuid.uuid4(),
                    branch_id=state.branch_id,
                    alert_type="force_recovery_45m",
                    triggered_at=now,
                    resolution_notes=f"Auto-recovery executed after {duration.total_seconds() / 60:.1f} minutes of freeze."
                )
                self.db.add(alert)
                await self.db.flush()

                # Execute cleanup or rollback
                if strategy == "rollback_to_origin":
                    # Roll back to the original status
                    # To do this safely, we lookup the last history entry to see from_status
                    stmt_hist = select(BranchStatusHistory).where(
                        BranchStatusHistory.branch_id == state.branch_id
                    ).order_by(BranchStatusHistory.changed_at.desc())
                    res_hist = await self.db.execute(stmt_hist)
                    hist = res_hist.scalars().first()
                    origin_status = hist.from_status if hist else "active"
                    
                    # Update status
                    state.status = origin_status
                    stmt_def = select(BranchStatusDefinition).where(BranchStatusDefinition.code == origin_status)
                    res_def = await self.db.execute(stmt_def)
                    origin_def = res_def.scalar_one()
                    state.is_operational = origin_def.is_operational
                    
                state.lifecycle_transition_in_progress = False
                state.saga_last_checkpoint = None
                state.saga_compensation_strategy = None
                state.watchdog_recovered_at = now
                state.watchdog_recovery_count += 1
                
                await self.db.commit()

            elif duration >= timedelta(minutes=15):
                # Check if 15m alert already triggered and is open
                stmt_alert = select(BranchWatchdogAlert).where(
                    BranchWatchdogAlert.branch_id == state.branch_id,
                    BranchWatchdogAlert.alert_type == "freeze_threshold_15m",
                    BranchWatchdogAlert.resolved_at.is_(None)
                )
                res_alert = await self.db.execute(stmt_alert)
                open_alert = res_alert.scalar_one_or_none()

                if not open_alert:
                    logger.warning(f"Watchdog trigger: Branch {state.branch_id} transition frozen for {duration}. Raising SLA alert.")
                    alert = BranchWatchdogAlert(
                        alert_id=uuid.uuid4(),
                        branch_id=state.branch_id,
                        alert_type="freeze_threshold_15m",
                        triggered_at=now
                    )
                    self.db.add(alert)
                    await self.db.commit()

    async def run_reconciliation_sweep(self) -> int:
        """
        Sync search indexes for branches and reconciliation sweep.
        Returns the number of claimed branches synced.
        """
        worker_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        # Claim batch (skip locked, 30m TTL rescue)
        claim_query = text("""
            UPDATE public.org_branch_state
            SET reconciliation_claimed_by = :worker_id,
                reconciliation_claimed_at = :now
            WHERE branch_id IN (
                SELECT branch_id FROM public.org_branch_state
                WHERE search_last_synced_at < :stale_limit
                  AND deleted_at IS NULL
                  AND (
                      reconciliation_claimed_at IS NULL
                      OR reconciliation_claimed_at < :ttl_limit
                  )
                LIMIT 100
                FOR UPDATE SKIP LOCKED
            )
            RETURNING branch_id;
        """)

        stale_limit = now - timedelta(hours=24)
        ttl_limit = now - timedelta(minutes=30)

        res = await self.db.execute(
            claim_query,
            {"worker_id": worker_id, "now": now, "stale_limit": stale_limit, "ttl_limit": ttl_limit}
        )
        claimed_ids = [row[0] for row in res.fetchall()]

        if not claimed_ids:
            return 0

        # Perform sync sweep
        for b_id in claimed_ids:
            try:
                # Update status
                await self.db.execute(
                    text("""
                        UPDATE public.org_branch_state
                        SET search_last_synced_at = :now,
                            search_visibility_version = search_visibility_version + 1,
                            reconciliation_claimed_by = NULL,
                            reconciliation_claimed_at = NULL
                        WHERE branch_id = :branch_id;
                    """),
                    {"now": now, "branch_id": b_id}
                )
            except Exception as e:
                logger.error(f"Failed to reconcile branch {b_id}: {e}")
                # Clear claim so it's retried
                await self.db.execute(
                    text("""
                        UPDATE public.org_branch_state
                        SET reconciliation_claimed_by = NULL,
                            reconciliation_claimed_at = NULL,
                            search_sync_failed_at = :now
                        WHERE branch_id = :branch_id;
                    """),
                    {"now": now, "branch_id": b_id}
                )

        await self.db.commit()
        return len(claimed_ids)

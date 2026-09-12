from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_branch import OrgBranchState
from app.models.branch_lifecycle import (
    BranchStatusDefinition,
    BranchStatusTransition,
    BranchDeactivationPolicy,
    BranchStatusHistory,
    BranchLifecycleEvent,
    BranchOutboxEvent,
    BranchWatchdogAlert,
)


logger = logging.getLogger(__name__)


class BranchLifecycleService:
    """Branch lifecycle state machine with durable Transaction-A intent.

    Transaction A runs in the request transaction and persists the state flip,
    immutable history/event records, and a ``branch.lifecycle_saga`` outbox row.
    Transaction B is database-only: booking mutation plus durable external
    commands are committed atomically by the leased worker. No request-scoped
    session may cross into deferred execution and no external command is marked
    delivered by this service.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def initiate_transition(
        self,
        branch_id: uuid.UUID,
        org_id: uuid.UUID,
        to_status: str,
        actor_id: uuid.UUID,
        actor_role: str,
        reason: Optional[str] = None,
        transition_source: str = "api",
    ) -> uuid.UUID:
        """Execute durable Transaction A and return its correlation id."""

        # Authorization must happen before any write-intent lock. A caller who
        # may read a branch but cannot transition it receives 403 rather than a
        # row-lock/RLS-induced false 404.
        read_stmt = select(OrgBranchState).where(
            OrgBranchState.branch_id == branch_id,
            OrgBranchState.org_id == org_id,
        )
        read_res = await self.db.execute(read_stmt)
        visible_state = read_res.scalar_one_or_none()
        if visible_state is None:
            raise HTTPException(status_code=404, detail="Branch not found")

        from_status = visible_state.status
        transition_stmt = select(BranchStatusTransition).where(
            BranchStatusTransition.from_status == from_status,
            BranchStatusTransition.to_status == to_status,
        )
        transition = (await self.db.execute(transition_stmt)).scalar_one_or_none()
        if transition is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Transition {from_status} -> {to_status} is not allowed",
            )
        if actor_role not in transition.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{actor_role}' is not authorized for this transition",
            )
        if transition.requires_reason and not (reason and reason.strip()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A status reason is required for this transition",
            )

        target_def = (
            await self.db.execute(
                select(BranchStatusDefinition).where(
                    BranchStatusDefinition.code == to_status
                )
            )
        ).scalar_one_or_none()
        if target_def is None:
            raise HTTPException(status_code=400, detail="Unknown target status")

        # Serialize branch transitions and the org-wide last-operational-branch
        # invariant in a stable lock order: organization advisory lock first,
        # then branch advisory lock, then the row lock.
        await self.db.execute(
            text(
                "SELECT pg_catalog.pg_advisory_xact_lock("
                "pg_catalog.hashtextextended(:scope, 0))"
            ),
            {"scope": f"branch-lifecycle:org:{org_id}"},
        )
        await self.db.execute(
            text(
                "SELECT pg_catalog.pg_advisory_xact_lock("
                "pg_catalog.hashtextextended(:scope, 0))"
            ),
            {"scope": f"branch-lifecycle:branch:{branch_id}"},
        )

        locked_stmt = (
            select(OrgBranchState)
            .where(
                OrgBranchState.branch_id == branch_id,
                OrgBranchState.org_id == org_id,
            )
            .with_for_update()
        )
        branch_state = (await self.db.execute(locked_stmt)).scalar_one_or_none()
        if branch_state is None:
            raise HTTPException(status_code=404, detail="Branch not found")
        if branch_state.status != from_status:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Branch status changed concurrently; retry the transition",
            )
        if branch_state.lifecycle_transition_in_progress:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A lifecycle transition is already in progress for this branch",
            )

        if branch_state.is_operational and not target_def.is_operational:
            operational_count = await self.db.scalar(
                select(func.count())
                .select_from(OrgBranchState)
                .where(
                    OrgBranchState.org_id == org_id,
                    OrgBranchState.is_operational.is_(True),
                    OrgBranchState.deleted_at.is_(None),
                )
            )
            if int(operational_count or 0) <= 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot deactivate the last operational branch",
                )

        now = datetime.now(timezone.utc)
        correlation_id = uuid.uuid4()

        branch_state.status = to_status
        branch_state.is_operational = target_def.is_operational
        branch_state.status_changed_at = now
        branch_state.status_changed_by = actor_id
        branch_state.status_reason = reason.strip() if reason and reason.strip() else None
        branch_state.transition_source = transition_source
        branch_state.lifecycle_transition_in_progress = True
        branch_state.saga_last_checkpoint = None
        # Transaction B is atomic. If it repeatedly fails, compensation may
        # safely roll Transaction A back because no partial B commit exists.
        branch_state.saga_compensation_strategy = "rollback_to_origin"

        event = BranchLifecycleEvent(
            event_id=uuid.uuid4(),
            branch_id=branch_id,
            event_type="status_change_initiated",
            payload={
                "org_id": str(org_id),
                "from_status": from_status,
                "to_status": to_status,
                "reason": reason,
                "actor_id": str(actor_id),
                "actor_role": actor_role,
            },
            emitted_at=now,
            correlation_id=correlation_id,
            step_sequence=1,
        )
        self.db.add(event)
        await self.db.flush()

        history = BranchStatusHistory(
            history_id=uuid.uuid4(),
            branch_id=branch_id,
            from_status=from_status,
            to_status=to_status,
            changed_by=actor_id,
            changed_at=now,
            reason=reason,
            transition_source=transition_source,
            snapshot={
                "is_operational": target_def.is_operational,
                "actor_role": actor_role,
            },
            correlation_id=correlation_id,
            correlation_emitted_at=now,
        )
        self.db.add(history)

        # Search visibility is an external command and must never be presented
        # as delivered merely because the state row changed.
        self.db.add(
            BranchOutboxEvent(
                outbox_id=uuid.uuid4(),
                tenant_id=org_id,
                branch_id=branch_id,
                event_type="branch.search_deindex"
                if not target_def.is_operational
                else "branch.search_index",
                payload={
                    "branch_id": str(branch_id),
                    "org_id": str(org_id),
                    "status": to_status,
                },
                created_at=now,
                process_after=now,
                status="pending",
                attempt_count=0,
                max_attempts=5,
                correlation_id=correlation_id,
            )
        )

        # This row is the durable replacement for FastAPI BackgroundTasks.
        self.db.add(
            BranchOutboxEvent(
                outbox_id=uuid.uuid4(),
                tenant_id=org_id,
                branch_id=branch_id,
                event_type="branch.lifecycle_saga",
                payload={
                    "branch_id": str(branch_id),
                    "org_id": str(org_id),
                    "from_status": from_status,
                    "to_status": to_status,
                    "actor_id": str(actor_id),
                    "actor_role": actor_role,
                },
                created_at=now,
                process_after=now,
                status="pending",
                attempt_count=0,
                max_attempts=15,
                correlation_id=correlation_id,
            )
        )

        await self.db.commit()
        return correlation_id

    async def _record_checkpoint(
        self,
        branch_state: OrgBranchState,
        checkpoint: str,
        correlation_id: uuid.UUID,
        step_sequence: int,
        *,
        compensation_strategy: str = "rollback_to_origin",
    ) -> None:
        """Record a checkpoint without committing the worker transaction."""

        branch_state.saga_last_checkpoint = checkpoint
        branch_state.saga_compensation_strategy = compensation_strategy
        self.db.add(
            BranchLifecycleEvent(
                event_id=uuid.uuid4(),
                branch_id=branch_state.branch_id,
                event_type=checkpoint,
                payload={"checkpoint": checkpoint},
                emitted_at=datetime.now(timezone.utc),
                correlation_id=correlation_id,
                step_sequence=step_sequence,
            )
        )
        await self.db.flush()

    # Preserve the old method name for focused tests and callers while changing
    # its transaction semantics from "commit before effect" to "flush only".
    async def _update_checkpoint(
        self,
        branch_id: uuid.UUID,
        checkpoint: str,
        compensation_strategy: str,
        correlation_id: uuid.UUID,
        step_sequence: int,
    ) -> None:
        state = (
            await self.db.execute(
                select(OrgBranchState)
                .where(OrgBranchState.branch_id == branch_id)
                .with_for_update()
            )
        ).scalar_one()
        await self._record_checkpoint(
            state,
            checkpoint,
            correlation_id,
            step_sequence,
            compensation_strategy=compensation_strategy,
        )

    async def _enqueue_child_command(
        self,
        *,
        branch_state: OrgBranchState,
        event_type: str,
        payload: dict,
        correlation_id: uuid.UUID,
        parent_outbox_id: Optional[uuid.UUID],
        worker_id: Optional[uuid.UUID],
    ) -> None:
        child_id = uuid.uuid5(
            correlation_id,
            f"{event_type}:{branch_state.branch_id}",
        )
        full_payload = {
            **payload,
            "branch_id": str(branch_state.branch_id),
            "org_id": str(branch_state.org_id),
        }

        if parent_outbox_id is not None and worker_id is not None:
            await self.db.execute(
                text(
                    """
                    SELECT public.enqueue_branch_lifecycle_child(
                        :parent_outbox_id,
                        :worker_id,
                        :event_type,
                        CAST(:payload AS jsonb),
                        :child_id
                    )
                    """
                ),
                {
                    "parent_outbox_id": parent_outbox_id,
                    "worker_id": worker_id,
                    "event_type": event_type,
                    "payload": __import__("json").dumps(full_payload),
                    "child_id": child_id,
                },
            )
            return

        # Direct invocation remains useful to deterministic service tests and
        # explicit foreground/admin repair. Production HTTP routing never calls
        # Transaction B directly; worker processing always supplies a live
        # parent_outbox_id + worker_id and therefore uses the bounded function.
        self.db.add(
            BranchOutboxEvent(
                outbox_id=child_id,
                tenant_id=branch_state.org_id,
                branch_id=branch_state.branch_id,
                event_type=event_type,
                payload=full_payload,
                created_at=datetime.now(timezone.utc),
                process_after=datetime.now(timezone.utc),
                status="pending",
                attempt_count=0,
                max_attempts=5,
                correlation_id=correlation_id,
            )
        )
        await self.db.flush()

    async def execute_saga_cascade(
        self,
        branch_id: uuid.UUID,
        org_id: uuid.UUID,
        from_status: str,
        to_status: str,
        correlation_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        parent_outbox_id: Optional[uuid.UUID] = None,
        worker_id: Optional[uuid.UUID] = None,
    ) -> None:
        """Execute Transaction B atomically.

        No external API is called here. Refund/search/notification work becomes
        durable child commands. The leased poller commits this method's database
        effects and the parent delivered marker together.
        """

        state = (
            await self.db.execute(
                select(OrgBranchState)
                .where(
                    OrgBranchState.branch_id == branch_id,
                    OrgBranchState.org_id == org_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if state is None:
            raise RuntimeError("Lifecycle saga branch is no longer visible")
        if not state.lifecycle_transition_in_progress:
            # Idempotent retry after the successful transaction committed but an
            # outer caller retried its own request: state already proves done.
            if state.status == to_status:
                return
            raise RuntimeError("Lifecycle saga state is not in progress")
        if state.status != to_status:
            raise RuntimeError(
                f"Lifecycle saga target drift: expected={to_status}, observed={state.status}"
            )

        policy = (
            await self.db.execute(
                select(BranchDeactivationPolicy).where(
                    BranchDeactivationPolicy.from_status == from_status,
                    BranchDeactivationPolicy.to_status == to_status,
                )
            )
        ).scalar_one_or_none()

        await self._record_checkpoint(
            state,
            "transaction_b_started",
            correlation_id,
            2,
        )

        if policy and policy.auto_cancel_bookings:
            # The booking relation is optional in the current product lineage.
            # If it exists, failure (including missing worker privilege) aborts
            # Transaction B and is retried; never catch permission/schema errors
            # and continue from an aborted PostgreSQL transaction.
            relation_exists = await self.db.scalar(
                text("SELECT pg_catalog.to_regclass('public.bookings') IS NOT NULL")
            )
            if relation_exists:
                await self.db.execute(
                    text(
                        """
                        UPDATE public.bookings
                        SET status = 'cancelled',
                            updated_at = pg_catalog.clock_timestamp()
                        WHERE branch_id = :branch_id
                          AND status IN ('confirmed', 'pending')
                        """
                    ),
                    {"branch_id": branch_id},
                )

        await self._record_checkpoint(
            state,
            "bookings_processed",
            correlation_id,
            3,
        )

        if policy and policy.refund_policy != "none":
            await self._enqueue_child_command(
                branch_state=state,
                event_type="branch.refund_required",
                payload={
                    "refund_policy": policy.refund_policy,
                    "from_status": from_status,
                    "to_status": to_status,
                },
                correlation_id=correlation_id,
                parent_outbox_id=parent_outbox_id,
                worker_id=worker_id,
            )
            await self._record_checkpoint(
                state,
                "refunds_queued",
                correlation_id,
                4,
            )

        if policy and policy.notify_members:
            await self._enqueue_child_command(
                branch_state=state,
                event_type="branch.member_notification",
                payload={
                    "from_status": from_status,
                    "to_status": to_status,
                },
                correlation_id=correlation_id,
                parent_outbox_id=parent_outbox_id,
                worker_id=worker_id,
            )
            await self._record_checkpoint(
                state,
                "notifications_queued",
                correlation_id,
                5,
            )

        state.lifecycle_transition_in_progress = False
        state.saga_last_checkpoint = None
        state.saga_compensation_strategy = None
        self.db.add(
            BranchLifecycleEvent(
                event_id=uuid.uuid4(),
                branch_id=branch_id,
                event_type="saga_database_completed",
                payload={
                    "from_status": from_status,
                    "to_status": to_status,
                    "external_commands_are_async": True,
                },
                emitted_at=datetime.now(timezone.utc),
                correlation_id=correlation_id,
                step_sequence=6,
            )
        )
        await self.db.flush()

        # Foreground direct service tests/admin repair own their transaction.
        # Production worker callers pass a parent lease and perform the one
        # commit only after marking that parent delivered in the same session.
        if parent_outbox_id is None:
            await self.db.commit()

    async def compensate_saga_from_dead_letter(
        self,
        *,
        branch_id: uuid.UUID,
        org_id: uuid.UUID,
        from_status: str,
        to_status: str,
        correlation_id: uuid.UUID,
        actor_id: uuid.UUID,
        parent_outbox_id: uuid.UUID,
        worker_id: uuid.UUID,
    ) -> None:
        """Rollback Transaction A after Transaction B exhausts retries.

        This compensation is safe because Transaction B commits atomically; a
        dead-lettered parent proves no B transaction reached its commit point.
        Search restoration is itself a durable child command.
        """

        state = (
            await self.db.execute(
                select(OrgBranchState)
                .where(
                    OrgBranchState.branch_id == branch_id,
                    OrgBranchState.org_id == org_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        if not state.lifecycle_transition_in_progress:
            return
        if state.status != to_status:
            raise RuntimeError(
                "Lifecycle compensation target drift: "
                f"expected={to_status}, observed={state.status}"
            )
        if state.saga_compensation_strategy != "rollback_to_origin":
            raise RuntimeError(
                "Lifecycle compensation strategy drift: "
                f"observed={state.saga_compensation_strategy!r}"
            )
        if state.saga_last_checkpoint is not None:
            raise RuntimeError(
                "Lifecycle compensation refuses persisted Transaction-B checkpoint: "
                f"{state.saga_last_checkpoint!r}"
            )
        if state.status_changed_by != actor_id:
            raise RuntimeError(
                "Lifecycle compensation actor drift: "
                f"expected={actor_id}, observed={state.status_changed_by}"
            )

        origin_def = (
            await self.db.execute(
                select(BranchStatusDefinition).where(
                    BranchStatusDefinition.code == from_status
                )
            )
        ).scalar_one()
        now = datetime.now(timezone.utc)
        compensation_reason = "Saga dead-letter compensation rollback"
        state.status = from_status
        state.is_operational = origin_def.is_operational
        state.status_changed_at = now
        state.status_reason = compensation_reason
        state.transition_source = "saga_compensation"
        state.lifecycle_transition_in_progress = False
        state.saga_last_checkpoint = None
        state.saga_compensation_strategy = None

        await self._enqueue_child_command(
            branch_state=state,
            event_type="branch.search_index",
            payload={"status": from_status, "reason": "saga_dead_letter_compensation"},
            correlation_id=correlation_id,
            parent_outbox_id=parent_outbox_id,
            worker_id=worker_id,
        )
        self.db.add(
            BranchLifecycleEvent(
                event_id=uuid.uuid4(),
                branch_id=branch_id,
                event_type="compensation_completed",
                payload={"rolled_back_to": from_status},
                emitted_at=now,
                correlation_id=correlation_id,
                step_sequence=99,
            )
        )
        self.db.add(
            BranchStatusHistory(
                history_id=uuid.uuid4(),
                branch_id=branch_id,
                from_status=to_status,
                to_status=from_status,
                changed_by=actor_id,
                changed_at=now,
                reason=compensation_reason,
                transition_source="saga_compensation",
                snapshot={"reason": "transaction_b_retry_exhausted"},
                correlation_id=correlation_id,
                correlation_emitted_at=now,
            )
        )
        await self.db.flush()

    async def run_watchdog_sweep(self) -> None:
        """Alert on frozen transitions; do not race a live durable saga worker."""

        now = datetime.now(timezone.utc)
        states = (
            await self.db.execute(
                select(OrgBranchState).where(
                    OrgBranchState.lifecycle_transition_in_progress.is_(True),
                    OrgBranchState.deleted_at.is_(None),
                )
            )
        ).scalars().all()

        for state in states:
            changed_at = state.status_changed_at
            if changed_at is None:
                logger.error(
                    "Lifecycle transition for branch %s has no status_changed_at",
                    state.branch_id,
                )
                continue
            duration = now - changed_at
            if duration < timedelta(minutes=15):
                continue

            open_alert = (
                await self.db.execute(
                    select(BranchWatchdogAlert).where(
                        BranchWatchdogAlert.branch_id == state.branch_id,
                        BranchWatchdogAlert.alert_type == "freeze_threshold_15m",
                        BranchWatchdogAlert.resolved_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if open_alert is None:
                self.db.add(
                    BranchWatchdogAlert(
                        alert_id=uuid.uuid4(),
                        branch_id=state.branch_id,
                        alert_type="freeze_threshold_15m",
                        triggered_at=now,
                        resolution_notes=(
                            "Durable lifecycle saga has exceeded 15 minutes; "
                            "worker/outbox state must be inspected."
                        ),
                    )
                )
                await self.db.commit()

            # Do not auto-roll back at 45 minutes while durable work may still
            # be pending/leased. Retry exhaustion is handled explicitly by the
            # outbox worker, which has the parent lease required for safe
            # compensation and child search restoration.
            if duration >= timedelta(minutes=45):
                logger.error(
                    "Lifecycle saga for branch %s has exceeded 45 minutes; "
                    "awaiting explicit outbox retry/dead-letter resolution",
                    state.branch_id,
                )

    async def run_reconciliation_sweep(self) -> int:
        """Enqueue bounded search repairs without fabricating provider success.

        P4B makes ``search_last_synced_at`` downstream-evidence-only. Maintenance
        therefore has one job: ask the SECURITY DEFINER reconciliation capability
        to enqueue current authoritative search work. Only the leased search
        worker may later acknowledge a provider-verified effect.
        """

        enqueued_count = await self.db.scalar(
            text(
                "SELECT app_secure.enqueue_branch_search_reconciliation("
                "CAST(:batch_size AS integer))"
            ),
            {"batch_size": 100},
        )
        await self.db.commit()
        return int(enqueued_count or 0)

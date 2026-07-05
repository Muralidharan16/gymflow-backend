from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from typing import Iterable

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.subscription_lifecycle import (
    BranchBrief,
    FreezeSummary,
    LifecycleV2Projection,
    MemberBrief,
    MultipleCurrentTermsError,
    SeriesSummary,
    SlotSummary,
    SubscriptionAssignmentStatus,
    SubscriptionFreezeStatus,
    SubscriptionOperationalStatus,
    SubscriptionSeriesNotFoundError,
    SubscriptionSeriesStatus,
    SubscriptionSlotRole,
    SubscriptionTermStatus,
    TermSummary,
    TimelineItem,
    available_actions,
    is_freeze_active,
    resolve_term_status,
)
from app.models.member import Member
from app.models.org_branch import OrgBranch
from app.models.subscription_lifecycle import (
    SubscriptionEvent,
    SubscriptionFreeze,
    SubscriptionSeries,
    SubscriptionSlotAssignment,
    SubscriptionTerm,
    SubscriptionTermSlot,
)


class SubscriptionLifecycleRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_series_by_id(self, org_id: uuid.UUID, series_id: uuid.UUID) -> SubscriptionSeries | None:
        result = await self.db.execute(
            select(SubscriptionSeries).where(
                SubscriptionSeries.org_id == org_id,
                SubscriptionSeries.id == series_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_term_by_id(self, org_id: uuid.UUID, term_id: uuid.UUID) -> SubscriptionTerm | None:
        result = await self.db.execute(
            select(SubscriptionTerm).where(
                SubscriptionTerm.org_id == org_id,
                SubscriptionTerm.id == term_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_series_summaries(
        self,
        org_id: uuid.UUID,
        *,
        business_date: date,
        branch_id: uuid.UUID | None = None,
        search: str | None = None,
        primary_member_id: uuid.UUID | None = None,
        plan_id: uuid.UUID | None = None,
        operational_status: SubscriptionOperationalStatus | None = None,
        lifecycle_status: SubscriptionSeriesStatus | None = None,
        has_scheduled_renewal: bool | None = None,
        has_vacant_slots: bool | None = None,
        include_archived: bool = False,
        page: int = 1,
        size: int = 25,
    ) -> tuple[list[SeriesSummary], int]:
        page = max(page, 1)
        size = min(max(size, 1), 100)

        stmt = self._series_base_select(org_id)
        if branch_id:
            stmt = stmt.where(SubscriptionSeries.originating_branch_id == branch_id)
        if primary_member_id:
            stmt = stmt.where(SubscriptionSeries.primary_member_id == primary_member_id)
        if lifecycle_status:
            stmt = stmt.where(SubscriptionSeries.lifecycle_status == lifecycle_status)
        elif not include_archived:
            stmt = stmt.where(SubscriptionSeries.lifecycle_status != SubscriptionSeriesStatus.archived)
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(
                    SubscriptionSeries.series_code.ilike(pattern),
                    Member.name.ilike(pattern),
                    Member.phone.ilike(pattern),
                    OrgBranch.branch_name.ilike(pattern),
                )
            )
        if plan_id:
            stmt = stmt.where(
                SubscriptionSeries.id.in_(
                    select(SubscriptionTerm.series_id).where(
                        SubscriptionTerm.org_id == org_id,
                        SubscriptionTerm.plan_id == plan_id,
                    )
                )
            )

        total = await self._count(stmt)
        page_rows = (
            await self.db.execute(
                stmt.order_by(SubscriptionSeries.opened_at.desc(), SubscriptionSeries.id)
                .offset((page - 1) * size)
                .limit(size)
            )
        ).all()

        summaries = await self._build_series_summaries(org_id, page_rows, business_date)

        # These filters depend on derived state. Phase 3 keeps them read-only and
        # local to the current page; API cutover can promote them to SQL windows.
        if operational_status:
            summaries = [item for item in summaries if item.operational_status == operational_status]
        if has_scheduled_renewal is not None:
            summaries = [
                item for item in summaries if (item.scheduled_next_term is not None) == has_scheduled_renewal
            ]
        if has_vacant_slots is not None:
            summaries = [item for item in summaries if (item.vacant_slots > 0) == has_vacant_slots]

        return summaries, total

    async def get_series_detail(
        self,
        org_id: uuid.UUID,
        series_id: uuid.UUID,
        *,
        business_date: date,
        history_limit: int = 10,
    ) -> SeriesSummary:
        rows = (
            await self.db.execute(
                self._series_base_select(org_id).where(SubscriptionSeries.id == series_id).limit(1)
            )
        ).all()
        if not rows:
            raise SubscriptionSeriesNotFoundError(f"Subscription series {series_id} was not found")
        summaries = await self._build_series_summaries(org_id, rows, business_date)
        return summaries[0]

    async def list_upcoming_terms(
        self,
        org_id: uuid.UUID,
        *,
        business_date: date,
        branch_id: uuid.UUID | None = None,
        page: int = 1,
        size: int = 25,
    ) -> tuple[list[TermSummary], int]:
        stmt = select(SubscriptionTerm).where(
            SubscriptionTerm.org_id == org_id,
            SubscriptionTerm.status.in_([SubscriptionTermStatus.active, SubscriptionTermStatus.scheduled]),
            SubscriptionTerm.starts_on > business_date,
        )
        if branch_id:
            stmt = stmt.where(SubscriptionTerm.branch_id == branch_id)
        return await self._term_page(stmt.order_by(SubscriptionTerm.starts_on, SubscriptionTerm.sequence_number), business_date, page, size)

    async def list_history_terms(
        self,
        org_id: uuid.UUID,
        *,
        business_date: date,
        series_id: uuid.UUID | None = None,
        member_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        plan_id: uuid.UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        page: int = 1,
        size: int = 25,
    ) -> tuple[list[TermSummary], int]:
        terminal_statuses = [
            SubscriptionTermStatus.expired,
            SubscriptionTermStatus.cancelled,
            SubscriptionTermStatus.terminated,
            SubscriptionTermStatus.voided,
        ]
        stmt = select(SubscriptionTerm).where(
            SubscriptionTerm.org_id == org_id,
            or_(
                SubscriptionTerm.status.in_(terminal_statuses),
                SubscriptionTerm.effective_ends_on < business_date,
            ),
        )
        if series_id:
            stmt = stmt.where(SubscriptionTerm.series_id == series_id)
        if member_id:
            stmt = stmt.where(
                SubscriptionTerm.series_id.in_(
                    select(SubscriptionSeries.id).where(
                        SubscriptionSeries.org_id == org_id,
                        SubscriptionSeries.primary_member_id == member_id,
                    )
                )
            )
        if branch_id:
            stmt = stmt.where(SubscriptionTerm.branch_id == branch_id)
        if plan_id:
            stmt = stmt.where(SubscriptionTerm.plan_id == plan_id)
        if from_date:
            stmt = stmt.where(SubscriptionTerm.effective_ends_on >= from_date)
        if to_date:
            stmt = stmt.where(SubscriptionTerm.starts_on <= to_date)

        return await self._term_page(stmt.order_by(SubscriptionTerm.effective_ends_on.desc()), business_date, page, size)

    async def list_all_terms(
        self,
        org_id: uuid.UUID,
        *,
        business_date: date,
        page: int = 1,
        size: int = 25,
    ) -> tuple[list[TermSummary], int]:
        stmt = select(SubscriptionTerm).where(SubscriptionTerm.org_id == org_id)
        return await self._term_page(stmt.order_by(SubscriptionTerm.starts_on.desc()), business_date, page, size)

    async def list_archived_series(
        self,
        org_id: uuid.UUID,
        *,
        business_date: date,
        page: int = 1,
        size: int = 25,
    ) -> tuple[list[SeriesSummary], int]:
        return await self.list_series_summaries(
            org_id,
            business_date=business_date,
            lifecycle_status=SubscriptionSeriesStatus.archived,
            include_archived=True,
            page=page,
            size=size,
        )

    async def list_slots(
        self,
        org_id: uuid.UUID,
        term_id: uuid.UUID,
        *,
        business_date: date,
    ) -> list[SlotSummary]:
        slots = (
            await self.db.execute(
                select(SubscriptionTermSlot)
                .where(SubscriptionTermSlot.org_id == org_id, SubscriptionTermSlot.term_id == term_id)
                .order_by(SubscriptionTermSlot.slot_index)
            )
        ).scalars().all()
        if not slots:
            return []

        slot_ids = [slot.id for slot in slots]
        assignment_rows = (
            await self.db.execute(
                select(SubscriptionSlotAssignment, Member)
                .join(Member, Member.id == SubscriptionSlotAssignment.member_id)
                .where(
                    SubscriptionSlotAssignment.org_id == org_id,
                    SubscriptionSlotAssignment.term_slot_id.in_(slot_ids),
                    self._assignment_active_on(business_date),
                )
                .order_by(SubscriptionSlotAssignment.assigned_at.desc())
            )
        ).all()
        assignments_by_slot: dict[uuid.UUID, tuple[SubscriptionSlotAssignment, Member]] = {}
        for assignment, member in assignment_rows:
            assignments_by_slot.setdefault(assignment.term_slot_id, (assignment, member))

        summaries = []
        for slot in slots:
            assignment_member = assignments_by_slot.get(slot.id)
            if assignment_member:
                assignment, member = assignment_member
                current_member = self._member_brief(member)
                effective_from = assignment.effective_from
                effective_until = assignment.effective_until
            else:
                current_member = None
                effective_from = None
                effective_until = None
            summaries.append(
                SlotSummary(
                    id=slot.id,
                    slot_index=slot.slot_index,
                    role=slot.slot_role,
                    current_member=current_member,
                    effective_from=effective_from,
                    effective_until=effective_until,
                    is_vacant=current_member is None,
                )
            )
        return summaries

    async def list_timeline(
        self,
        org_id: uuid.UUID,
        series_id: uuid.UUID,
        *,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[TimelineItem], int]:
        stmt = select(SubscriptionEvent).where(
            SubscriptionEvent.org_id == org_id,
            SubscriptionEvent.series_id == series_id,
        )
        total = await self._count(stmt)
        rows = (
            await self.db.execute(
                stmt.order_by(SubscriptionEvent.event_at.desc(), SubscriptionEvent.id)
                .offset((max(page, 1) - 1) * min(max(size, 1), 100))
                .limit(min(max(size, 1), 100))
            )
        ).scalars().all()
        return [
            TimelineItem(
                id=event.id,
                event_type=event.event_type,
                event_at=event.event_at,
                actor_user_id=event.actor_user_id,
                term_id=event.term_id,
                metadata=event.metadata_json,
            )
            for event in rows
        ], total

    async def get_v2_projection(
        self,
        org_id: uuid.UUID,
        series_id: uuid.UUID,
        *,
        business_date: date,
    ) -> LifecycleV2Projection | None:
        summaries = await self._build_series_summaries(
            org_id,
            (
                await self.db.execute(
                    self._series_base_select(org_id).where(SubscriptionSeries.id == series_id)
                )
            ).all(),
            business_date,
        )
        if not summaries or not summaries[0].current_term:
            return None

        summary = summaries[0]
        term = summary.current_term
        return LifecycleV2Projection(
            id=summary.id,
            org_id=summary.org_id,
            branch_id=term.branch_id,
            primary_member_id=summary.primary_member.id,
            membership_plan_id=term.plan_id,
            subscription_code=term.term_code,
            start_date=term.starts_on,
            end_date=term.effective_ends_on,
            status=term.derived_status,
            price_snapshot=term.price,
            currency_code=term.currency_code,
            duration_value_snapshot=term.duration_value,
            duration_unit_snapshot=term.duration_unit,
            max_members_snapshot=term.capacity,
            assigned_member_ids=[],
        )

    def _series_base_select(self, org_id: uuid.UUID) -> Select:
        return (
            select(SubscriptionSeries, Member, OrgBranch)
            .join(Member, Member.id == SubscriptionSeries.primary_member_id)
            .outerjoin(OrgBranch, and_(OrgBranch.id == SubscriptionSeries.originating_branch_id, OrgBranch.org_id == org_id))
            .where(SubscriptionSeries.org_id == org_id)
        )

    async def _build_series_summaries(
        self,
        org_id: uuid.UUID,
        rows: Iterable[tuple[SubscriptionSeries, Member, OrgBranch | None]],
        business_date: date,
    ) -> list[SeriesSummary]:
        row_list = list(rows)
        if not row_list:
            return []

        series_ids = [row[0].id for row in row_list]
        current_terms = await self._current_terms_by_series(org_id, series_ids, business_date)
        scheduled_terms = await self._scheduled_next_terms_by_series(org_id, series_ids, business_date)
        previous_counts = await self._previous_term_counts(org_id, series_ids, business_date)

        relevant_terms = [term for term in current_terms.values() if term]
        relevant_terms.extend(term for term in scheduled_terms.values() if term)
        term_ids = [term.id for term in relevant_terms]
        freeze_by_term = await self._active_freeze_by_term(org_id, term_ids, business_date)
        assignment_counts = await self._active_assignment_counts(org_id, term_ids, business_date)
        renewal_children = await self._renewal_children_by_parent(org_id, term_ids)

        summaries: list[SeriesSummary] = []
        for series, member, branch in row_list:
            current_term = current_terms.get(series.id)
            scheduled_term = scheduled_terms.get(series.id)
            current_freeze = self._freeze_summary(freeze_by_term.get(current_term.id)) if current_term else None
            current_summary = self._term_summary(
                current_term,
                business_date,
                freeze=current_freeze,
                assignment_count=assignment_counts.get(current_term.id, 0) if current_term else 0,
                renewal_child_term_id=renewal_children.get(current_term.id) if current_term else None,
            ) if current_term else None
            scheduled_summary = self._term_summary(
                scheduled_term,
                business_date,
                freeze=None,
                assignment_count=assignment_counts.get(scheduled_term.id, 0) if scheduled_term else 0,
                renewal_child_term_id=renewal_children.get(scheduled_term.id) if scheduled_term else None,
            ) if scheduled_term else None

            capacity = current_summary.capacity if current_summary else 0
            occupied = current_summary.assignment_count if current_summary else 0
            operational_status = current_summary.derived_status if current_summary else None
            summaries.append(
                SeriesSummary(
                    id=series.id,
                    series_code=series.series_code,
                    org_id=series.org_id,
                    branch=self._branch_brief(branch),
                    primary_member=self._member_brief(member),
                    lifecycle_status=series.lifecycle_status,
                    operational_status=operational_status,
                    current_term=current_summary,
                    scheduled_next_term=scheduled_summary,
                    previous_term_count=previous_counts.get(series.id, 0),
                    capacity=capacity,
                    occupied_slots=occupied,
                    vacant_slots=max(capacity - occupied, 0),
                    current_freeze=current_freeze,
                    available_actions=available_actions(
                        series.lifecycle_status,
                        operational_status,
                        has_scheduled_renewal=scheduled_summary is not None,
                        has_active_freeze=is_freeze_active(current_freeze, business_date),
                    ),
                    opened_at=series.opened_at,
                    archived_at=series.archived_at,
                )
            )
        return summaries

    async def _term_page(
        self,
        stmt: Select,
        business_date: date,
        page: int,
        size: int,
    ) -> tuple[list[TermSummary], int]:
        page = max(page, 1)
        size = min(max(size, 1), 100)
        total = await self._count(stmt)
        terms = (
            await self.db.execute(stmt.offset((page - 1) * size).limit(size))
        ).scalars().all()
        term_ids = [term.id for term in terms]
        freezes = await self._active_freeze_by_term(None, term_ids, business_date)
        assignment_counts = await self._active_assignment_counts(None, term_ids, business_date)
        renewal_children = await self._renewal_children_by_parent(None, term_ids)
        return [
            self._term_summary(
                term,
                business_date,
                freeze=self._freeze_summary(freezes.get(term.id)),
                assignment_count=assignment_counts.get(term.id, 0),
                renewal_child_term_id=renewal_children.get(term.id),
            )
            for term in terms
        ], total

    async def _current_terms_by_series(
        self,
        org_id: uuid.UUID,
        series_ids: list[uuid.UUID],
        business_date: date,
    ) -> dict[uuid.UUID, SubscriptionTerm]:
        rows = (
            await self.db.execute(
                select(SubscriptionTerm)
                .where(
                    SubscriptionTerm.org_id == org_id,
                    SubscriptionTerm.series_id.in_(series_ids),
                    SubscriptionTerm.status.in_([SubscriptionTermStatus.active, SubscriptionTermStatus.scheduled]),
                    SubscriptionTerm.starts_on <= business_date,
                    SubscriptionTerm.effective_ends_on >= business_date,
                )
                .order_by(SubscriptionTerm.series_id, SubscriptionTerm.sequence_number.desc())
            )
        ).scalars().all()
        grouped: dict[uuid.UUID, list[SubscriptionTerm]] = defaultdict(list)
        for term in rows:
            grouped[term.series_id].append(term)

        result: dict[uuid.UUID, SubscriptionTerm] = {}
        for series_id, terms in grouped.items():
            if len(terms) > 1:
                raise MultipleCurrentTermsError(series_id, [term.id for term in terms])
            result[series_id] = terms[0]
        return result

    async def _scheduled_next_terms_by_series(
        self,
        org_id: uuid.UUID,
        series_ids: list[uuid.UUID],
        business_date: date,
    ) -> dict[uuid.UUID, SubscriptionTerm]:
        rows = (
            await self.db.execute(
                select(SubscriptionTerm)
                .where(
                    SubscriptionTerm.org_id == org_id,
                    SubscriptionTerm.series_id.in_(series_ids),
                    SubscriptionTerm.status.in_([SubscriptionTermStatus.active, SubscriptionTermStatus.scheduled]),
                    SubscriptionTerm.starts_on > business_date,
                )
                .order_by(SubscriptionTerm.series_id, SubscriptionTerm.starts_on)
            )
        ).scalars().all()
        result: dict[uuid.UUID, SubscriptionTerm] = {}
        for term in rows:
            result.setdefault(term.series_id, term)
        return result

    async def _previous_term_counts(
        self,
        org_id: uuid.UUID,
        series_ids: list[uuid.UUID],
        business_date: date,
    ) -> dict[uuid.UUID, int]:
        rows = (
            await self.db.execute(
                select(SubscriptionTerm.series_id, func.count(SubscriptionTerm.id))
                .where(
                    SubscriptionTerm.org_id == org_id,
                    SubscriptionTerm.series_id.in_(series_ids),
                    or_(
                        SubscriptionTerm.effective_ends_on < business_date,
                        SubscriptionTerm.status.in_(
                            [
                                SubscriptionTermStatus.expired,
                                SubscriptionTermStatus.cancelled,
                                SubscriptionTermStatus.terminated,
                                SubscriptionTermStatus.voided,
                            ]
                        ),
                    ),
                )
                .group_by(SubscriptionTerm.series_id)
            )
        ).all()
        return {series_id: count for series_id, count in rows}

    async def _active_freeze_by_term(
        self,
        org_id: uuid.UUID | None,
        term_ids: list[uuid.UUID],
        business_date: date,
    ) -> dict[uuid.UUID, SubscriptionFreeze]:
        if not term_ids:
            return {}
        stmt = select(SubscriptionFreeze).where(
            SubscriptionFreeze.term_id.in_(term_ids),
            SubscriptionFreeze.status == SubscriptionFreezeStatus.active,
            SubscriptionFreeze.requested_starts_on <= business_date,
            or_(SubscriptionFreeze.planned_ends_on.is_(None), SubscriptionFreeze.planned_ends_on >= business_date),
            or_(SubscriptionFreeze.actual_ended_on.is_(None), SubscriptionFreeze.actual_ended_on >= business_date),
        )
        if org_id:
            stmt = stmt.where(SubscriptionFreeze.org_id == org_id)
        rows = (await self.db.execute(stmt.order_by(SubscriptionFreeze.requested_starts_on.desc()))).scalars().all()
        result: dict[uuid.UUID, SubscriptionFreeze] = {}
        for freeze in rows:
            result.setdefault(freeze.term_id, freeze)
        return result

    async def _active_assignment_counts(
        self,
        org_id: uuid.UUID | None,
        term_ids: list[uuid.UUID],
        business_date: date,
    ) -> dict[uuid.UUID, int]:
        if not term_ids:
            return {}
        stmt = (
            select(SubscriptionSlotAssignment.term_id, func.count(SubscriptionSlotAssignment.id))
            .where(SubscriptionSlotAssignment.term_id.in_(term_ids), self._assignment_active_on(business_date))
            .group_by(SubscriptionSlotAssignment.term_id)
        )
        if org_id:
            stmt = stmt.where(SubscriptionSlotAssignment.org_id == org_id)
        rows = (await self.db.execute(stmt)).all()
        return {term_id: count for term_id, count in rows}

    async def _renewal_children_by_parent(
        self,
        org_id: uuid.UUID | None,
        term_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, uuid.UUID]:
        if not term_ids:
            return {}
        stmt = select(SubscriptionTerm.renewed_from_term_id, SubscriptionTerm.id).where(
            SubscriptionTerm.renewed_from_term_id.in_(term_ids)
        )
        if org_id:
            stmt = stmt.where(SubscriptionTerm.org_id == org_id)
        rows = (await self.db.execute(stmt.order_by(SubscriptionTerm.sequence_number))).all()
        result: dict[uuid.UUID, uuid.UUID] = {}
        for parent_id, child_id in rows:
            result.setdefault(parent_id, child_id)
        return result

    def _term_summary(
        self,
        term: SubscriptionTerm,
        business_date: date,
        *,
        freeze: FreezeSummary | None,
        assignment_count: int,
        renewal_child_term_id: uuid.UUID | None,
    ) -> TermSummary:
        derived_status = resolve_term_status(
            term.status,
            term.starts_on,
            term.effective_ends_on,
            business_date,
            has_active_freeze=is_freeze_active(freeze, business_date),
        )
        return TermSummary(
            id=term.id,
            branch_id=term.branch_id,
            term_code=term.term_code,
            sequence_number=term.sequence_number,
            plan_id=term.plan_id,
            plan_code=term.plan_code_snapshot,
            plan_name=term.plan_name_snapshot,
            price=term.final_amount,
            currency_code=term.currency_code,
            duration_value=term.duration_value_snapshot,
            duration_unit=term.duration_unit_snapshot.value,
            starts_on=term.starts_on,
            base_ends_on=term.base_ends_on,
            effective_ends_on=term.effective_ends_on,
            stored_status=term.status,
            derived_status=derived_status,
            source_type=term.source_type,
            renewed_from_term_id=term.renewed_from_term_id,
            renewal_child_term_id=renewal_child_term_id,
            capacity=term.capacity_snapshot,
            assignment_count=assignment_count,
            freeze=freeze,
        )

    def _freeze_summary(self, freeze: SubscriptionFreeze | None) -> FreezeSummary | None:
        if not freeze:
            return None
        return FreezeSummary(
            id=freeze.id,
            status=freeze.status,
            requested_starts_on=freeze.requested_starts_on,
            planned_ends_on=freeze.planned_ends_on,
            actual_ended_on=freeze.actual_ended_on,
            extension_days=freeze.extension_days,
            reason=freeze.reason,
        )

    def _member_brief(self, member: Member) -> MemberBrief:
        return MemberBrief(
            id=member.id,
            name=member.name,
            member_number=member.member_number,
            phone=member.phone,
        )

    def _branch_brief(self, branch: OrgBranch | None) -> BranchBrief | None:
        if not branch:
            return None
        return BranchBrief(
            id=branch.id,
            name=branch.branch_name,
            code=branch.branch_code,
            timezone=branch.timezone,
        )

    def _assignment_active_on(self, business_date: date):
        return and_(
            SubscriptionSlotAssignment.assignment_state == SubscriptionAssignmentStatus.active,
            SubscriptionSlotAssignment.effective_from <= business_date,
            or_(
                SubscriptionSlotAssignment.effective_until.is_(None),
                SubscriptionSlotAssignment.effective_until >= business_date,
            ),
        )

    async def _count(self, stmt: Select) -> int:
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        return int((await self.db.execute(count_stmt)).scalar_one())

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.platform_billing.domain.read_models import PlanVersionRead, ProductRead
from app.platform_billing.models.catalog import (
    PlatformFeatureDefinition,
    PlatformPlanEntitlement,
    PlatformPlanVersion,
    PlatformPrice,
    PlatformProduct,
)
from app.platform_billing.repositories.mappers import plan_version_to_read, product_to_read


class PlatformCatalogReadRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_active_products(self) -> list[ProductRead]:
        result = await self._session.execute(
            select(PlatformProduct)
            .where(PlatformProduct.status == "active")
            .order_by(PlatformProduct.code)
        )
        return [product_to_read(row) for row in result.scalars().all()]

    async def list_published_plan_versions(
        self,
        *,
        country_code: str | None = None,
        currency_code: str | None = None,
        now: datetime | None = None,
    ) -> list[PlanVersionRead]:
        query = (
            select(PlatformPlanVersion)
            .options(selectinload(PlatformPlanVersion.prices))
            .where(PlatformPlanVersion.status == "published")
            .order_by(PlatformPlanVersion.code)
        )
        if country_code or currency_code or now is not None:
            query = query.where(
                PlatformPlanVersion.id.in_(
                    self._available_price_plan_ids(
                        country_code=country_code,
                        currency_code=currency_code,
                        now=now,
                    )
                )
            )
        result = await self._session.execute(query)
        return [plan_version_to_read(row, include_prices=True) for row in result.scalars().unique().all()]

    async def get_published_plan_version(
        self,
        plan_version_id: uuid.UUID,
        *,
        country_code: str | None = None,
        currency_code: str | None = None,
        now: datetime | None = None,
    ) -> PlanVersionRead | None:
        query = (
            select(PlatformPlanVersion)
            .options(
                selectinload(PlatformPlanVersion.prices),
                selectinload(PlatformPlanVersion.entitlements)
                .selectinload(PlatformPlanEntitlement.feature_definition),
            )
            .where(
                PlatformPlanVersion.id == plan_version_id,
                PlatformPlanVersion.status == "published",
            )
        )
        if country_code or currency_code or now is not None:
            query = query.where(
                PlatformPlanVersion.id.in_(
                    self._available_price_plan_ids(
                        country_code=country_code,
                        currency_code=currency_code,
                        now=now,
                    )
                )
            )
        result = await self._session.execute(query)
        row = result.scalars().unique().one_or_none()
        if row is None:
            return None
        return plan_version_to_read(row, include_prices=True, include_entitlements=True)

    def _available_price_plan_ids(
        self,
        *,
        country_code: str | None,
        currency_code: str | None,
        now: datetime | None,
    ) -> Select[tuple[uuid.UUID]]:
        query = select(PlatformPrice.plan_version_id).where(PlatformPrice.status == "active")
        if country_code is not None:
            query = query.where(or_(PlatformPrice.country_code.is_(None), PlatformPrice.country_code == country_code.upper()))
        if currency_code is not None:
            query = query.where(PlatformPrice.currency_code == currency_code.upper())
        if now is not None:
            query = query.where(
                PlatformPrice.valid_from <= now,
                or_(PlatformPrice.valid_until.is_(None), PlatformPrice.valid_until > now),
            )
        return query

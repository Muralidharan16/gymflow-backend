from __future__ import annotations

import asyncio
import hashlib
import math
import struct
import time
import uuid
from collections.abc import Awaitable, Callable
from numbers import Real

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.synthetic_organizations import SyntheticOrganizationLockContentionError
from app.models.organization import Organization
from app.models.organization_creation_idempotency import OrganizationCreationIdempotency


SYNTHETIC_ORGANIZATION_LOCK_TIMEOUT_SECONDS = 1.0
SYNTHETIC_ORGANIZATION_LOCK_POLL_INTERVAL_SECONDS = 0.01


class SyntheticOrganizationRepository:
    def __init__(
        self,
        session: AsyncSession,
        *,
        lock_timeout_seconds: float = SYNTHETIC_ORGANIZATION_LOCK_TIMEOUT_SECONDS,
        lock_poll_interval_seconds: float = SYNTHETIC_ORGANIZATION_LOCK_POLL_INTERVAL_SECONDS,
        lock_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        lock_timeout_seconds = _validate_lock_timing(
            "synthetic organization lock timeout",
            lock_timeout_seconds,
            maximum=30.0,
        )
        lock_poll_interval_seconds = _validate_lock_timing(
            "synthetic organization lock poll interval",
            lock_poll_interval_seconds,
            maximum=lock_timeout_seconds,
        )
        self._session = session
        self._lock_timeout_seconds = lock_timeout_seconds
        self._lock_poll_interval_seconds = lock_poll_interval_seconds
        self._lock_sleep = lock_sleep
        self._monotonic = monotonic

    async def acquire_idempotency_lock(self, idempotency_key: str) -> None:
        await self._acquire_lock(f"org-create:idempotency:{idempotency_key}")

    async def acquire_slug_lock(self, slug: str) -> None:
        await self._acquire_lock(f"org-create:slug:{slug}")

    async def get_evidence(self, *, operation: str, idempotency_key: str) -> OrganizationCreationIdempotency | None:
        # Callers must hold the idempotency advisory lock before relying on this
        # immutable evidence read for create/replay serialization.
        statement = select(OrganizationCreationIdempotency).where(
            OrganizationCreationIdempotency.operation == operation,
            OrganizationCreationIdempotency.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_organization_by_slug(self, slug: str, *, for_update: bool = False) -> Organization | None:
        statement = select(Organization).where(Organization.slug == slug)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_organization_by_id(self, organization_id: uuid.UUID, *, for_update: bool = False) -> Organization | None:
        statement = select(Organization).where(Organization.id == organization_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_replay_organization_by_id(self, organization_id: uuid.UUID):
        """Read only the canonical fields required to validate an idempotent replay.

        The idempotency advisory lock serializes callers using the same immutable
        evidence key. Replay does not mutate the organization row, so taking a
        row-level ``FOR UPDATE`` lock would unnecessarily require database UPDATE
        privilege for a read-only integrity check.
        """
        statement = select(
            Organization.id,
            Organization.name,
            Organization.slug,
            Organization.tier,
            Organization.business_type,
            Organization.is_active,
            Organization.max_branches,
            Organization.default_currency_code,
            Organization.description,
            Organization.tagline,
        ).where(Organization.id == organization_id)
        result = await self._session.execute(statement)
        return result.one_or_none()

    async def insert_organization(
        self,
        *,
        name: str,
        slug: str,
        tier,
        business_type: str,
        is_active: bool,
        default_currency_code: str,
        max_branches: int,
        description: str,
        tagline: str | None,
    ) -> Organization:
        organization = Organization(
            name=name,
            slug=slug,
            tier=tier,
            business_type=business_type,
            is_active=is_active,
            default_currency_code=default_currency_code,
            max_branches=max_branches,
            description=description,
            tagline=tagline,
        )
        self._session.add(organization)
        await self._session.flush()
        return organization

    async def insert_evidence(
        self,
        *,
        operation: str,
        idempotency_key: str,
        request_hash_sha256: str,
        canonicalization_version: int,
        organization_id: uuid.UUID,
        trusted_source: str,
    ) -> OrganizationCreationIdempotency:
        evidence = OrganizationCreationIdempotency(
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash_sha256=request_hash_sha256,
            canonicalization_version=canonicalization_version,
            organization_id=organization_id,
            trusted_source=trusted_source,
        )
        self._session.add(evidence)
        await self._session.flush()
        return evidence

    async def _acquire_lock(self, lock_name: str) -> None:
        lock_key = synthetic_organization_advisory_lock_key(lock_name)
        deadline = self._monotonic() + self._lock_timeout_seconds
        while True:
            result = await self._session.execute(
                text("SELECT pg_catalog.pg_try_advisory_xact_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
            if result.scalar_one() is True:
                return

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise SyntheticOrganizationLockContentionError()
            await self._lock_sleep(min(self._lock_poll_interval_seconds, remaining))


def _validate_lock_timing(name: str, value: object, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} is invalid")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{name} is invalid")
    if normalized <= 0 or normalized > maximum:
        raise ValueError(f"{name} is invalid")
    return normalized


def synthetic_organization_advisory_lock_key(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return struct.unpack(">q", digest[:8])[0]

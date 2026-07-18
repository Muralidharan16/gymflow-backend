from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.domain.synthetic_organizations import (
    SYNTHETIC_ORGANIZATION_CANONICALIZATION_VERSION,
    SYNTHETIC_ORGANIZATION_CURRENCY,
    SYNTHETIC_ORGANIZATION_DESCRIPTION,
    SYNTHETIC_ORGANIZATION_MAX_BRANCHES,
    SYNTHETIC_ORGANIZATION_OPERATION,
    SYNTHETIC_ORGANIZATION_TIER,
    SYNTHETIC_ORGANIZATION_TRUSTED_SOURCE,
    SyntheticOrganizationCreationCommand,
    SyntheticOrganizationCreationResult,
    SyntheticOrganizationError,
    canonical_hash,
    canonical_organization_payload,
    canonical_payload_from_organization,
    normalize_command,
)
from app.models.enums import OrgTier
from app.repositories.synthetic_organizations import SyntheticOrganizationRepository


_ALLOWED_ENVIRONMENTS = {"development", "dev", "test", "testing", "local"}
_PRODUCTION_ENVIRONMENTS = {"production", "prod", "live"}


class SyntheticOrganizationCreationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        environment: str | None = None,
        lock_timeout_seconds: float | None = None,
        lock_poll_interval_seconds: float | None = None,
        lock_sleep: Callable[[float], Awaitable[None]] | None = None,
    ):
        self._session = session
        repo_kwargs = {}
        if lock_timeout_seconds is not None:
            repo_kwargs["lock_timeout_seconds"] = lock_timeout_seconds
        if lock_poll_interval_seconds is not None:
            repo_kwargs["lock_poll_interval_seconds"] = lock_poll_interval_seconds
        if lock_sleep is not None:
            repo_kwargs["lock_sleep"] = lock_sleep
        self._repo = SyntheticOrganizationRepository(session, **repo_kwargs)
        self._environment = settings.ENVIRONMENT if environment is None else environment

    async def create_synthetic_organization(
        self,
        command: SyntheticOrganizationCreationCommand,
    ) -> SyntheticOrganizationCreationResult:
        _validate_environment(self._environment)
        normalized = normalize_command(command)
        payload = canonical_organization_payload(normalized)
        request_hash = canonical_hash(payload)

        async with self._session.begin_nested():
            await self._repo.acquire_idempotency_lock(normalized["idempotency_key"])
            evidence = await self._repo.get_evidence(
                operation=SYNTHETIC_ORGANIZATION_OPERATION,
                idempotency_key=normalized["idempotency_key"],
            )
            if evidence is not None:
                return await self._replay_or_conflict(evidence, request_hash)

            await self._repo.acquire_slug_lock(normalized["slug"])
            evidence = await self._repo.get_evidence(
                operation=SYNTHETIC_ORGANIZATION_OPERATION,
                idempotency_key=normalized["idempotency_key"],
            )
            if evidence is not None:
                return await self._replay_or_conflict(evidence, request_hash)

            existing = await self._repo.get_organization_by_slug(normalized["slug"], for_update=True)
            if existing is not None:
                raise SyntheticOrganizationError(
                    "SYNTHETIC_ORG_DUPLICATE_IDENTITY",
                    "Synthetic organization identity already exists.",
                )

            organization = await self._repo.insert_organization(
                name=normalized["name"],
                slug=normalized["slug"],
                tier=OrgTier.basic,
                business_type=normalized["business_type"],
                is_active=True,
                default_currency_code=SYNTHETIC_ORGANIZATION_CURRENCY,
                max_branches=SYNTHETIC_ORGANIZATION_MAX_BRANCHES,
                description=SYNTHETIC_ORGANIZATION_DESCRIPTION,
                tagline=normalized["tagline"],
            )
            await self._repo.insert_evidence(
                operation=SYNTHETIC_ORGANIZATION_OPERATION,
                idempotency_key=normalized["idempotency_key"],
                request_hash_sha256=request_hash,
                canonicalization_version=SYNTHETIC_ORGANIZATION_CANONICALIZATION_VERSION,
                organization_id=organization.id,
                trusted_source=SYNTHETIC_ORGANIZATION_TRUSTED_SOURCE,
            )
            return _result(organization, replayed=False)

    async def _replay_or_conflict(self, evidence, incoming_hash: str) -> SyntheticOrganizationCreationResult:
        if evidence.canonicalization_version != SYNTHETIC_ORGANIZATION_CANONICALIZATION_VERSION:
            raise SyntheticOrganizationError(
                "SYNTHETIC_ORG_UNSUPPORTED_CANONICAL_VERSION",
                "Synthetic organization replay version is unsupported.",
            )
        if evidence.request_hash_sha256 != incoming_hash:
            raise SyntheticOrganizationError(
                "SYNTHETIC_ORG_IDEMPOTENCY_CONFLICT",
                "Synthetic organization idempotency key was used with a different request.",
            )
        organization = await self._repo.get_organization_by_id(evidence.organization_id, for_update=True)
        if organization is None or not organization.is_active:
            raise SyntheticOrganizationError(
                "SYNTHETIC_ORG_REPLAY_INTEGRITY_CONFLICT",
                "Synthetic organization replay integrity check failed.",
            )
        persisted_hash = canonical_hash(canonical_payload_from_organization(organization))
        if persisted_hash != evidence.request_hash_sha256:
            raise SyntheticOrganizationError(
                "SYNTHETIC_ORG_REPLAY_INTEGRITY_CONFLICT",
                "Synthetic organization replay integrity check failed.",
            )
        return _result(organization, replayed=True)


def _validate_environment(value: str | None) -> None:
    normalized = str(value or "").strip().lower()
    if normalized in _PRODUCTION_ENVIRONMENTS:
        raise SyntheticOrganizationError(
            "SYNTHETIC_ORG_PRODUCTION_REJECTED",
            "Synthetic organization creation is not available in production.",
        )
    if normalized not in _ALLOWED_ENVIRONMENTS:
        raise SyntheticOrganizationError(
            "SYNTHETIC_ORG_ENVIRONMENT_REJECTED",
            "Synthetic organization creation requires an approved non-production environment.",
        )


def _result(organization, *, replayed: bool) -> SyntheticOrganizationCreationResult:
    return SyntheticOrganizationCreationResult(
        organization_id=uuid.UUID(str(organization.id)),
        name=str(organization.name),
        slug=str(organization.slug),
        is_active=bool(organization.is_active),
        currency=str(organization.default_currency_code),
        replayed=replayed,
    )

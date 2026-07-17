from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any


SYNTHETIC_ORGANIZATION_OPERATION = "synthetic_organization_create"
SYNTHETIC_ORGANIZATION_TRUSTED_SOURCE = "finance_razorpay_test_precondition"
SYNTHETIC_ORGANIZATION_PURPOSE = "razorpay_test_smoke_precondition"
SYNTHETIC_ORGANIZATION_DESCRIPTION = "INTERNAL RAZORPAY TEST SMOKE ORGANIZATION - NO REAL CUSTOMER"
SYNTHETIC_ORGANIZATION_BUSINESS_TYPE = "synthetic_test"
SYNTHETIC_ORGANIZATION_TIER = "basic"
SYNTHETIC_ORGANIZATION_CURRENCY = "INR"
SYNTHETIC_ORGANIZATION_MAX_BRANCHES = 1
SYNTHETIC_ORGANIZATION_CANONICALIZATION_VERSION = 1

_CONTROL_CHARS = re.compile(r"[\x00-]")
_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,118}[a-z0-9])?$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[a-z0-9:_-]{1,200}$")
_IDEMPOTENCY_PREFIX = "organization-create:synthetic:test:"
_TEST_WORD_PATTERN = re.compile(r"(^|[^a-z0-9])(test|sandbox)([^a-z0-9]|$)", re.IGNORECASE)


@dataclass(frozen=True)
class SyntheticOrganizationCreationCommand:
    name: str
    slug: str
    idempotency_key: str
    synthetic_mode: bool
    trusted_source: str


@dataclass(frozen=True)
class SyntheticOrganizationCreationResult:
    organization_id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    currency: str
    replayed: bool


@dataclass(frozen=True)
class SyntheticOrganizationError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class SyntheticOrganizationLockContentionError(SyntheticOrganizationError):
    def __init__(self) -> None:
        super().__init__(
            "SYNTHETIC_ORG_LOCK_CONTENTION",
            "Synthetic organization creation is currently contended.",
        )


def normalize_command(command: SyntheticOrganizationCreationCommand) -> dict[str, Any]:
    if not command.synthetic_mode:
        raise SyntheticOrganizationError("SYNTHETIC_ORG_MODE_REQUIRED", "Synthetic organization mode is required.")
    if command.trusted_source != SYNTHETIC_ORGANIZATION_TRUSTED_SOURCE:
        raise SyntheticOrganizationError("SYNTHETIC_ORG_SOURCE_REJECTED", "Synthetic organization source is not trusted.")

    name = _normalize_name(command.name)
    slug = _normalize_slug(command.slug)
    idempotency_key = _normalize_idempotency_key(command.idempotency_key)
    return {
        "name": name,
        "slug": slug,
        "idempotency_key": idempotency_key,
        "trusted_source": SYNTHETIC_ORGANIZATION_TRUSTED_SOURCE,
        "operation": SYNTHETIC_ORGANIZATION_OPERATION,
        "canonicalization_version": SYNTHETIC_ORGANIZATION_CANONICALIZATION_VERSION,
        "tier": SYNTHETIC_ORGANIZATION_TIER,
        "business_type": SYNTHETIC_ORGANIZATION_BUSINESS_TYPE,
        "is_active": True,
        "default_currency_code": SYNTHETIC_ORGANIZATION_CURRENCY,
        "max_branches": SYNTHETIC_ORGANIZATION_MAX_BRANCHES,
        "description": SYNTHETIC_ORGANIZATION_DESCRIPTION,
        "tagline": None,
        "synthetic_purpose": SYNTHETIC_ORGANIZATION_PURPOSE,
    }


def canonical_organization_payload(normalized: dict[str, Any]) -> dict[str, Any]:
    return {
        "business_type": normalized["business_type"],
        "default_currency_code": normalized["default_currency_code"],
        "description": normalized["description"],
        "is_active": normalized["is_active"],
        "max_branches": normalized["max_branches"],
        "name": normalized["name"],
        "slug": normalized["slug"],
        "synthetic_purpose": normalized["synthetic_purpose"],
        "tagline": normalized["tagline"],
        "tier": normalized["tier"],
        "trusted_source": normalized["trusted_source"],
    }


def canonical_payload_from_organization(organization) -> dict[str, Any]:
    return canonical_organization_payload(
        {
            "name": _collapse_text(organization.name),
            "slug": _normalize_slug(organization.slug),
            "trusted_source": SYNTHETIC_ORGANIZATION_TRUSTED_SOURCE,
            "tier": str(getattr(organization.tier, "value", organization.tier)),
            "business_type": organization.business_type,
            "is_active": bool(organization.is_active),
            "default_currency_code": str(organization.default_currency_code or "").upper(),
            "max_branches": int(organization.max_branches),
            "description": _collapse_text(organization.description),
            "tagline": _normalize_optional_text(organization.tagline),
            "synthetic_purpose": SYNTHETIC_ORGANIZATION_PURPOSE,
        }
    )


def canonical_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > 4096:
        raise SyntheticOrganizationError("SYNTHETIC_ORG_CANONICAL_TOO_LARGE", "Synthetic organization request is too large.")
    return hashlib.sha256(raw).hexdigest()


def _normalize_name(value: str) -> str:
    normalized = _collapse_text(value)
    if not normalized or len(normalized) > 100 or _CONTROL_CHARS.search(str(value or "")):
        raise SyntheticOrganizationError("SYNTHETIC_ORG_NAME_INVALID", "Synthetic organization name is invalid.")
    if not _TEST_WORD_PATTERN.search(normalized):
        raise SyntheticOrganizationError("SYNTHETIC_ORG_NAME_UNSAFE", "Synthetic organization name must be unmistakably test-only.")
    return normalized


def _normalize_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if not normalized or len(normalized) > 120 or _CONTROL_CHARS.search(str(value or "")):
        raise SyntheticOrganizationError("SYNTHETIC_ORG_SLUG_INVALID", "Synthetic organization slug is invalid.")
    if not _SLUG_PATTERN.fullmatch(normalized):
        raise SyntheticOrganizationError("SYNTHETIC_ORG_SLUG_INVALID", "Synthetic organization slug is invalid.")
    if "test" not in normalized and "sandbox" not in normalized:
        raise SyntheticOrganizationError("SYNTHETIC_ORG_SLUG_UNSAFE", "Synthetic organization slug must be test-only.")
    return normalized


def _normalize_idempotency_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized):
        raise SyntheticOrganizationError("SYNTHETIC_ORG_IDEMPOTENCY_INVALID", "Synthetic organization idempotency key is invalid.")
    if not normalized.startswith(_IDEMPOTENCY_PREFIX):
        raise SyntheticOrganizationError("SYNTHETIC_ORG_IDEMPOTENCY_INVALID", "Synthetic organization idempotency key is invalid.")
    return normalized


def _collapse_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.strip().split())


def _normalize_optional_text(value: str | None) -> str | None:
    normalized = _collapse_text(value)
    return normalized or None

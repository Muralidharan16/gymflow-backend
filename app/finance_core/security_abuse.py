from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from app.core.deps import Staff, require_org_admin
from app.core.redis import redis_client
from app.core.security import verify_finance_csrf_token, verify_token
from app.observability.runtime_metrics import runtime_metrics


logger = logging.getLogger("doers.finance.security")

RECENT_AUTH_WINDOW_SECONDS = 10 * 60
MAX_AUTH_CLOCK_SKEW_SECONDS = 60
_REASON_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_.:-]{2,79}$")
FORBIDDEN_PAYMENT_SECRET_FIELDS = frozenset(
    {
        "card_number",
        "card_pan",
        "primary_account_number",
        "cvv",
        "cvc",
        "card_security_code",
        "upi_pin",
        "pin",
    }
)
SECURE_EXPORT_MAX_ROWS = 5_000


@dataclass(frozen=True)
class FinanceSecurityContext:
    actor_id: uuid.UUID
    organization_id: uuid.UUID
    role: str
    jti: str
    family_id: str | None
    authenticated_at: int
    token_transport: str


def _deny(code: str, message: str, http_status: int) -> None:
    raise HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message},
    )


def _request_access_token(request: Request) -> tuple[str, str]:
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token:
            return token, "bearer"

    token = request.cookies.get("access_token")
    if token:
        return token, "cookie"

    _deny(
        "FINANCE_SECURITY_AUTH_REQUIRED",
        "A current authenticated session is required.",
        status.HTTP_401_UNAUTHORIZED,
    )


def _validated_identity(payload: dict, staff: Staff) -> tuple[uuid.UUID, uuid.UUID]:
    try:
        actor_id = uuid.UUID(str(payload.get("sub")))
        organization_id = uuid.UUID(str(payload.get("org_id")))
    except (TypeError, ValueError, AttributeError):
        _deny(
            "FINANCE_SECURITY_IDENTITY_INVALID",
            "Finance security identity is invalid.",
            status.HTTP_401_UNAUTHORIZED,
        )

    token_role = str(payload.get("role") or "").strip().lower()
    staff_role = str(staff.role or "").strip().lower()
    if (
        actor_id != staff.id
        or organization_id != staff.org_id
        or token_role != staff_role
    ):
        security_event(
            "finance.security.identity_mismatch",
            actor_id=staff.id,
            organization_id=staff.org_id,
        )
        _deny(
            "FINANCE_SECURITY_IDENTITY_MISMATCH",
            "Finance security identity no longer matches the active principal.",
            status.HTTP_401_UNAUTHORIZED,
        )

    return actor_id, organization_id


def _validated_recent_auth(payload: dict, *, now_epoch: int | None = None) -> int:
    auth_time = payload.get("auth_time")
    if isinstance(auth_time, bool) or not isinstance(auth_time, int) or auth_time < 0:
        _deny(
            "FINANCE_STEP_UP_REQUIRED",
            "Recent authentication is required for this finance action.",
            status.HTTP_401_UNAUTHORIZED,
        )

    now = int(time.time()) if now_epoch is None else int(now_epoch)
    age = now - auth_time
    if age < -MAX_AUTH_CLOCK_SKEW_SECONDS or age > RECENT_AUTH_WINDOW_SECONDS:
        _deny(
            "FINANCE_STEP_UP_REQUIRED",
            "Recent authentication is required for this finance action.",
            status.HTTP_401_UNAUTHORIZED,
        )
    return auth_time


async def _require_revocation_evidence(
    *,
    jti: str,
    family_id: str | None,
    actor_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> None:
    """Fail closed for privileged finance when revocation truth is unavailable."""
    try:
        await redis_client.ping()
        if await redis_client.get(f"blacklist:{jti}"):
            _deny(
                "FINANCE_SESSION_REVOKED",
                "The authenticated session has been revoked.",
                status.HTTP_401_UNAUTHORIZED,
            )
        if family_id and await redis_client.get(f"family_revoked:{family_id}"):
            _deny(
                "FINANCE_SESSION_REVOKED",
                "The authenticated session family has been revoked.",
                status.HTTP_401_UNAUTHORIZED,
            )
    except HTTPException:
        raise
    except Exception:
        security_event(
            "finance.security.revocation_backend_unavailable",
            actor_id=actor_id,
            organization_id=organization_id,
            severity="warning",
        )
        _deny(
            "FINANCE_SECURITY_DEPENDENCY_UNAVAILABLE",
            "Finance security verification is temporarily unavailable.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )


async def finance_high_risk_actor_dependency(
    request: Request,
    staff: Staff = Depends(require_org_admin),
) -> FinanceSecurityContext:
    """Authorize a privileged monetary write using only trusted session state.

    Browser-cookie requests additionally need a double-submit CSRF proof bound to
    the signed access-token JTI. Bearer callers are not CSRF-vulnerable, but they
    still require recent authentication and fail-closed revocation evidence.
    """
    access_token, transport = _request_access_token(request)
    try:
        payload = verify_token(access_token, expected_type="access")
    except Exception:
        _deny(
            "FINANCE_SECURITY_AUTH_INVALID",
            "The authenticated session is invalid or expired.",
            status.HTTP_401_UNAUTHORIZED,
        )

    actor_id, organization_id = _validated_identity(payload, staff)
    auth_time = _validated_recent_auth(payload)

    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        _deny(
            "FINANCE_SECURITY_SESSION_INVALID",
            "The finance session identifier is invalid.",
            status.HTTP_401_UNAUTHORIZED,
        )
    family_id = payload.get("f_id")
    if family_id is not None and (not isinstance(family_id, str) or not family_id):
        _deny(
            "FINANCE_SECURITY_SESSION_INVALID",
            "The finance session family is invalid.",
            status.HTTP_401_UNAUTHORIZED,
        )

    if transport == "cookie":
        cookie_proof = request.cookies.get("finance_csrf")
        header_proof = request.headers.get("X-CSRF-Token")
        if (
            not cookie_proof
            or not header_proof
            or cookie_proof != header_proof
            or not verify_finance_csrf_token(access_token, header_proof)
        ):
            security_event(
                "finance.security.csrf_rejected",
                actor_id=actor_id,
                organization_id=organization_id,
                severity="warning",
            )
            _deny(
                "FINANCE_CSRF_REJECTED",
                "Finance request CSRF verification failed.",
                status.HTTP_403_FORBIDDEN,
            )

    await _require_revocation_evidence(
        jti=jti,
        family_id=family_id,
        actor_id=actor_id,
        organization_id=organization_id,
    )

    return FinanceSecurityContext(
        actor_id=actor_id,
        organization_id=organization_id,
        role=str(staff.role).lower(),
        jti=jti,
        family_id=family_id,
        authenticated_at=auth_time,
        token_transport=transport,
    )


def validate_admin_reason_code(value: str | None) -> str:
    normalized = value.strip().upper() if isinstance(value, str) else ""
    if not _REASON_CODE_PATTERN.fullmatch(normalized):
        _deny(
            "FINANCE_REASON_CODE_REQUIRED",
            "A bounded administrative reason code is required.",
            status.HTTP_400_BAD_REQUEST,
        )
    return normalized


def enforce_maker_checker(*, maker_actor_id: uuid.UUID, checker_actor_id: uuid.UUID) -> None:
    if maker_actor_id == checker_actor_id:
        _deny(
            "FINANCE_MAKER_CHECKER_REQUIRED",
            "The actor who prepared the monetary action cannot approve it.",
            status.HTTP_409_CONFLICT,
        )


def enforce_secure_export(*, requested_rows: int, fields: set[str]) -> None:
    if requested_rows < 0 or requested_rows > SECURE_EXPORT_MAX_ROWS:
        _deny(
            "FINANCE_EXPORT_LIMIT_EXCEEDED",
            "Finance export size exceeds the secure export limit.",
            status.HTTP_400_BAD_REQUEST,
        )
    normalized = {field.strip().lower() for field in fields}
    if normalized & FORBIDDEN_PAYMENT_SECRET_FIELDS:
        _deny(
            "FINANCE_EXPORT_SENSITIVE_FIELD_FORBIDDEN",
            "Sensitive payment-authentication data cannot be exported.",
            status.HTTP_403_FORBIDDEN,
        )


def reject_payment_secret_fields(fields: set[str]) -> None:
    normalized = {field.strip().lower() for field in fields}
    if normalized & FORBIDDEN_PAYMENT_SECRET_FIELDS:
        _deny(
            "FINANCE_PAYMENT_SECRET_STORAGE_FORBIDDEN",
            "Payment-card PAN, CVV/CVC and payment PIN material must never be stored.",
            status.HTTP_400_BAD_REQUEST,
        )


def security_event(
    event: str,
    *,
    actor_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    severity: str = "info",
    reason_code: str | None = None,
) -> None:
    # Never include access tokens, cookies, signatures, raw provider payloads,
    # PAN/CVV/PIN material, or provider secrets in this record.
    runtime_metrics().finance_security_event(
        event=event,
        severity=severity,
    )
    log = logger.warning if severity in {"warning", "critical"} else logger.info
    log(
        "Finance security event",
        extra={
            "event": event,
            "actor_id": str(actor_id) if actor_id else None,
            "organization_id": str(organization_id) if organization_id else None,
            "reason_code": reason_code,
        },
    )

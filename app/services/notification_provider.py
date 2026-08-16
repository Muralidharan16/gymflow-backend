"""Evidence-backed P4C notification provider adapters.

Provider submission is deliberately non-terminal.  The Resend adapter returns a
provider reference plus hashed acknowledgement evidence; final delivery is
established later by signed webhook or reconciliation evidence.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

import httpx


_PROVIDER = "resend"
_ALLOWED_OUTCOMES = {
    "provider_accepted_nonterminal",
    "permanent_rejection",
    "retryable_failure",
    "ambiguous_outcome",
}
_TERMINAL_LAST_EVENTS = {
    "delivered",
    "bounced",
    "complained",
    "failed",
    "suppressed",
    "opened",
    "clicked",
}
_NONTERMINAL_LAST_EVENTS = {"sent", "delivery_delayed", "queued", "scheduled"}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256(_canonical_json(value))


@dataclass(frozen=True)
class NotificationProviderEvidence:
    provider_code: str
    provider_reference_id: str
    request_sha256: str
    provider_evidence_sha256: str


@dataclass(frozen=True)
class NotificationReconciliationEvidence:
    provider_code: str
    provider_reference_id: str
    last_event: str
    provider_evidence_sha256: str


class NotificationProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        outcome: str,
        error_code: str,
        request_sha256: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        if outcome not in _ALLOWED_OUTCOMES:
            raise ValueError(f"unsupported notification provider outcome: {outcome!r}")
        super().__init__(message)
        self.outcome = outcome
        self.error_code = error_code[:160]
        self.request_sha256 = request_sha256
        self.retry_after_seconds = retry_after_seconds


class ResendEmailProvider:
    """Resend REST adapter with deterministic logical idempotency."""

    def __init__(
        self,
        *,
        mode: str,
        api_key: str,
        from_email: str,
        base_url: str = "https://api.resend.com",
        timeout_seconds: float = 8.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.mode = mode.strip().lower()
        self.api_key = api_key.strip()
        self.from_email = from_email.strip()
        self.base_url = base_url.strip().rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self._client = client

    @classmethod
    def from_settings(cls, config: Any | None = None) -> "ResendEmailProvider":
        if config is None:
            from app.core.config import settings as config
        return cls(
            mode=str(getattr(config, "NOTIFICATION_EMAIL_PROVIDER_MODE", "disabled")),
            api_key=str(getattr(config, "P4C_RESEND_API_KEY", "")),
            from_email=str(getattr(config, "NOTIFICATION_EMAIL_FROM", getattr(config, "MAIL_FROM", ""))),
            base_url=str(getattr(config, "RESEND_API_BASE_URL", "https://api.resend.com")),
            timeout_seconds=float(getattr(config, "NOTIFICATION_PROVIDER_TIMEOUT_SECONDS", 8.0)),
        )

    def _validate_configuration(self, request_sha256: str) -> None:
        if self.mode != _PROVIDER:
            raise NotificationProviderError(
                "P4C email provider is disabled",
                outcome="permanent_rejection",
                error_code="provider_disabled",
                request_sha256=request_sha256,
            )
        if not self.api_key or not self.from_email:
            raise NotificationProviderError(
                "P4C Resend provider configuration is incomplete",
                outcome="permanent_rejection",
                error_code="provider_configuration_missing",
                request_sha256=request_sha256,
            )
        if not self.base_url.startswith("https://"):
            raise NotificationProviderError(
                "P4C Resend base URL must use HTTPS",
                outcome="permanent_rejection",
                error_code="provider_url_invalid",
                request_sha256=request_sha256,
            )
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise NotificationProviderError(
                "P4C notification provider timeout must be in (0, 60]",
                outcome="permanent_rejection",
                error_code="provider_timeout_invalid",
                request_sha256=request_sha256,
            )

    @staticmethod
    def _render_lifecycle_message(
        *,
        member_name: str,
        template_data: Mapping[str, Any],
    ) -> tuple[str, str, str]:
        branch_name = str(template_data.get("branch_name") or "your branch")
        from_status = str(template_data.get("from_status") or "unknown")
        to_status = str(template_data.get("to_status") or "unknown")
        safe_name = html.escape(member_name or "Member")
        safe_branch = html.escape(branch_name)
        safe_from = html.escape(from_status.replace("_", " "))
        safe_to = html.escape(to_status.replace("_", " "))
        subject = f"{branch_name} status update"
        plain = (
            f"Hi {member_name or 'Member'},\n\n"
            f"{branch_name} changed status from {from_status.replace('_', ' ')} "
            f"to {to_status.replace('_', ' ')}.\n\n— The Doers Team"
        )
        body = (
            "<p>Hi " + safe_name + ",</p>"
            "<p><strong>" + safe_branch + "</strong> changed status from "
            "<strong>" + safe_from + "</strong> to <strong>" + safe_to + "</strong>.</p>"
            "<p>— The Doers Team</p>"
        )
        return subject, plain, body

    @classmethod
    def request_payload(
        cls,
        *,
        destination: str,
        member_name: str,
        template_key: str,
        template_data: Mapping[str, Any],
        from_email: str,
    ) -> dict[str, Any]:
        if template_key != "branch_lifecycle_status_changed":
            raise ValueError(f"unsupported P4C notification template: {template_key!r}")
        subject, plain, body = cls._render_lifecycle_message(
            member_name=member_name,
            template_data=template_data,
        )
        return {
            "from": from_email,
            "to": [destination],
            "subject": subject,
            "text": plain,
            "html": body,
        }

    @classmethod
    def request_sha256(
        cls,
        *,
        destination: str,
        member_name: str,
        template_key: str,
        template_data: Mapping[str, Any],
        from_email: str,
    ) -> str:
        return _sha256_json(
            cls.request_payload(
                destination=destination,
                member_name=member_name,
                template_key=template_key,
                template_data=template_data,
                from_email=from_email,
            )
        )

    async def send(
        self,
        *,
        destination: str,
        member_name: str,
        template_key: str,
        template_data: Mapping[str, Any],
        idempotency_key: str,
    ) -> NotificationProviderEvidence:
        payload = self.request_payload(
            destination=destination,
            member_name=member_name,
            template_key=template_key,
            template_data=template_data,
            from_email=self.from_email,
        )
        request_sha256 = _sha256_json(payload)
        self._validate_configuration(request_sha256)
        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise NotificationProviderError(
                "P4C notification idempotency key is invalid",
                outcome="permanent_rejection",
                error_code="idempotency_key_invalid",
                request_sha256=request_sha256,
            )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
            )
        try:
            try:
                response = await client.post("/emails", headers=headers, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise NotificationProviderError(
                    "Resend submission has an unknown commit point",
                    outcome="ambiguous_outcome",
                    error_code="provider_transport_ambiguous",
                    request_sha256=request_sha256,
                ) from exc
        finally:
            if owns_client:
                await client.aclose()

        body = self._safe_json(response)
        evidence_hash = _sha256_json({"status": response.status_code, "body": body})
        if 200 <= response.status_code < 300:
            reference = body.get("id") if isinstance(body, dict) else None
            if not isinstance(reference, str) or not reference.strip():
                raise NotificationProviderError(
                    "Resend accepted submission without a provider email id",
                    outcome="ambiguous_outcome",
                    error_code="provider_reference_missing",
                    request_sha256=request_sha256,
                )
            return NotificationProviderEvidence(
                provider_code=_PROVIDER,
                provider_reference_id=reference,
                request_sha256=request_sha256,
                provider_evidence_sha256=evidence_hash,
            )

        retry_after = self._retry_after_seconds(response)
        if response.status_code == 429 or response.status_code >= 500:
            outcome = "retryable_failure"
        elif response.status_code == 409:
            # Same logical idempotency key may be concurrently in-flight or
            # already committed; retrying the same key is safe but the current
            # call cannot prove whether an external effect already exists.
            outcome = "ambiguous_outcome"
        else:
            outcome = "permanent_rejection"
        raise NotificationProviderError(
            f"Resend submission returned HTTP {response.status_code}",
            outcome=outcome,
            error_code=f"provider_http_{response.status_code}",
            request_sha256=request_sha256,
            retry_after_seconds=retry_after,
        )

    async def reconcile(self, provider_reference_id: str) -> NotificationReconciliationEvidence:
        if not provider_reference_id.strip():
            raise ValueError("provider_reference_id is required")
        request_sha256 = _sha256_json({"provider": _PROVIDER, "reference": provider_reference_id, "op": "get"})
        self._validate_configuration(request_sha256)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
            )
        try:
            try:
                response = await client.get(f"/emails/{provider_reference_id}", headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise NotificationProviderError(
                    "Resend reconciliation request failed",
                    outcome="retryable_failure",
                    error_code="reconciliation_transport_failure",
                    request_sha256=request_sha256,
                ) from exc
        finally:
            if owns_client:
                await client.aclose()
        body = self._safe_json(response)
        if response.status_code != 200 or not isinstance(body, dict):
            outcome = "retryable_failure" if response.status_code in {404, 429} or response.status_code >= 500 else "permanent_rejection"
            raise NotificationProviderError(
                f"Resend reconciliation returned HTTP {response.status_code}",
                outcome=outcome,
                error_code=f"reconciliation_http_{response.status_code}",
                request_sha256=request_sha256,
                retry_after_seconds=self._retry_after_seconds(response),
            )
        last_event = body.get("last_event")
        if not isinstance(last_event, str) or last_event not in _TERMINAL_LAST_EVENTS | _NONTERMINAL_LAST_EVENTS:
            raise NotificationProviderError(
                "Resend reconciliation omitted a supported last_event",
                outcome="retryable_failure",
                error_code="reconciliation_last_event_unknown",
                request_sha256=request_sha256,
            )
        return NotificationReconciliationEvidence(
            provider_code=_PROVIDER,
            provider_reference_id=provider_reference_id,
            last_event=last_event,
            provider_evidence_sha256=_sha256_json({"status": response.status_code, "body": body}),
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {"body_sha256": _sha256(response.content)}

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> int | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            seconds = int(value)
            return max(0, min(seconds, 3600))
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                if target.tzinfo is None:
                    return None
                from datetime import datetime, timezone
                return max(0, min(int((target - datetime.now(timezone.utc)).total_seconds()), 3600))
            except (TypeError, ValueError, OverflowError):
                return None

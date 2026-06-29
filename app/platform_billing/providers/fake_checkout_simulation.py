from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.platform_billing.domain.hashing import CanonicalSerializer
from app.platform_billing.webhooks.fake import sign_fake_webhook

CONFIRM_CHECKOUT_OPERATION_TYPE = "confirm_checkout"
FAKE_CHECKOUT_OUTCOMES = frozenset({"pending", "succeeded", "failed"})


@dataclass(frozen=True)
class FakeCheckoutOutcomeEvent:
    provider_event_id: str
    event_timestamp: int
    event_type: str
    external_operation_ref: str
    raw_body: bytes
    signature: str


class DeterministicFakeCheckoutOutcomeProducer:
    def __init__(self, *, clock=None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.generated: list[FakeCheckoutOutcomeEvent] = []

    def external_operation_ref(
        self,
        *,
        organization_id: uuid.UUID,
        checkout_operation_id: uuid.UUID,
        checkout_session_reference: str,
    ) -> str:
        canonical = CanonicalSerializer.serialize(
            {
                "checkout_operation_id": checkout_operation_id,
                "checkout_session_reference": checkout_session_reference,
                "operation_type": CONFIRM_CHECKOUT_OPERATION_TYPE,
                "organization_id": organization_id,
                "provider_code": "fake",
            }
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        return f"fake_confirm_{checkout_operation_id.hex}_{digest[:16]}"

    def generate(
        self,
        *,
        organization_id: uuid.UUID,
        checkout_operation_id: uuid.UUID,
        checkout_session_reference: str,
        simulation_operation_id: uuid.UUID,
        external_operation_ref: str,
        provider_customer_ref: str,
        requested_outcome: str,
    ) -> FakeCheckoutOutcomeEvent:
        if requested_outcome not in {"succeeded", "failed"}:
            raise ValueError("Only terminal checkout outcomes produce webhook events")
        now = self._clock()
        timestamp = int(now.timestamp())
        event_type = f"provider_operation.{requested_outcome}"
        event_identity = CanonicalSerializer.serialize(
            {
                "checkout_operation_id": checkout_operation_id,
                "checkout_session_reference": checkout_session_reference,
                "external_operation_ref": external_operation_ref,
                "operation_type": CONFIRM_CHECKOUT_OPERATION_TYPE,
                "organization_id": organization_id,
                "requested_outcome": requested_outcome,
                "simulation_operation_id": simulation_operation_id,
            }
        )
        provider_event_id = f"evt_fake_checkout_{hashlib.sha256(event_identity.encode('utf-8')).hexdigest()[:32]}"
        payload = {
            "created": timestamp,
            "data": {
                "checkout_operation_id": str(checkout_operation_id),
                "checkout_session_reference": checkout_session_reference,
                "external_customer_ref": provider_customer_ref,
                "external_object_ref": checkout_session_reference,
                "external_operation_ref": external_operation_ref,
                "organization_id": str(organization_id),
                "requested_outcome": requested_outcome,
                "simulation_operation_id": str(simulation_operation_id),
            },
            "id": provider_event_id,
            "type": event_type,
        }
        raw_body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = sign_fake_webhook(raw_body=raw_body, timestamp=timestamp)
        event = FakeCheckoutOutcomeEvent(
            provider_event_id=provider_event_id,
            event_timestamp=timestamp,
            event_type=event_type,
            external_operation_ref=external_operation_ref,
            raw_body=raw_body,
            signature=signature,
        )
        self.generated.append(event)
        return event

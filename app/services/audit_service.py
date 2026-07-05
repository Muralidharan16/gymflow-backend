import json
import hashlib
from typing import Any
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

class AuditService:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _canonicalize_json(data: dict[str, Any]) -> str:
        """
        Produce a canonical JSON string according to RFC 8785 rules.
        Sorted keys, no extra whitespace.
        """
        return json.dumps(data, separators=(',', ':'), sort_keys=True, ensure_ascii=False)

    async def append_audit_event(
        self,
        org_id: uuid.UUID,
        branch_id: uuid.UUID,
        actor_id: uuid.UUID,
        action: str,
        reason_code: str,
        reason: str,
        actor_snapshot: dict[str, Any],
        actor_permissions: dict[str, Any],
        diff: dict[str, Any] | None = None,
        request_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """
        Append an immutable audit event to the branch_audit_log using the database function.
        """
        # 1. Build canonical payload map
        payload = {
            "org_id": str(org_id),
            "branch_id": str(branch_id),
            "actor_id": str(actor_id),
            "action": action,
            "reason_code": reason_code,
            "reason": reason,
            "actor_snapshot": actor_snapshot,
            "actor_permissions": actor_permissions,
        }
        if diff:
            payload["diff"] = diff
        if request_id:
            payload["request_id"] = str(request_id)
            
        canonical_payload = self._canonicalize_json(payload)
        
        # 2. Compute SHA-256 hash
        event_hash = hashlib.sha256(canonical_payload.encode('utf-8')).hexdigest()
        
        # 3. Call the secure append function
        stmt = text("""
            SELECT app_private.append_audit_event(
                CAST(:org_id AS uuid),
                CAST(:branch_id AS uuid),
                CAST(:actor_id AS uuid),
                CAST(:actor_snapshot AS jsonb),
                CAST(:actor_permissions AS jsonb),
                CAST(:action AS varchar),
                CAST(:reason_code AS varchar),
                CAST(:reason AS text),
                CAST(:diff AS jsonb),
                CAST(:request_id AS uuid),
                CAST(:canonical_payload AS text),
                CAST(:event_hash AS varchar)
            ) AS event_id;
        """)
        
        # Need to ensure we execute as audit_writer
        # Typically the connection is app_runtime. In production, we'd SET ROLE audit_writer;
        # For now, we will execute it directly if app_runtime has access (or switch roles).
        
        result = await self.session.execute(stmt, {
            "org_id": org_id,
            "branch_id": branch_id,
            "actor_id": actor_id,
            "actor_snapshot": json.dumps(actor_snapshot),
            "actor_permissions": json.dumps(actor_permissions),
            "action": action,
            "reason_code": reason_code,
            "reason": reason,
            "diff": json.dumps(diff) if diff else None,
            "request_id": request_id,
            "canonical_payload": canonical_payload,
            "event_hash": event_hash
        })
        
        return result.scalar_one()

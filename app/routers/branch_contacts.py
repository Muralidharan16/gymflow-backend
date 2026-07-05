"""
FastAPI Endpoints Implementation Guide
Branch Contacts Subsystem - Zero-Downtime Production Ready

This guide shows how to implement the API endpoints using the hardened schema
and retry logic.
"""

from fastapi import APIRouter, Body, HTTPException, Depends, Header, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, text
from uuid import UUID
from typing import Optional, List
from datetime import datetime, timezone
import logging

from app.core.database import get_db as get_db_session
from app.core.deps import get_current_active_staff, Staff
from app.core.db_retry import managed_db_write, CircuitBreaker
from app.schemas.branch_contacts import (
    BranchContactCreate, BranchContactCreatePhone, BranchContactCreateEmail,
    BranchContactUpdate, BranchContactResponse, BranchContactAuditEvent,
    BranchContactORM, BranchContactAuditORM, PromoteToPrimaryRequest,
    ContactKind, VisibilityScope,
    normalize_phone, normalize_email
)
from app.models.org_branch import OrgBranch as OrgBranchORM

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/branches", tags=["branch-contacts"])

# Circuit breaker for write operations
write_circuit_breaker = CircuitBreaker(
    max_failures=5,
    timeout_seconds=60,
    name="branch_contacts_writes"
)


# ==============================================================================
# DEPENDENCY INJECTION
# ==============================================================================

async def get_current_user(
    current_staff: Staff = Depends(get_current_active_staff),
) -> UUID:
    return current_staff.id


async def get_current_org_id(
    current_staff: Staff = Depends(get_current_active_staff),
) -> UUID:
    return current_staff.org_id


async def set_session_context(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: Optional[UUID] = None,
    request_id: Optional[UUID] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    internal_maintenance: Optional[str] = None,
) -> None:
    """Set transaction-local PostgreSQL GUCs consumed by RLS and audit triggers."""
    values = {
        "app.current_org_id": org_id,
        "app.current_user_id": user_id,
        "app.request_id": request_id,
        "app.ip_address": ip_address,
        "app.user_agent": user_agent,
        "app.internal_maintenance": internal_maintenance,
    }
    for key, value in values.items():
        if value is not None:
            await session.execute(
                text("SELECT pg_catalog.set_config(:key, :value, true)"),
                {"key": key, "value": str(value)},
            )

async def validate_branch_ownership(
    branch_id: UUID,
    current_org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_db_session)
) -> OrgBranchORM:
    """
    Verify branch belongs to current organization.
    
    Prevents cross-org branch-contact injection attacks.
    """
    await set_session_context(session, org_id=current_org_id)
    
    stmt = select(OrgBranchORM).where(
        and_(
            OrgBranchORM.id == branch_id,
            OrgBranchORM.org_id == current_org_id
        )
    )
    branch = await session.scalar(stmt)
    
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    
    return branch


# ==============================================================================
# ENDPOINT 1: CREATE CONTACT
# ==============================================================================

@router.post(
    "/{branch_id}/contacts",
    response_model=BranchContactResponse,
    status_code=201,
    summary="Create a new branch contact",
    description="""
    Create a new contact for a branch.
    
    **Phone Contacts:**
    - Requires: phone_number (any format, will be normalized to E.164)
    - Optional: country_code (auto-detected if not provided)
    - Returns: E.164 format, display format, normalized digits
    
    **Email Contacts:**
    - Requires: email_address
    - Returns: raw email (for display), normalized (for indexing)
    
    Both types support:
    - contact_label (e.g., "Main", "Support")
    - visibility_scope (public, internal, management, emergency, billing)
    - channel_capabilities (whatsapp, sms, voice, fax)
    - is_active (default: true)
    """
)
async def create_contact(
    contact: BranchContactCreate = Body(...),
    branch_id: UUID = Path(...),
    current_user_id: UUID = Depends(get_current_user),
    current_org_id: UUID = Depends(get_current_org_id),
    request_id: Optional[UUID] = Header(None, alias="X-Request-ID"),
    ip_address: Optional[str] = Header(None, alias="X-Forwarded-For"),
    user_agent: Optional[str] = Header(None),
    branch: OrgBranchORM = Depends(validate_branch_ownership),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Create contact with automatic normalization + retry logic.
    """
    try:
        # Normalize based on contact kind
        if isinstance(contact, BranchContactCreatePhone):
            phone_e164, normalized_digits, display_format = normalize_phone(
                contact.phone_number,
                contact.country_code
            )
            
            db_contact = BranchContactORM(
                org_id=current_org_id,
                branch_id=branch_id,
                contact_kind=ContactKind.PHONE,
                phone_e164=phone_e164,
                normalized_digits=normalized_digits,
                display_format=display_format or contact.display_format,
                country_code=contact.country_code or "IN",  # Auto-detected
                contact_label=contact.contact_label,
                visibility_scope=contact.visibility_scope,
                channel_capabilities=contact.channel_capabilities.model_dump(by_alias=True),
                is_active=contact.is_active,
                created_by=current_user_id,
            )
        
        elif isinstance(contact, BranchContactCreateEmail):
            email_raw, email_normalized = normalize_email(contact.email_address)
            
            db_contact = BranchContactORM(
                org_id=current_org_id,
                branch_id=branch_id,
                contact_kind=ContactKind.EMAIL,
                email_raw=email_raw,
                email_normalized=email_normalized,
                contact_label=contact.contact_label,
                visibility_scope=contact.visibility_scope,
                channel_capabilities=contact.channel_capabilities.model_dump(by_alias=True),
                is_active=contact.is_active,
                created_by=current_user_id,
            )
        
        else:
            raise ValueError("Invalid contact kind")
        
        # Persist with retry logic
        async with managed_db_write(session, circuit_breaker=write_circuit_breaker):
            await set_session_context(
                session,
                org_id=current_org_id,
                user_id=current_user_id,
                request_id=request_id,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            
            session.add(db_contact)
            await session.flush()
            
            # Primary contact auto-promotion happens via trigger
            await session.commit()
        
        logger.info(
            f"Contact created",
            extra={
                "contact_id": db_contact.id,
                "branch_id": branch_id,
                "kind": contact.contact_kind
            }
        )
        
        return BranchContactResponse.model_validate(db_contact)
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Contact creation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to create contact")


# ==============================================================================
# ENDPOINT 2: LIST CONTACTS
# ==============================================================================

@router.get(
    "/{branch_id}/contacts",
    response_model=List[BranchContactResponse],
    summary="List active contacts for a branch"
)
async def list_contacts(
    branch_id: UUID = Path(...),
    visibility_scope: Optional[VisibilityScope] = Query(None),
    contact_kind: Optional[ContactKind] = Query(None),
    is_active: bool = Query(True),
    current_org_id: UUID = Depends(get_current_org_id),
    branch: OrgBranchORM = Depends(validate_branch_ownership),
    session: AsyncSession = Depends(get_db_session),
):
    """
    List contacts for a branch.
    
    Filters:
    - visibility_scope: public, internal, management, emergency, billing
    - contact_kind: phone, email
    - is_active: true/false (default: true)
    
    Uses covering index for fast retrieval (< 5ms).
    """
    stmt = select(BranchContactORM).where(
        and_(
            BranchContactORM.branch_id == branch_id,
            BranchContactORM.org_id == current_org_id,
            BranchContactORM.deleted_at.is_(None),
            BranchContactORM.is_active == is_active,
        )
    )
    
    if visibility_scope:
        stmt = stmt.where(BranchContactORM.visibility_scope == visibility_scope)
    
    if contact_kind:
        stmt = stmt.where(BranchContactORM.contact_kind == contact_kind)
    
    # Order by primary first, then by creation time
    stmt = stmt.order_by(
        BranchContactORM.is_primary.desc(),
        BranchContactORM.created_at.asc()
    )
    
    await set_session_context(session, org_id=current_org_id)
    
    contacts = await session.scalars(stmt)
    return [BranchContactResponse.model_validate(c) for c in contacts]


# ==============================================================================
# ENDPOINT 3: GET CONTACT
# ==============================================================================

@router.get(
    "/{branch_id}/contacts/{contact_id}",
    response_model=BranchContactResponse,
    summary="Get a specific contact"
)
async def get_contact(
    branch_id: UUID = Path(...),
    contact_id: UUID = Path(...),
    current_org_id: UUID = Depends(get_current_org_id),
    branch: OrgBranchORM = Depends(validate_branch_ownership),
    session: AsyncSession = Depends(get_db_session),
):
    """Get a single contact by ID"""
    await set_session_context(session, org_id=current_org_id)
    
    stmt = select(BranchContactORM).where(
        and_(
            BranchContactORM.id == contact_id,
            BranchContactORM.branch_id == branch_id,
            BranchContactORM.org_id == current_org_id,
            BranchContactORM.deleted_at.is_(None),
        )
    )
    
    contact = await session.scalar(stmt)
    
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    
    return BranchContactResponse.model_validate(contact)


# ==============================================================================
# ENDPOINT 4: UPDATE CONTACT
# ==============================================================================

@router.patch(
    "/{branch_id}/contacts/{contact_id}",
    response_model=BranchContactResponse,
    summary="Update a contact",
    description="""
    Update contact details.
    
    **IMMUTABLE Fields (cannot change):**
    - contact_kind (phone/email type is permanent)
    
    **Updatable Fields:**
    - phone_number (for phone contacts) - will be re-normalized
    - email_address (for email contacts) - will be re-normalized
    - contact_label
    - visibility_scope
    - channel_capabilities
    - is_active
    - is_primary (use /promote endpoint instead)
    
    **READ-ONLY Fields (server-computed):**
    - phone_e164, normalized_digits, display_format
    - email_normalized
    - created_at, created_by
    - updated_at, updated_by (auto-managed)
    """
)
async def update_contact(
    updates: BranchContactUpdate = Body(...),
    branch_id: UUID = Path(...),
    contact_id: UUID = Path(...),
    current_user_id: UUID = Depends(get_current_user),
    current_org_id: UUID = Depends(get_current_org_id),
    request_id: Optional[UUID] = Header(None, alias="X-Request-ID"),
    branch: OrgBranchORM = Depends(validate_branch_ownership),
    session: AsyncSession = Depends(get_db_session),
):
    """Update contact with normalization + retry logic"""
    try:
        await set_session_context(session, org_id=current_org_id)
        
        stmt = select(BranchContactORM).where(
            and_(
                BranchContactORM.id == contact_id,
                BranchContactORM.branch_id == branch_id,
                BranchContactORM.org_id == current_org_id,
                BranchContactORM.deleted_at.is_(None),
            )
        )
        
        contact = await session.scalar(stmt)
        
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")
        
        # Apply updates
        update_data = updates.model_dump(exclude_unset=True, by_alias=True)
        
        # Handle phone number normalization
        if contact.contact_kind == ContactKind.PHONE and updates.phone_number:
            phone_e164, normalized_digits, display_format = normalize_phone(
                updates.phone_number,
                updates.country_code
            )
            contact.phone_e164 = phone_e164
            contact.normalized_digits = normalized_digits
            contact.display_format = display_format
            contact.country_code = updates.country_code or contact.country_code
        
        # Handle email normalization
        if contact.contact_kind == ContactKind.EMAIL and updates.email_address:
            email_raw, email_normalized = normalize_email(updates.email_address)
            contact.email_raw = email_raw
            contact.email_normalized = email_normalized
        
        # Update other fields
        for key, value in update_data.items():
            if key not in ['phone_number', 'email_address', 'country_code', 'display_format']:
                if value is not None and hasattr(contact, key):
                    setattr(contact, key, value)
        
        # Persist with retry logic
        async with managed_db_write(session, circuit_breaker=write_circuit_breaker):
            await set_session_context(
                session,
                org_id=current_org_id,
                user_id=current_user_id,
                request_id=request_id,
            )
            
            await session.merge(contact)
            await session.commit()
        
        return BranchContactResponse.model_validate(contact)
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Contact update failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to update contact")


# ==============================================================================
# ENDPOINT 5: SOFT DELETE CONTACT
# ==============================================================================

@router.delete(
    "/{branch_id}/contacts/{contact_id}",
    status_code=204,
    summary="Delete (soft-delete) a contact",
    description="""
    Soft-delete a contact.
    
    **IMPORTANT: Soft-delete is PERMANENT**
    - deleted_at is immutable once set
    - Contact cannot be resurrected (database prevents it)
    - To reactivate functionality: insert a new contact record
    - Audit trail shows both deletion and new creation
    """
)
async def delete_contact(
    branch_id: UUID = Path(...),
    contact_id: UUID = Path(...),
    current_user_id: UUID = Depends(get_current_user),
    current_org_id: UUID = Depends(get_current_org_id),
    request_id: Optional[UUID] = Header(None, alias="X-Request-ID"),
    branch: OrgBranchORM = Depends(validate_branch_ownership),
    session: AsyncSession = Depends(get_db_session),
):
    """Soft-delete contact"""
    await set_session_context(session, org_id=current_org_id)
    
    stmt = select(BranchContactORM).where(
        and_(
            BranchContactORM.id == contact_id,
            BranchContactORM.branch_id == branch_id,
            BranchContactORM.org_id == current_org_id,
            BranchContactORM.deleted_at.is_(None),
        )
    )
    
    contact = await session.scalar(stmt)
    
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    
    try:
        async with managed_db_write(session, circuit_breaker=write_circuit_breaker):
            await set_session_context(
                session,
                org_id=current_org_id,
                user_id=current_user_id,
                request_id=request_id,
            )
            
            # Set soft-delete fields (atomic transaction)
            contact.deleted_at = datetime.now(timezone.utc)
            contact.deleted_by = current_user_id
            contact.is_active = False
            contact.is_primary = False
            
            await session.merge(contact)
            await session.commit()
    
    except Exception as e:
        logger.error(f"Contact deletion failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete contact")


# ==============================================================================
# ENDPOINT 6: PROMOTE TO PRIMARY
# ==============================================================================

@router.post(
    "/{branch_id}/contacts/{contact_id}/promote",
    response_model=BranchContactResponse,
    summary="Promote contact to primary status",
    description="""
    Promote a contact to primary status for its kind.
    
    **Invariant:** Each branch must have at least one primary contact per kind.
    This endpoint:
    1. Demotes current primary (if exists) to non-primary
    2. Promotes specified contact to primary
    3. Uses advisory locks to prevent race conditions
    
    **Note:** Primary auto-promotion happens via DB triggers on INSERT/DELETE.
    """
)
async def promote_to_primary(
    req: PromoteToPrimaryRequest = Body(...),
    branch_id: UUID = Path(...),
    contact_id: UUID = Path(...),
    current_user_id: UUID = Depends(get_current_user),
    current_org_id: UUID = Depends(get_current_org_id),
    request_id: Optional[UUID] = Header(None, alias="X-Request-ID"),
    branch: OrgBranchORM = Depends(validate_branch_ownership),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Promote contact to primary with advisory locking.
    
    Uses hashtextextended() advisory lock on branch_id for deterministic
    lock ordering and deadlock prevention.
    """
    await set_session_context(session, org_id=current_org_id)
    
    stmt = select(BranchContactORM).where(
        and_(
            BranchContactORM.id == contact_id,
            BranchContactORM.branch_id == branch_id,
            BranchContactORM.org_id == current_org_id,
            BranchContactORM.deleted_at.is_(None),
        )
    )
    
    contact = await session.scalar(stmt)
    
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    
    if contact.contact_kind != req.contact_kind:
        raise HTTPException(
            status_code=400,
            detail=f"Contact kind mismatch: contact is {contact.contact_kind}, "
                   f"requested {req.contact_kind}"
        )
    
    try:
        async with managed_db_write(session, circuit_breaker=write_circuit_breaker):
            await set_session_context(
                session,
                org_id=current_org_id,
                user_id=current_user_id,
                request_id=request_id,
                internal_maintenance="off",
            )
            
            # Acquire advisory lock (prevents race with other promotions)
            await session.execute(
                text("""
                    SELECT pg_advisory_xact_lock(
                        pg_catalog.hashtextextended(:branch_id, 0)
                    );
                """),
                {"branch_id": str(branch_id)},
            )
            
            # Demote current primary and promote new primary in a SINGLE statement
            # to prevent statement-level DB triggers from auto-promoting during intermediate states
            await session.execute(
                text("""
                    UPDATE public.branch_contacts
                    SET 
                        is_primary = CASE WHEN id = :contact_id THEN TRUE ELSE FALSE END,
                        is_active = CASE WHEN id = :contact_id THEN TRUE ELSE is_active END
                    WHERE branch_id = :branch_id
                      AND contact_kind = CAST(:contact_kind AS public.contact_kind_enum)
                      AND (is_primary = TRUE OR id = :contact_id)
                      AND deleted_at IS NULL;
                """),
                {
                    "branch_id": str(branch_id), 
                    "contact_kind": req.contact_kind.value,
                    "contact_id": str(contact_id)
                },
            )
            
            # Update local object for response
            contact.is_primary = True
            contact.is_active = True
            
            await session.commit()
        
        return BranchContactResponse.model_validate(contact)
    
    except Exception as e:
        logger.error(f"Primary promotion failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to promote contact")


# ==============================================================================
# ENDPOINT 7: GET AUDIT TRAIL
# ==============================================================================

@router.get(
    "/{branch_id}/contacts/{contact_id}/audit",
    response_model=List[BranchContactAuditEvent],
    summary="Get audit trail for a contact"
)
async def get_audit_trail(
    branch_id: UUID = Path(...),
    contact_id: UUID = Path(...),
    limit: int = Query(100, ge=1, le=1000),
    current_org_id: UUID = Depends(get_current_org_id),
    branch: OrgBranchORM = Depends(validate_branch_ownership),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Get complete audit trail for a contact.
    
    Returns all changes (INSERT, UPDATE, DELETE) in reverse chronological order.
    Includes: who changed it, when, what changed, why (if provided).
    """
    await set_session_context(session, org_id=current_org_id)
    
    # Verify contact belongs to this branch and org
    contact_stmt = select(BranchContactORM).where(
        and_(
            BranchContactORM.id == contact_id,
            BranchContactORM.branch_id == branch_id,
            BranchContactORM.org_id == current_org_id,
        )
    )
    if not await session.scalar(contact_stmt):
        raise HTTPException(status_code=404, detail="Contact not found in this branch")

    stmt = select(BranchContactAuditORM).where(
        and_(
            BranchContactAuditORM.branch_contact_id == contact_id,
            BranchContactAuditORM.org_id == current_org_id,
        )
    ).order_by(
        BranchContactAuditORM.changed_at.desc()
    ).limit(limit)
    
    events = await session.scalars(stmt)
    return [BranchContactAuditEvent.model_validate(e) for e in events]


# Include router in main FastAPI application
# app.include_router(router)

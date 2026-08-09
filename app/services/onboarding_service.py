# app/services/onboarding_service.py
import logging
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from app.models.auth import Owner
from app.models.organization import Organization
from app.models.trial import TrialSubscription
from app.models.audit import AuditLog
from app.schemas.onboarding import OnboardingCompleteRequest
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

class OnboardingService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def complete_onboarding(
        self, 
        owner_id: str, 
        data: OnboardingCompleteRequest,
        ip_address: str,
        user_agent: str
    ) -> dict:
        """
        Finalizes the onboarding process:
        1. Updates Organization with address details.
        2. Sets Owner as onboarding_completed.
        3. Starts 7-day Free Trial.
        4. Logs audit event.
        """
        # 1. Fetch Owner and Org
        q = select(Owner).where(Owner.id == owner_id)
        result = await self.session.execute(q)
        owner = result.scalar_one_or_none()
        
        if not owner:
            raise HTTPException(status_code=404, detail="User not found")
        
        if owner.onboarding_completed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Onboarding already completed"
            )

        q_org = select(Organization).where(Organization.id == owner.org_id)
        result_org = await self.session.execute(q_org)
        org = result_org.scalar_one_or_none()

        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        # 2. Atomic Transaction
        try:
            async with self.session.begin_nested():
                # Establish transaction-local tenant/audit identity before any
                # tenant-scoped write. org_branches and related onboarding
                # tables use forced RLS, so setting these GUCs after flush
                # would make the first principal-branch insert invalid.
                await self.session.execute(
                    text("SELECT pg_catalog.set_config('app.current_org_id', :org_id, true)"),
                    {"org_id": str(org.id)}
                )
                await self.session.execute(
                    text("SELECT pg_catalog.set_config('app.current_user_id', :user_id, true)"),
                    {"user_id": str(owner.id)}
                )

                # a. Update Organization
                org.phone = data.phone
                org.address_line1 = data.address_line1
                org.address_line2 = data.address_line2
                org.city = data.city
                org.state = data.state
                org.pincode = data.pincode
                org.profile_completed = True
                
                # Update Branding
                if data.tagline:
                    org.tagline = data.tagline
                if data.description:
                    org.description = data.description
                if data.year_established:
                    org.year_established = data.year_established
                if data.website_url:
                    org.website_url = data.website_url
                if data.social_links:
                    org.social_links = data.social_links

                # b. Update Owner
                owner.onboarding_completed = True
                owner.onboarding_completed_at = datetime.now(timezone.utc)

                # c. Initialize Free Trial — UTC canonical timestamps
                now_utc = datetime.now(timezone.utc)
                trial_start = now_utc
                trial_end = trial_start + timedelta(days=7)
                grace_end = trial_start + timedelta(days=10)
                hard_lock_at = trial_start + timedelta(days=11)

                trial = TrialSubscription(
                    organization_id=org.id,
                    trial_start=trial_start,
                    trial_end=trial_end,
                    grace_end=grace_end,
                    hard_lock_at=hard_lock_at,
                    status="active"
                )
                self.session.add(trial)

                # e. Auto-Create First Principal Branch
                from app.models.org_branch import OrgBranch, OrgBranchState
                from app.models.address import OrganizationAddress
                from app.models.gym import Gym
                import uuid
                import time
                
                branch_id = uuid.uuid4()
                
                branch = OrgBranch(
                    id=branch_id,
                    org_id=org.id,
                    branch_name=f"{org.name} Principal",
                    branch_code="PRNC-01",
                    internal_slug=f"{org.name.lower().replace(' ', '-')}-principal",
                    timezone="Asia/Kolkata",
                    currency_code="INR",
                    country_code="IN",
                    address_id=None,
                    created_by=owner.id
                )
                self.session.add(branch)
                await self.session.flush() # Persist branch to satisfy FK check on address

                # Create the OrganizationAddress record referencing branch_id
                org_address = OrganizationAddress(
                    org_id=org.id,
                    branch_id=branch_id,
                    address_type="physical",
                    address_line1=f"enc:{data.address_line1}",
                    address_line2=data.address_line2,
                    city=data.city,
                    state_province=data.state,
                    postal_code=data.pincode,
                    country_code="IN",
                    is_primary=True,
                    effective_from=datetime.now(timezone.utc)
                )
                self.session.add(org_address)
                await self.session.flush() # Persist address to get org_address.id

                # Link address_id back to branch
                branch.address_id = org_address.id

                # Create real contacts in branch_contacts table
                from app.schemas.branch_contacts import BranchContactORM, ContactKind, VisibilityScope, normalize_phone, normalize_email

                try:
                    phone_e164, normalized_digits, display_format = normalize_phone(data.phone, "IN")
                except Exception:
                    phone_e164, normalized_digits, display_format = f"+91{data.phone}", data.phone, data.phone

                phone_contact = BranchContactORM(
                    id=uuid.uuid4(),
                    org_id=org.id,
                    branch_id=branch_id,
                    contact_kind=ContactKind.PHONE,
                    phone_e164=phone_e164,
                    normalized_digits=normalized_digits,
                    display_format=display_format,
                    country_code="IN",
                    contact_label="Main",
                    visibility_scope=VisibilityScope.PUBLIC,
                    channel_capabilities={"whatsapp": True, "sms": True, "voice": True, "fax": False},
                    is_primary=True,
                    is_active=True,
                    created_at=datetime.now(timezone.utc),
                    created_by=owner.id,
                    updated_at=datetime.now(timezone.utc)
                )

                try:
                    email_raw, email_normalized = normalize_email(owner.email)
                except Exception:
                    email_raw, email_normalized = owner.email, owner.email.lower()

                email_contact = BranchContactORM(
                    id=uuid.uuid4(),
                    org_id=org.id,
                    branch_id=branch_id,
                    contact_kind=ContactKind.EMAIL,
                    email_raw=email_raw,
                    email_normalized=email_normalized,
                    contact_label="Main",
                    visibility_scope=VisibilityScope.PUBLIC,
                    channel_capabilities={"whatsapp": False, "sms": False, "voice": False, "fax": False},
                    is_primary=True,
                    is_active=True,
                    created_at=datetime.now(timezone.utc),
                    created_by=owner.id,
                    updated_at=datetime.now(timezone.utc)
                )

                self.session.add(phone_contact)
                self.session.add(email_contact)

                # Mock ULID for search_epoch_ulid (26 chars)
                mock_ulid = str(uuid.uuid4()).replace("-", "").upper()[:26]
                
                branch_state = OrgBranchState(
                    branch_id=branch_id,
                    org_id=org.id,
                    branch_status="active",
                    is_primary=True,
                    is_active=True,
                    is_public=True,
                    status="active",
                    is_operational=True,
                    search_epoch_ulid=mock_ulid
                )
                
                self.session.add(branch_state)

                # Sync with the legacy Gym record so the UI branch selector can list it with address
                gym_q = select(Gym).where(Gym.org_id == org.id)
                gym_res = await self.session.execute(gym_q)
                gym = gym_res.scalar_one_or_none()
                if gym:
                    gym.address = f"{data.address_line1}, {data.address_line2}" if data.address_line2 else data.address_line1
                    gym.city = data.city
                    gym.phone = data.phone

                # f. Audit Log
                audit = AuditLog(
                    user_id=owner.id,
                    organization_id=org.id,
                    action="ONBOARDING_COMPLETED",
                    ip_address=ip_address,
                    user_agent=user_agent,
                    metadata_json={
                        "pincode": data.pincode,
                        "trial_end": trial_end.isoformat()
                    }
                )
                self.session.add(audit)

            await self.session.commit()
            
            return {
                "status": "success",
                "trial_start": trial_start.isoformat(),
                "trial_end": trial_end.isoformat(),
                "days_remaining": 7
            }

        except Exception:
            logger.exception("Failed to complete onboarding for owner %s", owner_id)
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to finalize onboarding. Please try again."
            )

    async def get_status(self, owner_id: str) -> dict:
        """
        Returns the onboarding and trial status for the current owner.
        """
        q = select(Owner).where(Owner.id == owner_id)
        result = await self.session.execute(q)
        owner = result.scalar_one_or_none()
        
        if not owner:
            raise HTTPException(status_code=404, detail="User not found")

        trial_q = select(TrialSubscription).where(TrialSubscription.organization_id == owner.org_id)
        trial_result = await self.session.execute(trial_q)
        trial = trial_result.scalar_one_or_none()

        now_utc = datetime.now(timezone.utc)
        now_ist = now_utc.astimezone(IST)  # display-only conversion — not a canonical write
        days_remaining = 0
        if trial:
            days_remaining = max(0, (trial.trial_end.astimezone(IST).date() - now_ist.date()).days)

        return {
            "onboarding_completed": owner.onboarding_completed,
            "trial_status": trial.status if trial else "none",
            "days_remaining": days_remaining,
            "soft_lock_at": trial.trial_end.isoformat() if trial else None,
            "hard_lock_at": trial.hard_lock_at.isoformat() if trial else None
        }
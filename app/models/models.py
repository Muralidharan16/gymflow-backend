import enum
import uuid
from sqlalchemy import Column, String, DateTime, Boolean, Integer, ForeignKey, Numeric, Date, Text, func, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.types import Enum as SAEnum
from ..database import Base

# --- ENUMS ---

class SaaSPlanTier(str, enum.Enum):
    basic = 'basic'
    pro = 'pro'
    elite = 'elite'

class StaffRole(str, enum.Enum):
    owner = 'owner'
    admin = 'admin'
    trainer = 'trainer'
    receptionist = 'receptionist'

class MemberStatus(str, enum.Enum):
    active = 'active'
    inactive = 'inactive'
    frozen = 'frozen'
    blocked = 'blocked'
    expired = 'expired'

class SubscriptionStatus(str, enum.Enum):
    active = 'active'
    frozen = 'frozen'
    expired = 'expired'
    cancelled = 'cancelled'
    pending = 'pending'

class SubscriptionEndedReason(str, enum.Enum):
    expired = 'expired'
    cancelled = 'cancelled'
    upgraded = 'upgraded'
    transferred = 'transferred'
    admin_terminated = 'admin_terminated'

class RenewalType(str, enum.Enum):
    new_join = 'new_join'
    renewal = 'renewal'
    upgrade = 'upgrade'
    downgrade = 'downgrade'
    transfer = 'transfer'

class PaymentMethod(str, enum.Enum):
    cash = 'cash'
    upi = 'upi'
    card = 'card'
    bank_transfer = 'bank_transfer'

class PaymentStatus(str, enum.Enum):
    pending = 'pending'
    success = 'success'
    failed = 'failed'
    refunded = 'refunded'

class PaymentSource(str, enum.Enum):
    frontend = 'frontend'
    admin_panel = 'admin_panel'
    auto_renewal = 'auto_renewal'
    offline_cash = 'offline_cash'
    imported = 'imported'

class EntryType(str, enum.Enum):
    entry = 'entry'
    exit = 'exit'

class WhatsappStatus(str, enum.Enum):
    queued = 'queued'
    sent = 'sent'
    delivered = 'delivered'
    failed = 'failed'
    undelivered = 'undelivered'

class WhatsappMessageType(str, enum.Enum):
    reminder = 'reminder'
    alert = 'alert'
    invoice = 'invoice'
    other = 'other'

class DeviceStatus(str, enum.Enum):
    online = 'online'
    offline = 'offline'
    maintenance = 'maintenance'

# --- CORE SAAS TENANT LAYER ---

class Organization(Base):
    __tablename__ = 'organizations'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    pan_number = Column(String(10), nullable=True, unique=True, index=True)
    plan_tier = Column(SAEnum(SaaSPlanTier), nullable=False, default=SaaSPlanTier.basic)
    is_active = Column(Boolean, default=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class OrganizationFeature(Base):
    __tablename__ = 'organization_features'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False, index=True)
    feature_key = Column(String, nullable=False)
    is_enabled = Column(Boolean, default=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    __table_args__ = (Index('ix_org_feature', 'org_id', 'feature_key', unique=True),)


# --- GYM OPERATIONS ---

class GymBranch(Base):
    __tablename__ = 'gym_branches'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False, index=True)
    name = Column(String, nullable=False)
    branch_code = Column(String(10), nullable=False, index=True) # e.g. CHN001
    gstin = Column(String(15), nullable=True)
    address = Column(Text, nullable=True)
    city = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    
    __table_args__ = (Index('ix_branch_org_code', 'org_id', 'branch_code', unique=True),)


class Staff(Base):
    __tablename__ = 'staff'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False, index=True)
    primary_branch_id = Column(UUID(as_uuid=True), ForeignKey('gym_branches.id', ondelete='SET NULL'), nullable=True)
    role = Column(SAEnum(StaffRole), nullable=False)
    name = Column(String, nullable=True)  # Owner/staff display name
    email = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class StaffBranchAccess(Base):
    __tablename__ = 'staff_branch_access'
    staff_id = Column(UUID(as_uuid=True), ForeignKey('staff.id', ondelete='CASCADE'), primary_key=True)
    branch_id = Column(UUID(as_uuid=True), ForeignKey('gym_branches.id', ondelete='CASCADE'), primary_key=True)
    access_role = Column(SAEnum(StaffRole), nullable=False)


class StaffSession(Base):
    __tablename__ = 'staff_sessions'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    staff_id = Column(UUID(as_uuid=True), ForeignKey('staff.id', ondelete='CASCADE'), nullable=False, index=True)
    refresh_token_hash = Column(String, nullable=False, unique=True, index=True)
    ip_address = Column(String, nullable=True)
    user_agent = Column(Text, nullable=True)
    is_revoked = Column(Boolean, default=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    
    last_activity_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EmailVerificationToken(Base):
    __tablename__ = 'email_verification_tokens'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    staff_id = Column(UUID(as_uuid=True), ForeignKey('staff.id', ondelete='CASCADE'), nullable=False)
    token = Column(String(255), nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --- DEVICES ---

class Device(Base):
    __tablename__ = 'devices'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False)
    branch_id = Column(UUID(as_uuid=True), ForeignKey('gym_branches.id', ondelete='CASCADE'), nullable=False, index=True)
    device_uid = Column(String, nullable=False, unique=True, index=True)
    api_key_hash = Column(String, nullable=False) # Cryptographic auth
    device_name = Column(String, nullable=True)
    device_type = Column(String, nullable=True)
    firmware_version = Column(String, nullable=True)
    status = Column(SAEnum(DeviceStatus), default=DeviceStatus.offline, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


# --- MEMBERS ---

class Member(Base):
    __tablename__ = 'members'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False, index=True)
    home_branch_id = Column(UUID(as_uuid=True), ForeignKey('gym_branches.id', ondelete='RESTRICT'), nullable=False, index=True)
    member_uid = Column(String, nullable=False) # e.g. MEM-CHN001-0001
    
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True, index=True)
    email = Column(String, nullable=True)
    status = Column(SAEnum(MemberStatus), default=MemberStatus.active, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    fingerprint_id = Column(String, nullable=True)
    qr_token = Column(String, nullable=True, unique=True)
    photo_url = Column(String, nullable=True) # S3/CDN Path
    notes = Column(Text, nullable=True)
    
    current_subscription_id = Column(UUID(as_uuid=True), ForeignKey('member_subscriptions.id', ondelete='SET NULL', use_alter=True), nullable=True)
    
    # ORM relationship for eager-loading in attendance checks
    current_subscription = relationship('MemberSubscription', foreign_keys=[current_subscription_id], lazy='noload')
    
    created_by = Column(UUID(as_uuid=True), ForeignKey('staff.id', ondelete='SET NULL'), nullable=True)
    updated_by = Column(UUID(as_uuid=True), ForeignKey('staff.id', ondelete='SET NULL'), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    
    __table_args__ = (
        Index('ix_member_org_uid', 'org_id', 'member_uid', unique=True),
        Index('ix_member_org_fp', 'org_id', 'fingerprint_id'),
    )


class MemberMeasurement(Base):
    __tablename__ = 'member_measurements'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False)
    member_id = Column(UUID(as_uuid=True), ForeignKey('members.id', ondelete='CASCADE'), nullable=False, index=True)
    height_cm = Column(Numeric(5, 2), nullable=True)
    weight_kg = Column(Numeric(5, 2), nullable=True)
    recorded_date = Column(Date, nullable=False, server_default=func.current_date())
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --- SUBSCRIPTIONS & FINANCIALS ---

class SubscriptionPlan(Base):
    __tablename__ = 'subscription_plans'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False, index=True)
    name = Column(String, nullable=False)
    duration_days = Column(Integer, nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    grace_period_days = Column(Integer, nullable=False, default=3)
    is_active = Column(Boolean, default=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class MemberSubscription(Base):
    __tablename__ = 'member_subscriptions'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False)
    branch_id = Column(UUID(as_uuid=True), ForeignKey('gym_branches.id', ondelete='RESTRICT'), nullable=False)
    member_id = Column(UUID(as_uuid=True), ForeignKey('members.id', ondelete='CASCADE'), nullable=False, index=True)
    plan_id = Column(UUID(as_uuid=True), ForeignKey('subscription_plans.id', ondelete='RESTRICT'), nullable=False)
    
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    grace_until = Column(Date, nullable=True)
    status = Column(SAEnum(SubscriptionStatus), default=SubscriptionStatus.active)
    renewal_type = Column(SAEnum(RenewalType), nullable=False)
    
    ended_at = Column(DateTime(timezone=True), nullable=True)
    ended_reason = Column(SAEnum(SubscriptionEndedReason), nullable=True)
    notes = Column(Text, nullable=True)
    
    created_by = Column(UUID(as_uuid=True), ForeignKey('staff.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    __table_args__ = (Index('ix_sub_member_dates', 'member_id', 'start_date', 'end_date'),)


class Payment(Base):
    __tablename__ = 'payments'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False)
    branch_id = Column(UUID(as_uuid=True), ForeignKey('gym_branches.id', ondelete='RESTRICT'), nullable=False)
    member_id = Column(UUID(as_uuid=True), ForeignKey('members.id', ondelete='SET NULL'), nullable=True)
    subscription_id = Column(UUID(as_uuid=True), ForeignKey('member_subscriptions.id', ondelete='SET NULL'), nullable=True)
    
    amount = Column(Numeric(10, 2), nullable=False)
    payment_method = Column(SAEnum(PaymentMethod), nullable=False)
    payment_source = Column(SAEnum(PaymentSource), nullable=False)
    status = Column(SAEnum(PaymentStatus), default=PaymentStatus.pending)
    payment_date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    transaction_id = Column(String, nullable=True)
    idempotency_key = Column(String, nullable=True)  # DB-level double-charge prevention
    notes = Column(Text, nullable=True)
    
    created_by = Column(UUID(as_uuid=True), ForeignKey('staff.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    __table_args__ = (
        Index('ix_payment_idempotency', 'org_id', 'idempotency_key', unique=True, postgresql_where=Column('idempotency_key').isnot(None)),
    )


class Invoice(Base):
    __tablename__ = 'invoices'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False)
    branch_id = Column(UUID(as_uuid=True), ForeignKey('gym_branches.id', ondelete='RESTRICT'), nullable=False)
    member_id = Column(UUID(as_uuid=True), ForeignKey('members.id', ondelete='SET NULL'), nullable=True)
    payment_id = Column(UUID(as_uuid=True), ForeignKey('payments.id', ondelete='SET NULL'), nullable=True)
    
    invoice_number = Column(String, nullable=False) # e.g. INV-CHN001-001
    tax_amount = Column(Numeric(10, 2), default=0)
    total_amount = Column(Numeric(10, 2), nullable=False)
    pdf_url = Column(String, nullable=True) # S3/CDN Path
    
    created_by = Column(UUID(as_uuid=True), ForeignKey('staff.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    __table_args__ = (Index('ix_inv_org_number', 'org_id', 'invoice_number', unique=True),)


# --- ATTENDANCE ---

class AttendanceLog(Base):
    __tablename__ = 'attendance_logs'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False)
    branch_id = Column(UUID(as_uuid=True), ForeignKey('gym_branches.id', ondelete='CASCADE'), nullable=False)
    member_id = Column(UUID(as_uuid=True), ForeignKey('members.id', ondelete='SET NULL'), nullable=True)
    device_id = Column(UUID(as_uuid=True), ForeignKey('devices.id', ondelete='SET NULL'), nullable=True)
    
    scan_time = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    access_method = Column(String, nullable=False)
    entry_type = Column(SAEnum(EntryType), nullable=False)
    access_granted = Column(Boolean, nullable=False)
    denial_reason = Column(String, nullable=True)
    access_status_snapshot = Column(String, nullable=True)
    
    __table_args__ = (Index('ix_attendance_dashboard', 'org_id', 'branch_id', 'scan_time'),)


class WhatsappLog(Base):
    __tablename__ = 'whatsapp_logs'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    member_id = Column(UUID(as_uuid=True), ForeignKey('members.id', ondelete='SET NULL'), nullable=True)
    message_type = Column(SAEnum(WhatsappMessageType), nullable=False)
    content = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(SAEnum(WhatsappStatus), nullable=False, default=WhatsappStatus.queued)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditLog(Base):
    __tablename__ = 'audit_logs'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False, index=True)
    actor_id = Column(UUID(as_uuid=True), ForeignKey('staff.id', ondelete='SET NULL'), nullable=True)
    action = Column(String, nullable=False)
    ip_address = Column(String, nullable=True)
    metadata_json = Column(JSONB, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    __table_args__ = (Index('ix_audit_org_action', 'org_id', 'action'),)

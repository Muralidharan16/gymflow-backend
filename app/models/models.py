import enum
import uuid
from sqlalchemy import (
    Column,
    String,
    DateTime,
    Boolean,
    Integer,
    ForeignKey,
    Numeric,
    Date,
    Text,
    JSON,
    func,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.types import Enum as SAEnum
from ..database import Base


def gen_uuid() -> str:
    return str(uuid.uuid4())


class SubscriptionStatus(str, enum.Enum):
    active = 'active'
    expired = 'expired'
    cancelled = 'cancelled'
    pending = 'pending'
    trial = 'trial'


class PaymentStatus(str, enum.Enum):
    pending = 'pending'
    success = 'success'
    failed = 'failed'
    refunded = 'refunded'
    charged_back = 'charged_back'


class DeviceStatus(str, enum.Enum):
    online = 'online'
    offline = 'offline'
    unknown = 'unknown'
    maintenance = 'maintenance'


class AttendanceDenialReason(str, enum.Enum):
    no_subscription = 'no_subscription'
    outside_hours = 'outside_hours'
    blacklisted = 'blacklisted'
    fingerprint_not_found = 'fingerprint_not_found'
    device_error = 'device_error'
    other = 'other'


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


class Gym(Base):
    __tablename__ = 'gyms'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    owner = Column(UUID(as_uuid=True), nullable=True)
    address = Column(Text, nullable=True)
    subscription_plan = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GymOwner(Base):
    __tablename__ = 'gym_owners'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gym_id = Column(UUID(as_uuid=True), ForeignKey('gyms.id', ondelete='CASCADE'), nullable=False)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    gym = relationship('Gym', backref='owners')


class Member(Base):
    __tablename__ = 'members'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gym_id = Column(UUID(as_uuid=True), ForeignKey('gyms.id', ondelete='CASCADE'), nullable=False, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True, index=True)
    email = Column(String, nullable=True, index=True)
    fingerprint_id = Column(String, nullable=True, unique=False)
    photo_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    gym = relationship('Gym', backref='members')


class SubscriptionPlan(Base):
    __tablename__ = 'subscription_plans'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gym_id = Column(UUID(as_uuid=True), ForeignKey('gyms.id', ondelete='CASCADE'), nullable=False, index=True)
    name = Column(String, nullable=False)
    duration_days = Column(Integer, nullable=False)
    price = Column(Numeric(12, 2), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    gym = relationship('Gym', backref='plans')


class Payment(Base):
    __tablename__ = 'payments'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gym_id = Column(UUID(as_uuid=True), ForeignKey('gyms.id', ondelete='CASCADE'), nullable=False, index=True)
    member_id = Column(UUID(as_uuid=True), ForeignKey('members.id', ondelete='SET NULL'), nullable=True)
    amount = Column(Numeric(12, 2), nullable=False)
    payment_date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    razorpay_id = Column(String, nullable=True)
    status = Column(SAEnum(PaymentStatus, name='payment_status'), nullable=False, default=PaymentStatus.pending)
    raw_payload = Column(JSON, nullable=True)

    gym = relationship('Gym', backref='payments')
    member = relationship('Member', backref='payments')


class MemberSubscription(Base):
    __tablename__ = 'member_subscriptions'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    member_id = Column(UUID(as_uuid=True), ForeignKey('members.id', ondelete='CASCADE'), nullable=False)
    plan_id = Column(UUID(as_uuid=True), ForeignKey('subscription_plans.id', ondelete='RESTRICT'), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    status = Column(SAEnum(SubscriptionStatus, name='subscription_status'), nullable=False, default=SubscriptionStatus.active)
    payment_id = Column(UUID(as_uuid=True), ForeignKey('payments.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    member = relationship('Member', backref='subscriptions')
    plan = relationship('SubscriptionPlan', backref='subscriptions')
    payment = relationship('Payment', backref='subscription')


class AttendanceLog(Base):
    __tablename__ = 'attendance_logs'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gym_id = Column(UUID(as_uuid=True), ForeignKey('gyms.id', ondelete='CASCADE'), nullable=False, index=True)
    member_id = Column(UUID(as_uuid=True), ForeignKey('members.id', ondelete='SET NULL'), nullable=True, index=True)
    scan_time = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    access_granted = Column(Boolean, nullable=False, default=False)
    denial_reason = Column(SAEnum(AttendanceDenialReason, name='attendance_denial_reason'), nullable=True)

    gym = relationship('Gym', backref='attendance_logs')
    member = relationship('Member', backref='attendance_logs')


class Device(Base):
    __tablename__ = 'devices'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gym_id = Column(UUID(as_uuid=True), ForeignKey('gyms.id', ondelete='CASCADE'), nullable=False, index=True)
    device_ip = Column(String, nullable=False)
    device_model = Column(String, nullable=True)
    last_connected = Column(DateTime(timezone=True), nullable=True)
    status = Column(SAEnum(DeviceStatus, name='device_status'), nullable=False, default=DeviceStatus.unknown)
    metadata_ = Column('metadata', JSON, nullable=True)
    auth_token = Column(String, nullable=True, unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    gym = relationship('Gym', backref='devices')


class WhatsappLog(Base):
    __tablename__ = 'whatsapp_logs'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    member_id = Column(UUID(as_uuid=True), ForeignKey('members.id', ondelete='SET NULL'), nullable=True)
    message_type = Column(SAEnum(WhatsappMessageType, name='whatsapp_message_type'), nullable=False)
    content = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(SAEnum(WhatsappStatus, name='whatsapp_status'), nullable=False, default=WhatsappStatus.queued)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    member = relationship('Member', backref='whatsapp_logs')


# Index hints (some duplicated by index=True above)
Index('ix_members_gym_phone', Member.gym_id, Member.phone)
Index('ix_attendance_gym_time', AttendanceLog.gym_id, AttendanceLog.scan_time)

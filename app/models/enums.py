import enum


class OrgTier(str, enum.Enum):
    basic = "basic"      # 1 branch, 300 members
    pro = "pro"          # 3 branches, 1500 members
    elite = "elite"      # 5 branches, 5000 members


class FacilityType(str, enum.Enum):
    gym = "gym"
    calisthenics = "calisthenics"
    yoga = "yoga"
    martial_arts = "martial_arts"
    dance = "dance"
    swimming = "swimming"
    crossfit = "crossfit"


class StaffRole(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    trainer = "trainer"
    receptionist = "receptionist"


class MemberStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"
    frozen = "frozen"
    expired = "expired"
    blocked = "blocked"


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    expired = "expired"
    frozen = "frozen"
    cancelled = "cancelled"
    pending = "pending"


class PaymentMethod(str, enum.Enum):
    cash = "cash"
    upi = "upi"
    card = "card"
    bank_transfer = "bank_transfer"
    cheque = "cheque"
    online = "online"


class PaymentType(str, enum.Enum):
    subscription = "subscription"
    registration = "registration"
    addon = "addon"
    penalty = "penalty"
    refund = "refund"


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    completed = "completed"
    failed = "failed"
    refunded = "refunded"


class CheckInMethod(str, enum.Enum):
    qr = "qr"
    fingerprint = "fingerprint"
    manual = "manual"
    rfid = "rfid"
    face = "face"
    door_lock = "door_lock"


class AttendanceDenialReason(str, enum.Enum):
    subscription_expired = "subscription_expired"
    no_active_subscription = "no_active_subscription"
    account_frozen = "account_frozen"
    not_found = "not_found"


class FreezeStatus(str, enum.Enum):
    requested = "requested"
    active = "active"
    completed = "completed"
    cancelled = "cancelled"


class InvoiceStatus(str, enum.Enum):
    draft = "draft"
    issued = "issued"
    paid = "paid"
    void = "void"


class InvoiceType(str, enum.Enum):
    bill_of_supply = "bill_of_supply"
    tax_invoice = "tax_invoice"


class ImportStatus(str, enum.Enum):
    processing = "processing"
    completed = "completed"
    failed = "failed"


# License limits per tier
TIER_LIMITS = {
    OrgTier.basic: {"max_branches": 1, "max_members": 300},
    OrgTier.pro: {"max_branches": 3, "max_members": 1500},
    OrgTier.elite: {"max_branches": 5, "max_members": 5000},
}

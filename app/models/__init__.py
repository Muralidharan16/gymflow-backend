# Import all models for Alembic auto-detection
from app.models.base import Base  # noqa: F401
from app.models.organization import Organization  # noqa: F401
from app.models.gym import Gym, BranchTaxSettings, FacilityType  # noqa: F401
from app.models.staff import GymOwner  # noqa: F401
from app.models.auth import Owner, RefreshToken  # noqa: F401
from app.models.member import Member, MemberMeasurement  # noqa: F401
from app.models.subscription import (  # noqa: F401
    SubscriptionPlan,
    MemberSubscription,
    MemberFreezeLog,
)
from app.models.payment import Payment, Invoice  # noqa: F401
from app.models.attendance import AttendanceLog  # noqa: F401
from app.models.import_log import ImportLog  # noqa: F401
from app.models.trial import TrialSubscription  # noqa: F401
from app.models.audit import AuditLog  # noqa: F401
from app.models.address import (  # noqa: F401
    OrganizationAddress, MemberAddress, BranchGeolocationState,
    BranchAddressHistory, BranchAddressAuditLog, BranchGeocodeAttempt, AddressChangeOutbox
)
from app.models.notification import Notification  # noqa: F401
from app.models.org_branch import OrgBranch, OrgBranchState, AllowedBranchTransition, ActiveOrgBranch, BranchNameTranslation  # noqa: F401
from app.models.organization_user import OrganizationUser, BranchStaffRole, BranchStaffRoleEnum  # noqa: F401
from app.models.auth_session import AuthSessionFamily, AuthSession  # noqa: F401
from app.models.branch_audit import BranchAuditLog  # noqa: F401
from app.models.audit_key import AuditKeyRegistry  # noqa: F401
from app.models.outbox import TransactionalOutbox  # noqa: F401
from app.models.branch_operating_hours import (  # noqa: F401
    OrganizationOperatingHours,
    BranchOperatingHours,
    BranchSpecialHours,
    BranchHoursProjection,
    BranchHoursAuditLog
)
from app.models.branch_lifecycle import (  # noqa: F401
    BranchStatusDefinition,
    BranchStatusTransition,
    BranchDeactivationPolicy,
    BranchStatusHistory,
    BranchLifecycleEvent,
    BranchOutboxEvent,
    BranchWatchdogAlert
)

from app.models.membership_plan import MembershipPlan, PlanStatus, DurationUnit  # noqa: F401
from app.models.organization_counter import OrganizationCounter  # noqa: F401
from app.models.member_subscription_v2 import (  # noqa: F401
    MemberSubscriptionV2,
    ModernSubscriptionStatus,
    SubscriptionMember,
    SubscriptionMemberRole,
)
from app.models.subscription_lifecycle import (  # noqa: F401
    SubscriptionEvent,
    SubscriptionFreeze,
    SubscriptionOperationIdempotency,
    SubscriptionSeries,
    SubscriptionSlotAssignment,
    SubscriptionTerm,
    SubscriptionTermSlot,
)

# Import all models for Alembic auto-detection
from app.models.base import Base  # noqa: F401
from app.models.organization import Organization  # noqa: F401
from app.models.gym import Gym, BranchTaxSettings  # noqa: F401
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

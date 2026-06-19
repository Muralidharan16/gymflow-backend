"""Platform Billing compatibility layer for legacy shadow comparison — Phase 2."""

from app.platform_billing.compat.legacy_access_snapshot import (
    LegacyAccessSnapshot,
    LegacyTrialAdapter,
)

__all__ = [
    "LegacyAccessSnapshot",
    "LegacyTrialAdapter",
]
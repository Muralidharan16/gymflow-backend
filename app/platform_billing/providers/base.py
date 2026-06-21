from __future__ import annotations

from typing import Protocol

from app.platform_billing.domain.provider_operations import (
    ProviderCallRequest,
    ProviderCallResult,
)


class PlatformBillingProvider(Protocol):
    async def execute(self, request: ProviderCallRequest) -> ProviderCallResult:
        """Execute one provider-neutral operation."""

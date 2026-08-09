from __future__ import annotations

import pytest_asyncio

from app.core.database import AsyncSessionLocal
from app.domain.synthetic_organizations import (
    SYNTHETIC_ORGANIZATION_TRUSTED_SOURCE,
    SyntheticOrganizationCreationCommand,
)
from app.services.synthetic_organizations import SyntheticOrganizationCreationService


_D11_TEST_MODULE = "test_phase6an_d10_synthetic_organization_service.py"
_D11_NAME = "DOERS RAZORPAY TEST SMOKE ORGANIZATION"
_D11_SLUG = "doers-razorpay-test-smoke"
_D11_KEY = "organization-create:synthetic:test:finance-razorpay-smoke"


@pytest_asyncio.fixture(autouse=True)
async def bootstrap_persistent_d11_replay_evidence(request):
    """Provide the immutable D11 identity that the historical replay tests require.

    The original regression surface intentionally treats this identity/evidence
    pair as persistent and append-only. Fresh CI databases therefore need to
    create it explicitly instead of depending on state left by a developer's
    local database. Seed through the production service contract; never disable
    the immutability trigger and never delete the evidence afterward.
    """
    if request.node.path.name != _D11_TEST_MODULE:
        yield
        return

    async with AsyncSessionLocal() as session:
        result = await SyntheticOrganizationCreationService(
            session,
            environment="development",
        ).create_synthetic_organization(
            SyntheticOrganizationCreationCommand(
                name=_D11_NAME,
                slug=_D11_SLUG,
                idempotency_key=_D11_KEY,
                synthetic_mode=True,
                trusted_source=SYNTHETIC_ORGANIZATION_TRUSTED_SOURCE,
            )
        )
        await session.commit()

    assert result.slug == _D11_SLUG
    assert result.is_active is True
    yield

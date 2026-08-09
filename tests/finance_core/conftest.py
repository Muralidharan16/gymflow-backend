from __future__ import annotations

import os

import pytest
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
_FINANCE_LANE_ENV = "FINANCE_CORE_TEST_DATABASE_URL"


def pytest_collection_modifyitems(config, items):
    """Keep Finance Core out of the general runtime lane.

    Finance tests exercise a distinct security boundary and must run only when
    the dedicated Finance Core disposable database/runtime identity is present.
    They are not silently disabled in CI: ``finance-hardening-ci.yml`` supplies
    this environment and executes the complete ``tests/finance_core`` package.
    """
    if os.environ.get(_FINANCE_LANE_ENV):
        return

    skip = pytest.mark.skip(
        reason=(
            "Finance Core requires the isolated finance runtime/admin CI lane; "
            f"{_FINANCE_LANE_ENV} is not configured"
        )
    )
    for item in items:
        if "tests/finance_core/" in item.nodeid:
            item.add_marker(skip)


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

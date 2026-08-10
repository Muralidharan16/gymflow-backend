from __future__ import annotations

import os

import pytest
import pytest_asyncio

from app.domain.synthetic_organizations import (
    SYNTHETIC_ORGANIZATION_TRUSTED_SOURCE,
    SyntheticOrganizationCreationCommand,
)
from app.services.synthetic_organizations import SyntheticOrganizationCreationService
from tests.finance_core.admin_database import finance_admin_session
from tests.finance_core.synthetic_database import SyntheticOrgSessionLocal


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
    """Isolate synthetic-organization evidence from Finance Core runtime.

    Synthetic organization creation exists only as an approved non-production
    bootstrap facility. The historical D10/D11 module therefore executes through
    a dedicated synthetic test login rather than the Finance Core runtime login.
    The immutable D11 baseline itself is seeded by the guarded admin identity via
    the real service contract; no trigger or RLS protection is disabled.
    """
    if request.node.path.name != _D11_TEST_MODULE:
        yield
        return

    module = request.module
    original_session_factory = getattr(module, "AsyncSessionLocal", None)
    if original_session_factory is None:
        raise AssertionError(
            f"{_D11_TEST_MODULE} must expose its AsyncSessionLocal integration boundary"
        )
    module.AsyncSessionLocal = SyntheticOrgSessionLocal

    try:
        async with finance_admin_session() as session:
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
    finally:
        module.AsyncSessionLocal = original_session_factory

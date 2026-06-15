"""
tests/platform_billing/conftest.py
===================================
Shared fixtures for platform billing tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clear_policy_cache() -> None:
    from app.platform_billing.policies import policy_loader

    policy_loader._runtime_policy = None


@pytest.fixture
def policies_dir() -> Path:
    from app.platform_billing.policies.policy_loader import POLICIES_DIR

    return POLICIES_DIR


@pytest.fixture
def runtime_policy():
    from app.platform_billing.policies.policy_loader import _reload_for_test

    return _reload_for_test()

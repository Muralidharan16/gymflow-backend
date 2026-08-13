from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.synthetic_organizations import SyntheticOrganizationRepository


_CANONICAL_REPLAY_COLUMNS = (
    "id",
    "name",
    "slug",
    "tier",
    "business_type",
    "is_active",
    "max_branches",
    "default_currency_code",
    "description",
    "tagline",
)


class _OneOrNoneResult:
    def one_or_none(self):
        return None


class _CaptureSession:
    def __init__(self):
        self.statements = []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return _OneOrNoneResult()


@pytest.mark.asyncio
async def test_replay_organization_lookup_is_plain_select_of_canonical_columns_only():
    session = _CaptureSession()
    repository = SyntheticOrganizationRepository(session)

    await repository.get_replay_organization_by_id(uuid.uuid4())

    sql = str(session.statements[-1].compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" not in sql
    for column in _CANONICAL_REPLAY_COLUMNS:
        assert f"ORGANIZATIONS.{column.upper()}" in sql

    # Sensitive/profile fields outside the replay hash/result contract must not
    # be widened into the reduced replay query.
    assert "ORGANIZATIONS.PHONE" not in sql
    assert "ORGANIZATIONS.ADDRESS_LINE1" not in sql
    assert "ORGANIZATIONS.DOCUMENT_URL" not in sql
    assert "ORGANIZATIONS.LOGO_META" not in sql


def test_idempotency_migration_owns_column_scoped_replay_grant_without_write_privilege():
    root = Path(__file__).resolve().parents[2]
    migration = root / "alembic/versions/2b3c4d5e6f70_organization_creation_idempotency.py"
    source = migration.read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    expected_columns = ", ".join(_CANONICAL_REPLAY_COLUMNS)
    assert f'GRANT SELECT ({{_replay_column_list()}}) ON TABLE public.organizations TO test_runner;' in normalized
    assert f'REVOKE SELECT ({{_replay_column_list()}}) ON TABLE public.organizations FROM test_runner;' in normalized

    assert not re.search(
        r"GRANT\s+UPDATE(?:\s*\([^)]*\))?\s+ON\s+(?:TABLE\s+)?public\.organizations\s+TO\s+test_runner",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert not re.search(
        r"GRANT\s+DELETE\s+ON\s+(?:TABLE\s+)?public\.organizations\s+TO\s+test_runner",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # The migration's declared column tuple must remain aligned with the query
    # contract; adding a replay field requires an intentional privilege review.
    for column in _CANONICAL_REPLAY_COLUMNS:
        assert f'"{column}"' in source
    assert expected_columns == "id, name, slug, tier, business_type, is_active, max_branches, default_currency_code, description, tagline"

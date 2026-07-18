from __future__ import annotations

import pytest
from tests.platform_billing.platform_billing_test_isolation import (
    PLATFORM_BILLING_TEST_ADMIN_DATABASE_URL,
    PLATFORM_BILLING_TEST_DATABASE_URL,
    PlatformBillingTestIsolationError,
    get_platform_billing_test_config,
    sanitized_error_message,
)


def _url(database: str, *, user: str = "test_runner", password: str = "secret") -> str:
    return f"postgresql+asyncpg://{user}:{password}@localhost:5432/{database}"


def _env(
    *,
    runtime: str | None = None,
    admin: str | None = None,
    shared: str | None = None,
) -> dict[str, str]:
    values: dict[str, str] = {}
    if runtime is not None:
        values[PLATFORM_BILLING_TEST_DATABASE_URL] = runtime
    if admin is not None:
        values[PLATFORM_BILLING_TEST_ADMIN_DATABASE_URL] = admin
    if shared is not None:
        values["TEST_DATABASE_URL"] = shared
    return values


def test_missing_runtime_platform_billing_url_is_rejected():
    with pytest.raises(PlatformBillingTestIsolationError, match="missing runtime"):
        get_platform_billing_test_config(
            _env(admin=_url("gymflow_platform_billing_test_unit", user="migration_owner"))
        )


def test_missing_admin_platform_billing_url_is_rejected():
    with pytest.raises(PlatformBillingTestIsolationError, match="missing admin"):
        get_platform_billing_test_config(_env(runtime=_url("gymflow_platform_billing_test_unit")))


def test_runtime_url_equal_to_test_database_url_is_rejected():
    runtime = _url("gymflow_platform_billing_test_unit")
    with pytest.raises(PlatformBillingTestIsolationError, match="runtime.*TEST_DATABASE_URL"):
        get_platform_billing_test_config(
            _env(
                runtime=runtime,
                admin=_url("gymflow_platform_billing_test_unit", user="migration_owner"),
                shared=runtime,
            )
        )


def test_admin_url_equal_to_test_database_url_is_rejected():
    admin = _url("gymflow_platform_billing_test_unit", user="migration_owner")
    with pytest.raises(PlatformBillingTestIsolationError, match="admin.*TEST_DATABASE_URL"):
        get_platform_billing_test_config(
            _env(
                runtime=_url("gymflow_platform_billing_test_unit"),
                admin=admin,
                shared=admin,
            )
        )


def test_runtime_and_admin_database_mismatch_is_rejected():
    with pytest.raises(PlatformBillingTestIsolationError, match="same database"):
        get_platform_billing_test_config(
            _env(
                runtime=_url("gymflow_platform_billing_test_a"),
                admin=_url("gymflow_platform_billing_test_b", user="migration_owner"),
            )
        )


@pytest.mark.parametrize("database", ["gymflow_test", "gymflow_migration_test", "postgres", "template1"])
def test_shared_or_system_database_name_is_rejected(database: str):
    with pytest.raises(PlatformBillingTestIsolationError, match="not disposable"):
        get_platform_billing_test_config(
            _env(runtime=_url(database), admin=_url(database, user="migration_owner"))
        )


@pytest.mark.parametrize("database", ["gymflow_prod", "gymflow_staging", "customer_test"])
def test_unsafe_database_name_is_rejected(database: str):
    with pytest.raises(PlatformBillingTestIsolationError, match="explicitly disposable"):
        get_platform_billing_test_config(
            _env(runtime=_url(database), admin=_url(database, user="migration_owner"))
        )


def test_same_runtime_and_admin_user_is_rejected():
    with pytest.raises(PlatformBillingTestIsolationError, match="users must be separated"):
        get_platform_billing_test_config(
            _env(
                runtime=_url("gymflow_platform_billing_test_unit", user="migration_owner"),
                admin=_url("gymflow_platform_billing_test_unit", user="migration_owner"),
            )
        )


def test_sanitized_errors_contain_no_password_or_full_url():
    secret = "super-secret-password"
    full_url = _url("gymflow_test", password=secret)
    with pytest.raises(PlatformBillingTestIsolationError) as excinfo:
        get_platform_billing_test_config(
            _env(runtime=full_url, admin=_url("gymflow_test", user="migration_owner"))
        )

    message = sanitized_error_message(excinfo.value)
    assert secret not in message
    assert full_url not in message


def test_valid_dedicated_runtime_and_admin_pair_is_accepted():
    config = get_platform_billing_test_config(
        _env(
            runtime=_url("gymflow_platform_billing_test_unit"),
            admin=_url("gymflow_platform_billing_test_unit", user="migration_owner"),
            shared=_url("gymflow_test"),
        )
    )

    assert config.identity.database == "gymflow_platform_billing_test_unit"
    assert config.runtime_user == "test_runner"
    assert config.admin_user == "migration_owner"
    assert config.admin_url.password == "secret"


class _FakeResult:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)


class _FakeEngine:
    def __init__(self):
        self.disposed = False

    async def dispose(self):
        self.disposed = True


class _FakeSession:
    def __init__(self, *, fail_on_truncate: bool = False):
        self.fail_on_truncate = fail_on_truncate
        self.executed: list[str] = []
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append(sql)
        if "information_schema.tables" in sql:
            return _FakeResult([("organizations",), ("platform_products",)])
        if "TRUNCATE TABLE" in sql and self.fail_on_truncate:
            raise RuntimeError("forced cleanup failure")
        return _FakeResult()

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class _FakeSessionmaker:
    def __init__(self, session: _FakeSession):
        self.session = session

    def __call__(self):
        return self.session


@pytest.mark.asyncio
async def test_cleanup_uses_admin_connection_and_checks_postconditions_before_commit(monkeypatch):
    from tests.platform_billing import test_phase1_schema as phase1

    fake_engine = _FakeEngine()
    fake_session = _FakeSession()
    guard_calls: list[str] = []

    async def fake_assert_test_database(session):
        guard_calls.append("test_database")

    async def fake_admin_posture(session, config):
        guard_calls.append("admin_posture")

    async def fake_no_protected_identity(session):
        guard_calls.append("no_protected")

    monkeypatch.setattr(phase1, "get_platform_billing_test_config", lambda: object())
    monkeypatch.setattr(
        phase1,
        "create_platform_billing_admin_sessionmaker",
        lambda config: (fake_engine, _FakeSessionmaker(fake_session)),
    )
    monkeypatch.setattr(phase1, "assert_test_database", fake_assert_test_database)
    monkeypatch.setattr(phase1, "assert_platform_billing_admin_posture", fake_admin_posture)
    monkeypatch.setattr(phase1, "assert_no_protected_d11_identity", fake_no_protected_identity)

    await phase1.cleanup_phase1_tables()

    assert fake_session.committed is True
    assert fake_session.rolled_back is False
    assert fake_engine.disposed is True
    assert guard_calls == ["test_database", "admin_posture", "no_protected", "no_protected"]
    assert any("TRUNCATE TABLE" in sql for sql in fake_session.executed)


@pytest.mark.asyncio
async def test_cleanup_error_rolls_back_and_does_not_commit(monkeypatch):
    from tests.platform_billing import test_phase1_schema as phase1

    fake_engine = _FakeEngine()
    fake_session = _FakeSession(fail_on_truncate=True)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(phase1, "get_platform_billing_test_config", lambda: object())
    monkeypatch.setattr(
        phase1,
        "create_platform_billing_admin_sessionmaker",
        lambda config: (fake_engine, _FakeSessionmaker(fake_session)),
    )
    monkeypatch.setattr(phase1, "assert_test_database", noop)
    monkeypatch.setattr(phase1, "assert_platform_billing_admin_posture", noop)
    monkeypatch.setattr(phase1, "assert_no_protected_d11_identity", noop)

    with pytest.raises(RuntimeError, match="forced cleanup failure"):
        await phase1.cleanup_phase1_tables()

    assert fake_session.committed is False
    assert fake_session.rolled_back is True
    assert fake_engine.disposed is True

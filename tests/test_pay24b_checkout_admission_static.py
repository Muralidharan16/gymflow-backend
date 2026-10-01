from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "zz67d8e9f0a66_pay24b_checkout_admission_bridge.py"
AUTHORITY = ROOT / "app" / "payment_activation" / "authority.py"
CHECKOUT = ROOT / "app" / "finance_core" / "services" / "checkout_orchestration.py"
PAYMENT_API = ROOT / "app" / "finance_core" / "api" / "payment_boundary.py"
MEMBER_ROUTE = ROOT / "app" / "routers" / "member_subscriptions_v2.py"
REFUND_WORKER = ROOT / "app" / "finance_core" / "services" / "refund_provider_worker.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_pay24b_bridge_is_checkout_only_and_least_privilege() -> None:
    source = _source(MIGRATION)
    assert 'revision = "zz67d8e9f0a66"' in source
    assert 'down_revision = "zz57d8e9f0a65"' in source
    assert "SECURITY DEFINER" in source
    assert "SET search_path=pg_catalog" in source
    assert "SET row_security=on" in source
    assert "p_capability IS DISTINCT FROM 'checkout'" in source
    assert "finance_payment_runtime" in source
    assert "pay24a_request_provider_admission(" in source
    assert "GRANT EXECUTE ON FUNCTION {_FUNCTION} TO finance_payment_runtime" in source
    assert "TO app_runtime" not in source
    assert "TO finance_refund_runtime" not in source
    assert "provider I/O" in source



def test_migration_preflight_does_not_require_app_secure_schema_usage() -> None:
    source = _source(MIGRATION)
    assert "pg_catalog.to_regprocedure" not in source
    assert "pg_catalog.pg_proc" in source
    assert "pg_catalog.pg_namespace" in source
    assert "pg_catalog.oidvectortypes" in source
    assert source.count("_catalog_function_exists(") >= 4

def test_payment_authority_exposes_current_generation_bridge_without_snapshot_use() -> None:
    source = _source(AUTHORITY)
    assert "_REQUEST_CURRENT_PROVIDER_ADMISSION_SQL" in source
    assert "pay24b_request_current_provider_admission" in source
    method = source.split("async def request_current_provider_admission(", 1)[1]
    method = method.split("async def start_provider_admission(", 1)[0]
    assert "_REQUEST_CURRENT_PROVIDER_ADMISSION_SQL" in method
    assert "_ACTIVATION_SNAPSHOT_SQL" not in method
    assert "expected_generation" not in method


def test_checkout_admission_identity_binds_finance_operation_fence_and_request_hash() -> None:
    source = _source(CHECKOUT)
    method = source.split("def provider_admission_binding(", 1)[1]
    method = method.split("async def call_provider(", 1)[0]
    assert 'f"checkout:{claim.operation_id}:{claim.lease_fence}"' in method
    assert "provider_checkout_request_hash(" in method
    assert "payment_id=prepared.finance_checkout_intent_id" in method
    assert "request=prepared.provider_request" in method


def test_payment_route_commits_admission_start_before_provider_io_and_finishes_atomically() -> None:
    source = _source(PAYMENT_API)
    route = source.split("async def create_checkout_session(", 1)[1].split(
        "@router.get", 1
    )[0]

    local_prepare = route.index("prepare_checkout_session(")
    local_commit = route.index("await db.commit()", local_prepare)
    bind = route.index("bind_provider_effects(payment_db)", local_commit)
    claim = route.index("payment_effects.claim_provider_operation(", bind)
    request = route.index("request_current_provider_admission(", claim)
    claim_admission_commit = route.index("await payment_db.commit()", request)
    start = route.index("start_provider_admission(", claim_admission_commit)
    start_commit = route.index("await payment_db.commit()", start)
    provider = route.index("call_provider(", start_commit)
    local_success = route.index("payment_effects.finish_provider_success(", provider)
    admission_success = route.rindex("finish_provider_admission(")
    final_commit = route.index("await payment_db.commit()", admission_success)

    assert (
        local_prepare
        < local_commit
        < bind
        < claim
        < request
        < claim_admission_commit
        < start
        < start_commit
        < provider
        < local_success
        < admission_success
        < final_commit
    )
    assert "DurableActivationAuthority(payment_db)" in route
    assert "DurableActivationAuthority(db)" not in route
    assert route.count("await db.commit()") == 1
    assert "execution_id=lease_owner" in route
    assert "ActivationCapability.CHECKOUT" in route
    assert 'outcome=("unknown" if exc.requires_reconciliation else "completed")' in route
    assert 'outcome="completed"' in route


def test_b1b_intentionally_wires_member_checkout_but_not_refund_worker() -> None:
    member = _source(MEMBER_ROUTE)
    refund = _source(REFUND_WORKER)
    assert "request_current_provider_admission(" in member
    assert "start_provider_admission(" in member
    assert "finish_provider_admission(" in member
    assert "ActivationCapability.CHECKOUT" in member
    assert "request_current_provider_admission(" not in refund
    assert "start_provider_admission(" not in refund
    assert "finish_provider_admission(" not in refund

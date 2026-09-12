from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "zg07d8e9f0a41_p5w_worker_claim_fences.py"
LIFECYCLE_MODEL = ROOT / "app" / "models" / "branch_lifecycle.py"
TRANSACTIONAL_MODEL = ROOT / "app" / "models" / "outbox.py"
LIFECYCLE_POLLER = ROOT / "app" / "tasks" / "branch_outbox_poller.py"
TRANSACTIONAL_POLLER = ROOT / "app" / "tasks" / "outbox_poller.py"
WORKFLOW = ROOT / ".github" / "workflows" / "p5w-worker-fencing-pg16.yml"
SLICE = ROOT / "docs" / "architecture" / "P5W1_WORKER_CLAIM_FENCING.md"


def _function(source: str, start: str, end: str | None) -> str:
    body = source.split(start, 1)[1]
    if end is not None:
        body = body.split(end, 1)[0]
    return body


def test_p5w_migration_is_append_only_and_adds_only_claim_generation() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "zg07d8e9f0a41"' in source
    assert 'down_revision = "zf07d8e9f0a40"' in source
    assert source.count("ADD COLUMN lease_fence bigint NOT NULL DEFAULT 0") == 1
    assert '"public", "branch_outbox_events", "chk_branch_outbox_lease_fence"' in source
    assert '"public", "transactional_outbox", "chk_transactional_outbox_lease_fence"' in source
    assert "CHECK (lease_fence >= 0)" in source
    assert "GRANT UPDATE (lease_fence)" in source
    assert "GRANT UPDATE ON TABLE" not in source
    assert "GRANT SELECT" not in source
    assert "REVOKE INSERT ON TABLE public.branch_outbox_events FROM app_runtime" in source
    assert "app_runtime retained table-wide branch outbox INSERT" in source
    assert "lease_fence INSERT leaked" in source
    assert "CREATE POLICY" not in source
    assert "BYPASSRLS" not in source
    assert "provider" in source.lower()
    assert "financial execution" in source.lower()


def test_p5w_migration_refuses_claim_evidence_loss_on_downgrade() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    downgrade = _function(source, "def downgrade()", None)

    assert "WHERE lease_fence <> 0" in downgrade
    assert "downgrade refuses loss of durable P5 claim-generation evidence" in downgrade
    assert downgrade.index("WHERE lease_fence <> 0") < downgrade.index("DROP COLUMN lease_fence")
    assert "REVOKE UPDATE (lease_fence)" in downgrade
    assert "GRANT INSERT ON TABLE public.branch_outbox_events TO app_runtime" in downgrade


def test_p5w_models_match_both_database_fence_columns() -> None:
    lifecycle = LIFECYCLE_MODEL.read_text(encoding="utf-8")
    transactional = TRANSACTIONAL_MODEL.read_text(encoding="utf-8")

    for source, constraint in (
        (lifecycle, "chk_branch_outbox_lease_fence"),
        (transactional, "chk_transactional_outbox_lease_fence"),
    ):
        assert "lease_fence: Mapped[int]" in source
        assert "BigInteger" in source
        assert 'server_default=text("0")' in source
        assert '"lease_fence >= 0"' in source
        assert constraint in source


def test_lifecycle_claim_rotates_fence_and_reclaims_expired_final_attempt() -> None:
    source = LIFECYCLE_POLLER.read_text(encoding="utf-8")
    claim = _function(source, "async def _claim_events", "async def _install_saga_context")

    assert "status = 'processing' AS reclaiming" in claim
    assert "status = 'pending'" in claim
    assert "attempt_count < max_attempts" in claim
    assert "status = 'processing'" in claim
    assert "leased_until <= pg_catalog.clock_timestamp()" in claim
    assert "WHEN candidates.reclaiming THEN outbox_data.attempt_count" in claim
    assert "ELSE outbox_data.attempt_count + 1" in claim
    assert "lease_fence = outbox_data.lease_fence + 1" in claim
    assert "outbox_data.lease_fence" in claim
    assert "FOR UPDATE SKIP LOCKED" in claim

    # The attempt bound belongs only to a new pending attempt. Keeping it outside
    # the pending arm would strand an expired processing row at max attempts.
    where = claim.split("WHERE (", 1)[1].split("ORDER BY", 1)[0]
    assert where.index("status = 'pending'") < where.index("attempt_count < max_attempts")
    assert where.index("attempt_count < max_attempts") < where.index("OR (")


def test_lifecycle_terminal_and_failure_writes_require_live_exact_fence() -> None:
    source = LIFECYCLE_POLLER.read_text(encoding="utf-8")
    delivered = _function(source, "async def _mark_delivered", "async def _process_saga_event")
    failure = _function(source, "async def _fail_event", "async def _process_deferred_external_event")

    for body in (delivered, failure):
        assert "lease_fence = :lease_fence" in body
        assert "leased_until > pg_catalog.clock_timestamp()" in body
    assert failure.count("lease_fence = :lease_fence") == 3
    assert failure.count("leased_until > pg_catalog.clock_timestamp()") == 3
    assert 'int(event["lease_fence"])' in failure


def test_transactional_claim_rotates_fence_and_reclaims_expired_final_attempt() -> None:
    source = TRANSACTIONAL_POLLER.read_text(encoding="utf-8")
    claim = _function(source, "async def _claim_ready_events", "async def _install_tenant_worker_context")

    assert "leased_until IS NOT NULL AS reclaiming" in claim
    assert "leased_until IS NULL" in claim
    assert "delivery_attempts < :max_attempts" in claim
    assert "leased_until <= pg_catalog.clock_timestamp()" in claim
    assert "WHEN candidates.reclaiming THEN outbox_data.delivery_attempts" in claim
    assert "ELSE outbox_data.delivery_attempts + 1" in claim
    assert "lease_fence = outbox_data.lease_fence + 1" in claim
    assert "outbox_data.lease_fence" in claim
    assert "FOR UPDATE SKIP LOCKED" in claim


def test_transactional_terminal_and_failure_writes_require_live_exact_fence() -> None:
    source = TRANSACTIONAL_POLLER.read_text(encoding="utf-8")
    complete = _function(source, "async def _complete_owned_event", "async def _supersede_unleased_temporal_refreshes")
    failure = _function(source, "async def _release_failed_event", "async def _process_claimed_event")

    for body in (complete, failure):
        assert "lease_fence = :lease_fence" in body
        assert "leased_until > pg_catalog.clock_timestamp()" in body
    assert failure.count("lease_fence = :lease_fence") == 2
    assert failure.count("leased_until > pg_catalog.clock_timestamp()") == 2
    assert 'int(event["lease_fence"])' in failure


def test_p5w_runtime_gate_uses_real_postgres_reduced_identity_and_same_head() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    for phrase in (
        "PostgreSQL 16",
        "worker_test_runtime",
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS",
        "tests/test_p5w_worker_fencing_runtime.py",
        "tests/test_p5w_worker_fencing_static_contracts.py",
        "downgrade zf07d8e9f0a40",
        "current --check-heads",
        "git rev-parse HEAD",
        "P5W_WORKER_FENCING_PG16=PASS",
        "claim-fence INSERT leaked",
    ):
        assert phrase in source


def test_p5w_slice_claims_only_its_bounded_evidence() -> None:
    source = SLICE.read_text(encoding="utf-8").lower()

    assert "a246f90bb0b349d8c1f88b72e6a93733b62bc3a6" in source
    assert "241792b096f22d1a7471e5d1a13a103a21d9f518" in source
    assert "ownership had no monotonic claim generation" in source
    assert "strand a worker that died during its final allowed attempt" in source
    assert "p5w_worker_fencing_pg16=pass" in source
    for limitation in (
        "does not complete p5-w or p5",
        "real worker-process death before/after commit",
        "real celery/redis redelivery",
        "provider-success/database-acknowledgement recovery",
        "database restart/disconnect recovery",
        "lifecycle/finance race certification",
        "compensation crash/replay certification",
        "refund-provider execution remains deferred and fail-closed",
    ):
        assert limitation in source

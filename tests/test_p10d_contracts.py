from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/p10d-query-performance.yml"
PROBE = ROOT / "scripts/ci/p10d_query_performance.py"
ROUTER = ROOT / "app/routers/members.py"
REPOSITORY = ROOT / "app/repositories/member_repo.py"


def test_p10d_uses_real_explain_analyze_buffers_and_index_inventory():
    probe = PROBE.read_text(encoding="utf-8")
    for fragment in (
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)",
        "pg_catalog.pg_index",
        "pg_catalog.pg_get_indexdef",
        "exact_duplicate_signatures",
        "uq_members_org_member_number",
        "ix_members_org_phone",
        "temp_read_blocks",
        "temp_written_blocks",
    ):
        assert fragment in probe


def test_p10d_query_count_is_fixed_not_per_row():
    probe = PROBE.read_text(encoding="utf-8")
    assert "page_size=1 expected 3 fixed business SQL statements" in probe
    assert "page_size=50 expected 3 fixed business SQL statements" in probe
    assert "query count grows with returned row count (N+1 regression)" in probe
    assert "MemberService(session).list_members_org" in probe


def test_critical_member_collection_route_has_hard_page_cap():
    router = ROUTER.read_text(encoding="utf-8")
    assert "page_size: int = Query(50, ge=1, le=200" in router
    assert 'modern_router = APIRouter(prefix="/organizations/{org_id}/members"' in router
    # An older repository helper is deliberately not wired to an API route.
    assert "get_all_for_gym(" not in router


def test_p10d_workflow_uses_pg16_production_shape_and_no_new_postfreeze_budget():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for fragment in (
        "P10-D Query Plans Indexes N+1 and Pagination",
        "scripts/ci/prepare_p3e_pg16.sh",
        "scripts/ci/p9m_seed_populated_predecessor.sql",
        "scripts/ci/p10b_seed_baseline.sql",
        "scripts/ci/verify_p10_performance_budgets.py",
        "scripts/ci/p10d_query_performance.py",
        "P10_QUERY_PERFORMANCE=PASS",
        "P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert fragment in workflow
    assert "p10d_performance_budgets" not in workflow.lower()


def test_member_repository_page_query_is_explicitly_bounded():
    source = REPOSITORY.read_text(encoding="utf-8")
    assert ".offset(offset)" in source
    assert ".limit(size)" in source

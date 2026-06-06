"""
Governance & Security Validation Scripts for Branch Contacts

Run these scripts during:
1. CI/CD pipeline (pre-deployment checks)
2. Post-deployment verification
3. Weekly security audits
4. Incident response validation

All checks should pass with exit code 0.
Failures indicate security violations requiring immediate attention.
"""

import sys
import asyncio
from typing import List, Tuple
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import text

# ==============================================================================
# CHECK 1: SECURITY DEFINER Ownership Governance
# ==============================================================================

async def check_security_definer_ownership(session: AsyncSession) -> bool:
    """
    Verify all SECURITY DEFINER functions are owned by app_rls_executor.
    
    This prevents privilege escalation attacks through function ownership chains.
    """
    result = await session.execute(text("""
        SELECT
            proname,
            proowner::regrole,
            prosecdef
        FROM
            pg_proc
        WHERE
            pronamespace = 'app_private'::regnamespace
            AND prosecdef = true
            AND proowner::regrole::text != 'app_rls_executor';
    """))
    
    violations = result.fetchall()
    
    if violations:
        print("❌ SECURITY DEFINER OWNERSHIP VIOLATION")
        print("   The following functions are not owned by app_rls_executor:")
        for proname, owner, prosecdef in violations:
            print(f"   - {proname} (owner: {owner})")
        return False
    
    print("✅ All SECURITY DEFINER functions correctly owned by app_rls_executor")
    return True


# ==============================================================================
# CHECK 2: PUBLIC EXECUTE Privilege Revocation
# ==============================================================================

async def check_public_execute_revocation(session: AsyncSession) -> bool:
    """
    Verify PUBLIC does not have EXECUTE on sensitive functions.
    
    Functions should only be executable by app_rls_executor or explicit roles.
    """
    result = await session.execute(text("""
        SELECT
            p.proname,
            a.grantee,
            a.privilege_type
        FROM
            pg_proc p
            LEFT JOIN information_schema.role_routine_grants a
                ON p.proname = a.routine_name
                AND p.pronamespace = (
                    SELECT oid FROM pg_namespace WHERE nspname = a.routine_schema
                )
        WHERE
            p.pronamespace = 'app_private'::regnamespace
            AND (
                a.grantee = 'PUBLIC'
                OR (a.privilege_type = 'EXECUTE' AND a.grantee != 'app_rls_executor')
            );
    """))
    
    violations = result.fetchall()
    
    if violations:
        print("❌ PUBLIC EXECUTE PRIVILEGE VIOLATION")
        print("   The following functions are improperly granted:")
        for proname, grantee, priv in violations:
            print(f"   - {proname} granted to {grantee} ({priv})")
        return False
    
    print("✅ No unauthorized EXECUTE privileges found")
    return True


# ==============================================================================
# CHECK 3: search_path Hardening
# ==============================================================================

async def check_search_path_hardening(session: AsyncSession) -> bool:
    """
    Verify SECURITY DEFINER functions use minimal search_path.
    
    Safe: 'pg_catalog'
    Acceptable: 'pg_catalog, app_private'
    Unsafe: Anything broader
    """
    result = await session.execute(text("""
        SELECT
            proname,
            proconfig
        FROM
            pg_proc
        WHERE
            pronamespace = 'app_private'::regnamespace
            AND prosecdef = true
            AND NOT EXISTS (
                SELECT 1
                FROM unnest(COALESCE(proconfig, ARRAY[]::text[])) AS cfg(setting)
                WHERE setting = 'search_path=pg_catalog'
            );
    """))
    
    violations = result.fetchall()
    
    if violations:
        print("❌ SEARCH_PATH HARDENING VIOLATION")
        print("   The following functions lack proper search_path config:")
        for proname, config in violations:
            print(f"   - {proname}: {config}")
        return False
    
    print("✅ All SECURITY DEFINER functions have hardened search_path")
    return True


# ==============================================================================
# CHECK 4: Table Ownership Drift
# ==============================================================================

async def check_table_ownership_drift(session: AsyncSession) -> bool:
    """
    Verify branch_contacts tables are owned by app_rls_executor.
    
    Accidental ownership reassignment is a critical security issue.
    """
    result = await session.execute(text("""
        SELECT
            tablename,
            tableowner
        FROM
            pg_tables
        WHERE
            schemaname = 'public'
            AND tablename IN ('branch_contacts', 'branch_contacts_audit')
            AND tableowner != 'app_rls_executor';
    """))
    
    violations = result.fetchall()
    
    if violations:
        print("❌ TABLE OWNERSHIP DRIFT VIOLATION")
        print("   The following tables have incorrect ownership:")
        for tablename, owner in violations:
            print(f"   - {tablename} owned by {owner} (should be app_rls_executor)")
        return False
    
    print("✅ All branch_contacts tables correctly owned by app_rls_executor")
    return True


# ==============================================================================
# CHECK 5: Timestamp Index Detection
# ==============================================================================

async def check_no_timestamp_indexes(session: AsyncSession) -> bool:
    """
    Verify no indexes on updated_at or updated_by.
    
    Indexing timestamp metadata columns breaks HOT optimizations.
    This is a performance killer and should trigger alerts.
    """
    result = await session.execute(text("""
        SELECT
            tablename,
            indexname,
            indexdef
        FROM
            pg_indexes
        WHERE
            tablename = 'branch_contacts'
            AND (
                indexdef LIKE '%updated_at%'
                OR indexdef LIKE '%updated_by%'
            );
    """))
    
    violations = result.fetchall()
    
    if violations:
        print("❌ HOT OPTIMIZATION VIOLATION (Timestamp Indexes)")
        print("   The following indexes will destroy HOT optimization:")
        for tablename, indexname, indexdef in violations:
            print(f"   - {indexname}: {indexdef}")
        return False
    
    print("✅ No problematic timestamp indexes detected")
    return True


# ==============================================================================
# CHECK 6: RLS Force Enforcement
# ==============================================================================

async def check_rls_force_enforcement(session: AsyncSession) -> bool:
    """
    Verify RLS is FORCE enabled on sensitive tables.
    
    FORCE ROW LEVEL SECURITY prevents table owners from bypassing RLS.
    """
    result = await session.execute(text("""
        SELECT
            c.relname AS tablename,
            c.relrowsecurity AS rowsecurity,
            c.relforcerowsecurity AS forcerowsecurity
        FROM
            pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE
            n.nspname = 'public'
            AND c.relname IN ('branch_contacts', 'branch_contacts_audit')
            AND c.relkind IN ('r', 'p')
            AND (c.relrowsecurity = false OR c.relforcerowsecurity = false);
    """))
    
    violations = result.fetchall()
    
    if violations:
        print("❌ RLS ENFORCEMENT VIOLATION")
        print("   The following tables don't have FORCE RLS enabled:")
        for tablename, rls, force_rls in violations:
            print(f"   - {tablename} (RLS: {rls}, FORCE: {force_rls})")
        return False
    
    print("✅ All sensitive tables have FORCE ROW LEVEL SECURITY")
    return True


# ==============================================================================
# CHECK 7: Constraint Validation Progress
# ==============================================================================

async def check_constraint_validation_progress(session: AsyncSession) -> bool:
    """
    Report on NOT VALID constraint validation progress.
    
    Phase C deployment: all constraints should eventually validate.
    This check provides visibility into validation completion.
    """
    result = await session.execute(text("""
        SELECT
            conname,
            convalidated
        FROM
            pg_constraint
        WHERE
            conrelid = 'public.branch_contacts'::regclass
            AND contype = 'c';  -- CHECK constraints
    """))
    
    constraints = result.fetchall()
    total = len(constraints)
    valid = sum(1 for _, validated in constraints if validated)
    
    print(f"📊 CONSTRAINT VALIDATION STATUS: {valid}/{total} VALID")
    
    if valid < total:
        print(f"   {total - valid} constraints still pending validation")
        for conname, convalidated in constraints:
            status = "✅ VALID" if convalidated else "⏳ PENDING"
            print(f"   - {conname}: {status}")
    
    return True  # Not a hard failure, just informational


# ==============================================================================
# CHECK 8: Soft-Delete Resurrection Prevention
# ==============================================================================

async def check_soft_delete_resurrection_prevention(session: AsyncSession) -> bool:
    """
    Verify prevent_soft_delete_resurrection() trigger is in place.
    
    Without this, deleted_at could be set back to NULL (resurrection).
    """
    result = await session.execute(text("""
        SELECT
            tgname,
            tgfoid::regproc
        FROM
            pg_trigger
        WHERE
            tgrelid = 'public.branch_contacts'::regclass
            AND tgname = 'trg_prevent_soft_delete_resurrection';
    """))
    
    trigger = result.fetchone()
    
    if not trigger:
        print("❌ RESURRECTION PREVENTION VIOLATION")
        print("   prevent_soft_delete_resurrection trigger not found!")
        return False
    
    print("✅ Soft-delete resurrection prevention trigger is active")
    return True


# ==============================================================================
# CHECK 9: Audit Table Append-Only Enforcement
# ==============================================================================

async def check_audit_append_only(session: AsyncSession) -> bool:
    """
    Verify prevent_audit_modification() trigger prevents UPDATE/DELETE on audit table.
    
    Audit table must be strictly append-only.
    """
    result = await session.execute(text("""
        SELECT
            tgname,
            tgfoid::regproc
        FROM
            pg_trigger
        WHERE
            tgrelid = 'public.branch_contacts_audit'::regclass
            AND tgname = 'trg_prevent_audit_update';
    """))
    
    trigger = result.fetchone()
    
    if not trigger:
        print("❌ AUDIT IMMUTABILITY VIOLATION")
        print("   prevent_audit_modification trigger not found!")
        return False
    
    print("✅ Audit table append-only enforcement is active")
    return True


# ==============================================================================
# CHECK 10: Primary Contact Invariant Triggers
# ==============================================================================

async def check_primary_contact_triggers(session: AsyncSession) -> bool:
    """
    Verify all three primary contact invariant triggers are in place.
    
    INSERT, UPDATE, DELETE handlers maintain "always at least one primary" invariant.
    """
    required_triggers = [
        'trg_ensure_primary_contact_insert',
        'trg_ensure_primary_contact_update',
        'trg_ensure_primary_contact_delete',
    ]
    
    result = await session.execute(text(f"""
        SELECT
            tgname
        FROM
            pg_trigger
        WHERE
            tgrelid = 'public.branch_contacts'::regclass
            AND tgname = ANY(ARRAY{required_triggers});
    """))
    
    found_triggers = [row[0] for row in result.fetchall()]
    missing = set(required_triggers) - set(found_triggers)
    
    if missing:
        print("❌ PRIMARY CONTACT INVARIANT VIOLATION")
        print(f"   Missing triggers: {', '.join(missing)}")
        return False
    
    print("✅ All primary contact invariant triggers are active")
    return True


# ==============================================================================
# CHECK 11: Advisory Lock Function Presence
# ==============================================================================

async def check_advisory_lock_function(session: AsyncSession) -> bool:
    """
    Verify process_primary_contact_batch() uses native hashing.
    
    This is harder to check statically, but we can verify the function exists.
    """
    result = await session.execute(text("""
        SELECT
            proname
        FROM
            pg_proc
        WHERE
            pronamespace = 'app_private'::regnamespace
            AND proname = 'process_primary_contact_batch';
    """))
    
    func = result.fetchone()
    
    if not func:
        print("❌ ADVISORY LOCK FUNCTION MISSING")
        print("   process_primary_contact_batch not found!")
        return False
    
    print("✅ Advisory lock batch processor function is present")
    return True


# ==============================================================================
# CHECK 12: Index Coverage
# ==============================================================================

async def check_index_coverage(session: AsyncSession) -> bool:
    """
    Verify critical indices for performance exist.
    
    Missing indices will cause performance degradation.
    """
    required_indices = [
        'ix_contacts_org_branch_active',
        'ix_primary_contact_lookup',
        'ix_branch_contacts_primary_ordered',
        'uq_primary_contact_guard_idx',
        'ix_audit_branch_contacts_ordered',
    ]
    
    result = await session.execute(text(f"""
        SELECT
            indexname
        FROM
            pg_indexes
        WHERE
            tablename IN ('branch_contacts', 'branch_contacts_audit')
            AND indexname = ANY(ARRAY{required_indices});
    """))
    
    found_indices = [row[0] for row in result.fetchall()]
    missing = set(required_indices) - set(found_indices)
    
    if missing:
        print("⚠️  MISSING PERFORMANCE INDICES")
        print(f"   Consider adding: {', '.join(missing)}")
        return True  # Warning, not failure
    
    print("✅ All critical indices are present")
    return True


# ==============================================================================
# MAIN VALIDATION ORCHESTRATOR
# ==============================================================================

async def run_all_governance_checks(db_url: str) -> Tuple[int, List[str]]:
    """
    Run all governance checks and return exit code.
    
    Args:
        db_url: Database connection URL (async SQLAlchemy format)
    
    Returns:
        (exit_code, failures_list)
        - exit_code: 0 = all checks passed, 1 = at least one check failed
        - failures_list: list of failed check names
    """
    engine = create_async_engine(db_url, echo=False)
    
    checks = [
        ("SECURITY_DEFINER_OWNERSHIP", check_security_definer_ownership),
        ("PUBLIC_EXECUTE_REVOCATION", check_public_execute_revocation),
        ("SEARCH_PATH_HARDENING", check_search_path_hardening),
        ("TABLE_OWNERSHIP_DRIFT", check_table_ownership_drift),
        ("TIMESTAMP_INDEX_DETECTION", check_no_timestamp_indexes),
        ("RLS_FORCE_ENFORCEMENT", check_rls_force_enforcement),
        ("CONSTRAINT_VALIDATION", check_constraint_validation_progress),
        ("SOFT_DELETE_RESURRECTION", check_soft_delete_resurrection_prevention),
        ("AUDIT_APPEND_ONLY", check_audit_append_only),
        ("PRIMARY_CONTACT_TRIGGERS", check_primary_contact_triggers),
        ("ADVISORY_LOCK_FUNCTION", check_advisory_lock_function),
        ("INDEX_COVERAGE", check_index_coverage),
    ]
    
    failures = []
    
    async with AsyncSession(engine) as session:
        for check_name, check_func in checks:
            try:
                print(f"\n[{check_name}]")
                result = await check_func(session)
                if not result:
                    failures.append(check_name)
            except Exception as e:
                print(f"❌ ERROR: {e}")
                failures.append(check_name)
    
    await engine.dispose()
    
    print("\n" + "="*80)
    if failures:
        print(f"❌ {len(failures)} CHECKS FAILED")
        for failure in failures:
            print(f"   - {failure}")
        return 1, failures
    else:
        print("✅ ALL GOVERNANCE CHECKS PASSED")
        return 0, []


# ==============================================================================
# CLI INTERFACE
# ==============================================================================

async def main():
    """CLI entry point for governance validation"""
    import os
    
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://user:password@localhost/gymflow_db"
    )
    
    exit_code, failures = await run_all_governance_checks(db_url)
    sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())

"""
Branch Contacts: Production Observability & Monitoring Queries

Use these queries to monitor:
1. Lock contention and deadlock patterns
2. Long-running transactions
3. Concurrent write load
4. Partition health
5. Index bloat
6. RLS policy effectiveness

Deploy to Datadog/Prometheus via SQL agent.
"""

# ==============================================================================
# SECTION 1: Lock Contention Monitoring
# ==============================================================================

LOCK_WAITS_QUERY = """
-- Lock wait analysis: transactions waiting on branch_contacts locks
-- Use this to detect primary contact swap contention
SELECT
    blocked_locks.locktype,
    blocked_locks.relation::regclass AS blocked_table,
    blocked_locks.page,
    blocked_locks.tuple,
    blocked_locks.virtualxid,
    blocked_locks.transactionid,
    blocked_locks.classid,
    blocked_locks.objid,
    blocked_locks.objsubid,
    blocking_locks.pid AS blocking_pid,
    blocked_locks.pid AS blocked_pid,
    blocked_activity.usename AS blocked_user,
    blocked_activity.application_name AS blocked_app,
    blocked_activity.wait_event_type,
    blocked_activity.wait_event,
    now() - pg_stat_activity.query_start AS blocked_duration,
    blocking_activity.query AS blocking_query,
    blocking_activity.wait_event_type AS blocking_wait_event_type
FROM
    pg_catalog.pg_locks blocked_locks
    JOIN pg_catalog.pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
    JOIN pg_catalog.pg_locks blocking_locks ON blocking_locks.locktype = blocked_locks.locktype
        AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
        AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
        AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
        AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
        AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
        AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
        AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
        AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
        AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
        AND blocking_locks.pid != blocked_locks.pid
    JOIN pg_catalog.pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
WHERE
    NOT blocked_locks.granted
    AND blocked_locks.relation = 'public.branch_contacts'::regclass
ORDER BY
    blocking_activity.query_start DESC,
    blocked_activity.query_start DESC;
"""

ADVISORY_LOCKS_QUERY = """
-- Monitor advisory locks used by primary contact swaps
-- Shows which branch_ids are currently locked
SELECT
    locktype,
    objid,
    'branch_id: ' || objid::regclass AS resource,
    pid,
    usename,
    application_name,
    state,
    query,
    now() - query_start AS query_duration
FROM
    pg_catalog.pg_locks
    JOIN pg_catalog.pg_stat_activity USING (pid)
WHERE
    locktype = 'advisory'
    AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY
    query_start DESC;
"""

LOCK_TIMEOUT_EVENTS = """
-- Slow branch_contacts statements that may indicate lock waits/timeouts.
-- Pair with application logs for exact SQLSTATE 55P03 counts.
SELECT
    queryid,
    calls,
    mean_exec_time,
    max_exec_time,
    rows,
    query
FROM
    pg_stat_statements
WHERE
    query LIKE '%branch_contacts%'
    AND calls > 0
    AND mean_exec_time > 5000  -- Transactions taking > 5 seconds
ORDER BY
    mean_exec_time DESC
LIMIT 50;
"""

# ==============================================================================
# SECTION 2: Deadlock Monitoring
# ==============================================================================

DEADLOCK_MONITORING = """
-- Deadlock statistics by table
-- Monitor for increasing deadlock rates
SELECT
    schemaname,
    relname,
    n_tup_ins,
    n_tup_upd,
    n_tup_del,
    n_live_tup,
    n_dead_tup,
    last_vacuum,
    last_autovacuum,
    last_analyze,
    last_autoanalyze
FROM
    pg_stat_user_tables
WHERE
    relname IN ('branch_contacts', 'branch_contacts_audit')
ORDER BY
    n_tup_upd DESC;
"""

LONG_RUNNING_TRANSACTIONS = """
-- Identify transactions holding locks for too long
-- Primary contact swaps should complete in < 50ms
SELECT
    pid,
    usename,
    application_name,
    state,
    state_change,
    now() - state_change AS duration,
    query,
    backend_xmin,
    backend_xid
FROM
    pg_stat_activity
WHERE
    datname = current_database()
    AND query LIKE '%branch_contacts%'
    AND state != 'idle'
    AND now() - state_change > INTERVAL '1 second'
ORDER BY
    state_change ASC;
"""

# ==============================================================================
# SECTION 3: Write Load & Throughput
# ==============================================================================

BRANCH_CONTACTS_WRITE_LOAD = """
-- Measure current write load on branch_contacts
-- Useful for capacity planning and load testing
SELECT
    schemaname,
    relname,
    seq_scan,
    seq_tup_read,
    idx_scan,
    idx_tup_fetch,
    n_tup_ins,
    n_tup_upd,
    n_tup_del,
    n_live_tup,
    n_dead_tup,
    round(
        100 * n_dead_tup / NULLIF((n_live_tup + n_dead_tup)::numeric, 0),
        2
    ) AS dead_tuple_ratio,
    last_vacuum,
    last_autovacuum
FROM
    pg_stat_user_tables
WHERE
    relname = 'branch_contacts'
ORDER BY
    n_tup_upd DESC;
"""

AUDIT_INSERT_RATE = """
-- Measure audit table insert rate
-- Should closely track primary contact mutation rate
SELECT
    'branch_contacts_audit' AS table_name,
    t.n_tup_ins AS total_inserts,
    round(
        t.n_tup_ins / GREATEST(EXTRACT(epoch FROM (now() - d.stats_reset)), 1),
        2
    ) AS inserts_per_second,
    t.last_autoanalyze,
    t.last_vacuum
FROM
    pg_stat_user_tables t
    JOIN pg_stat_database d ON d.datname = current_database()
WHERE
    t.relname = 'branch_contacts_audit'
LIMIT 1;
"""

# ==============================================================================
# SECTION 4: Index Performance
# ==============================================================================

INDEX_USAGE_ANALYSIS = """
-- Index usage on branch_contacts
-- Identify missing or underutilized indices
SELECT
    schemaname,
    tablename,
    indexname,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch,
    CASE
        WHEN idx_tup_read = 0 THEN 'UNUSED'
        WHEN idx_tup_read = idx_tup_fetch THEN 'PERFECT'
        ELSE 'PARTIAL'
    END AS efficiency,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM
    pg_stat_user_indexes
WHERE
    tablename IN ('branch_contacts', 'branch_contacts_audit')
ORDER BY
    idx_scan DESC;
"""

INDEX_BLOAT_CHECK = """
-- Check index bloat on branch_contacts indices
-- Alert if bloat > 15%
WITH index_bloat AS (
    SELECT
        current_database() AS db,
        schemaname,
        tablename,
        indexname,
        ROUND(100 * (pg_relation_size(indexrelid) -
            pg_relation_size(indexrelid, 'main')) /
            pg_relation_size(indexrelid)::numeric) AS bloat_ratio
    FROM
        pg_stat_user_indexes
    WHERE
        tablename IN ('branch_contacts', 'branch_contacts_audit')
)
SELECT
    db,
    schemaname,
    tablename,
    indexname,
    bloat_ratio,
    CASE
        WHEN bloat_ratio > 20 THEN 'CRITICAL'
        WHEN bloat_ratio > 15 THEN 'HIGH'
        WHEN bloat_ratio > 10 THEN 'MEDIUM'
        ELSE 'NORMAL'
    END AS severity
FROM
    index_bloat
ORDER BY
    bloat_ratio DESC;
"""

# ==============================================================================
# SECTION 5: Constraint & RLS Monitoring
# ==============================================================================

NOT_VALID_CONSTRAINTS = """
-- Monitor NOT VALID constraints waiting for validation
-- Phase C deployment: these should eventually all become VALID
SELECT
    table_schema,
    table_name,
    constraint_name,
    constraint_type,
    (SELECT convalidated FROM pg_constraint WHERE conname = constraint_name) AS is_valid
FROM
    information_schema.table_constraints
WHERE
    table_schema = 'public'
    AND table_name IN ('branch_contacts', 'branch_contacts_audit')
    AND constraint_type = 'CHECK'
ORDER BY
    table_name, constraint_name;
"""

RLS_POLICY_EFFECTIVENESS = """
-- Verify RLS policies are actually protecting data
-- Should show org_id filters being applied
SELECT
    schemaname,
    tablename,
    policyname,
    permissive,
    roles,
    qual
FROM
    pg_policies
WHERE
    tablename IN ('branch_contacts', 'branch_contacts_audit')
ORDER BY
    tablename, policyname;
"""

# ==============================================================================
# SECTION 6: Partition Health
# ==============================================================================

PARTITION_SIZES = """
-- Monitor partition sizes
-- Alert if any partition > 500MB or growing too fast
SELECT
    t.schemaname,
    t.tablename,
    pg_size_pretty(pg_total_relation_size(t.schemaname||'.'||t.tablename)) AS total_size,
    pg_size_pretty(pg_relation_size(t.schemaname||'.'||t.tablename)) AS table_size,
    pg_size_pretty(pg_indexes_size(t.schemaname||'.'||t.tablename)) AS index_size,
    s.n_live_tup,
    s.n_dead_tup,
    s.last_autovacuum
FROM
    pg_tables t
    JOIN pg_stat_user_tables s
        ON s.schemaname = t.schemaname
        AND s.relname = t.tablename
WHERE
    t.tablename LIKE 'branch_contacts_audit_%'
ORDER BY
    pg_total_relation_size(t.schemaname||'.'||t.tablename) DESC;
"""

PARTITION_DEFAULT_BLOAT = """
-- Check default partition for bloat
-- If default partition accumulates data, partitioning isn't working
SELECT
    schemaname,
    relname AS tablename,
    n_live_tup,
    n_dead_tup,
    round(
        100 * n_dead_tup / NULLIF((n_live_tup + n_dead_tup)::numeric, 0),
        2
    ) AS dead_ratio,
    last_autovacuum
FROM
    pg_stat_user_tables
WHERE
    relname = 'branch_contacts_audit_default'
ORDER BY
    n_live_tup DESC;
"""

# ==============================================================================
# SECTION 7: Soft-Delete Audit
# ==============================================================================

SOFT_DELETE_COVERAGE = """
-- Verify soft-delete is working (no hard deletes)
-- Should show INSERT/UPDATE but zero DELETE operations
SELECT
    action,
    COUNT(*) AS event_count,
    MAX(changed_at) AS latest_event
FROM
    public.branch_contacts_audit
WHERE
    changed_at > now() - INTERVAL '24 hours'
GROUP BY
    action
ORDER BY
    action;
"""

DELETED_CONTACTS_QUERY = """
-- Find recently deleted contacts
-- Verify deletion metadata is complete
SELECT
    id,
    org_id,
    branch_id,
    contact_kind,
    is_active,
    deleted_at,
    deleted_by,
    created_at,
    now() - deleted_at AS time_since_deletion
FROM
    public.branch_contacts
WHERE
    deleted_at IS NOT NULL
ORDER BY
    deleted_at DESC
LIMIT 100;
"""

RESURRECTION_ATTEMPTS = """
-- Find any attempted resurrections (should be zero)
-- These would show deleted_at being set to NULL
SELECT
    branch_contact_id,
    changed_at,
    changed_by,
    changed_fields,
    action
FROM
    public.branch_contacts_audit
WHERE
    changed_fields ? 'deleted_at'
    AND changed_fields->'deleted_at' ? 'n'
    AND changed_fields->'deleted_at'->'n' = 'null'::jsonb
    AND action = 'UPDATE'
ORDER BY
    changed_at DESC
LIMIT 100;
"""

# ==============================================================================
# SECTION 8: RLS Breach Detection
# ==============================================================================

CROSS_ORG_QUERY_TEST = """
-- TEST: Verify RLS prevents cross-tenant access
-- Set app.current_org_id to different value and run this query
-- Should return 0 rows if RLS is working
SET app.current_org_id = '00000000-0000-0000-0000-000000000001';

SELECT COUNT(*) AS should_be_zero
FROM public.branch_contacts
WHERE org_id != NULLIF(current_setting('app.current_org_id'), '')::UUID;
"""

SECURITY_DEFINER_AUDIT = """
-- Verify SECURITY DEFINER functions are properly owned
-- All should be owned by app_rls_executor
SELECT
    proname,
    proowner::regrole,
    prosecdef,
    CASE
        WHEN proowner::regrole::text = 'app_rls_executor' THEN 'OK'
        ELSE 'VIOLATION'
    END AS ownership_status
FROM
    pg_proc
WHERE
    pronamespace = 'app_private'::regnamespace
    AND prosecdef = true
ORDER BY
    proname;
"""

# ==============================================================================
# SECTION 9: DATADOG/PROMETHEUS INTEGRATION
# ==============================================================================

"""
Datadog Configuration Examples:

1. Lock Contention Metric:
   SELECT COUNT(*) as lock_waits FROM (
       -- LOCK_WAITS_QUERY above
   )

2. Deadlock Rate (per minute):
   SELECT COUNT(*) FROM branch_contacts_audit
   WHERE action = 'DEADLOCK' AND changed_at > now() - interval '1 minute'

3. Write Latency (p99):
   SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY duration)
   FROM query_performance
   WHERE query LIKE '%branch_contacts%'

4. Partition Size:
   SELECT pg_total_relation_size(schemaname||'.'||tablename)
   FROM pg_tables WHERE tablename LIKE 'branch_contacts_audit_%'

5. RLS Effectiveness (rows filtered):
   SELECT COUNT(*) FROM branch_contacts
   WHERE org_id != current_org_id  -- Should be 0 after RLS

Prometheus Config:
  - scrape_interval: 30s
  - queries_timeout: 10s
  - alerts: threshold > 100ms for lock waits
"""

# ==============================================================================
# EXPORTS FOR APPLICATION
# ==============================================================================

OBSERVABILITY_QUERIES = {
    "lock_waits": LOCK_WAITS_QUERY,
    "advisory_locks": ADVISORY_LOCKS_QUERY,
    "lock_timeouts": LOCK_TIMEOUT_EVENTS,
    "deadlock_monitoring": DEADLOCK_MONITORING,
    "long_running_transactions": LONG_RUNNING_TRANSACTIONS,
    "write_load": BRANCH_CONTACTS_WRITE_LOAD,
    "audit_insert_rate": AUDIT_INSERT_RATE,
    "index_usage": INDEX_USAGE_ANALYSIS,
    "index_bloat": INDEX_BLOAT_CHECK,
    "not_valid_constraints": NOT_VALID_CONSTRAINTS,
    "rls_policies": RLS_POLICY_EFFECTIVENESS,
    "partition_sizes": PARTITION_SIZES,
    "partition_default": PARTITION_DEFAULT_BLOAT,
    "soft_delete_coverage": SOFT_DELETE_COVERAGE,
    "deleted_contacts": DELETED_CONTACTS_QUERY,
    "resurrection_attempts": RESURRECTION_ATTEMPTS,
    "cross_org_query_test": CROSS_ORG_QUERY_TEST,
    "security_definer_audit": SECURITY_DEFINER_AUDIT,
}

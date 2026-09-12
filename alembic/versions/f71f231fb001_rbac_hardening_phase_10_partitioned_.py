"""RBAC hardening phase 10 — audit-ledger preservation checkpoint.

Revision ID: f71f231fb001
Revises: fbcddf8779b8
Create Date: 2026-05-23 16:03:41.630268

The canonical partitioned audit ledger is established by predecessor
revisions. This revision validates that predecessor contract without
replacing the parent relation, partitions, rows, policies, triggers,
constraints, indexes, sequence, functions, privileges, or ownership.

Both migration directions are deliberately validation-only.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f71f231fb001"
down_revision: Union[str, Sequence[str], None] = "fbcddf8779b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _validate_predecessor_audit_contract() -> None:
    op.execute(
        r"""
        DO $f71_audit_contract$
        DECLARE
            v_parent_oid OID :=
                to_regclass('public.branch_audit_log');

            v_sequence_oid OID :=
                to_regclass('public.branch_audit_log_seq');

            v_parent_kind TEXT;
            v_parent_owner TEXT;
            v_rls_enabled BOOLEAN;
            v_rls_forced BOOLEAN;
            v_partition_key TEXT;

            v_definition TEXT;
            v_generated TEXT;
            v_bound TEXT;
            v_child_owner TEXT;

            v_parent_trigger_oid OID;
            v_function_oid OID;

            v_partition_count INTEGER;
            v_trigger_count INTEGER;
            v_policy_count INTEGER;

            v_missing TEXT;
            v_partition RECORD;
            v_function RECORD;

            v_role_guidance CONSTANT TEXT :=
                'security/cluster_role_bootstrap';
        BEGIN
            -- Required roles are externally managed principals.
            -- Alembic validates but never creates or repairs them.
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = 'audit_writer'
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: audit_writer role is missing; '
                    'provision via %', v_role_guidance;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = 'app_security_owner'
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: app_security_owner role is missing; '
                    'provision via %', v_role_guidance;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = 'app_rls_executor'
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: app_rls_executor role is missing; '
                    'provision via %', v_role_guidance;
            END IF;

            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = 'audit_writer'
                  AND (
                      rolsuper
                      OR rolcreatedb
                      OR rolcreaterole
                      OR rolreplication
                      OR rolbypassrls
                      OR rolcanlogin
                      OR rolinherit
                  )
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: audit_writer attributes are unsafe; '
                    'repair via %', v_role_guidance;
            END IF;

            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = 'app_security_owner'
                  AND (
                      rolsuper
                      OR rolcreatedb
                      OR rolcreaterole
                      OR rolreplication
                      OR rolbypassrls
                      OR rolcanlogin
                      OR rolinherit
                  )
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: app_security_owner attributes are unsafe; '
                    'repair via %', v_role_guidance;
            END IF;

            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = 'app_rls_executor'
                  AND (
                      rolsuper
                      OR rolcreatedb
                      OR rolcreaterole
                      OR rolreplication
                      OR rolbypassrls
                      OR rolcanlogin
                      OR rolinherit
                  )
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: app_rls_executor attributes are unsafe; '
                    'repair via %', v_role_guidance;
            END IF;

            -- Parent identity and partition/RLS contract.
            IF v_parent_oid IS NULL THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'branch_audit_log is missing';
            END IF;

            SELECT
                relation.relkind::TEXT,
                pg_catalog.pg_get_userbyid(
                    relation.relowner
                ),
                relation.relrowsecurity,
                relation.relforcerowsecurity
            INTO
                v_parent_kind,
                v_parent_owner,
                v_rls_enabled,
                v_rls_forced
            FROM pg_catalog.pg_class AS relation
            WHERE relation.oid = v_parent_oid;

            IF v_parent_kind <> 'p' THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'branch_audit_log is not partitioned';
            END IF;

            IF v_parent_owner <> current_user::TEXT THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'parent owner % differs from migration identity %',
                    v_parent_owner,
                    current_user;
            END IF;

            IF NOT v_rls_enabled OR NOT v_rls_forced THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'RLS must be enabled and forced';
            END IF;

            v_partition_key :=
                pg_catalog.pg_get_partkeydef(v_parent_oid);

            IF v_partition_key <> 'RANGE (created_at)' THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'unexpected partition key %',
                    v_partition_key;
            END IF;

            -- Exact predecessor columns, types, and nullability.
            FOR v_missing IN
                SELECT requirement.column_name
                FROM (
                    VALUES
                        ('id', 'uuid', TRUE),
                        ('branch_id', 'uuid', TRUE),
                        ('org_id', 'uuid', TRUE),
                        ('actor_id', 'uuid', TRUE),
                        ('action', 'text', TRUE),
                        ('reason', 'text', FALSE),
                        ('diff', 'jsonb', FALSE),
                        (
                            'created_at',
                            'timestamp with time zone',
                            TRUE
                        ),
                        ('audit_sequence', 'bigint', TRUE),
                        ('event_id', 'uuid', TRUE),
                        ('request_id', 'uuid', FALSE),
                        ('region_id', 'uuid', FALSE),
                        ('actor_snapshot', 'jsonb', TRUE),
                        ('actor_permissions', 'jsonb', TRUE),
                        (
                            'action_category',
                            'character varying(32)',
                            FALSE
                        ),
                        (
                            'reason_code',
                            'character varying(32)',
                            TRUE
                        ),
                        (
                            'previous_event_hash',
                            'character varying(64)',
                            FALSE
                        ),
                        (
                            'event_hash',
                            'character varying(64)',
                            FALSE
                        ),
                        ('hash_key_version', 'smallint', TRUE),
                        ('policy_version', 'integer', TRUE),
                        (
                            'app_version',
                            'character varying(32)',
                            FALSE
                        ),
                        ('deployment_id', 'uuid', FALSE)
                ) AS requirement(
                    column_name,
                    data_type,
                    is_not_null
                )
                LEFT JOIN pg_catalog.pg_attribute AS attribute
                  ON attribute.attrelid = v_parent_oid
                 AND attribute.attname =
                     requirement.column_name
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
                WHERE attribute.attname IS NULL
                   OR pg_catalog.format_type(
                          attribute.atttypid,
                          attribute.atttypmod
                      ) <> requirement.data_type
                   OR attribute.attnotnull IS DISTINCT FROM
                      requirement.is_not_null
            LOOP
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'column contract mismatch for %',
                    v_missing;
            END LOOP;

            SELECT
                attribute.attgenerated::TEXT,
                pg_catalog.pg_get_expr(
                    default_value.adbin,
                    default_value.adrelid
                )
            INTO
                v_generated,
                v_definition
            FROM pg_catalog.pg_attribute AS attribute
            LEFT JOIN pg_catalog.pg_attrdef AS default_value
              ON default_value.adrelid = attribute.attrelid
             AND default_value.adnum = attribute.attnum
            WHERE attribute.attrelid = v_parent_oid
              AND attribute.attname = 'action_category'
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped;

            IF v_generated <> 's'
               OR v_definition IS NULL
               OR position(
                    'split_part' IN lower(v_definition)
               ) = 0
            THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'action_category generation is invalid';
            END IF;

            -- Dedicated sequence and exact predecessor defaults.
            IF v_sequence_oid IS NULL THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'branch_audit_log_seq is missing';
            END IF;

            SELECT pg_catalog.pg_get_expr(
                default_value.adbin,
                default_value.adrelid
            )
            INTO v_definition
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_attrdef AS default_value
              ON default_value.adrelid = attribute.attrelid
             AND default_value.adnum = attribute.attnum
            WHERE attribute.attrelid = v_parent_oid
              AND attribute.attname = 'audit_sequence';

            IF v_definition IS NULL
               OR position(
                    'nextval' IN lower(v_definition)
               ) = 0
               OR position(
                    'branch_audit_log_seq'
                    IN lower(v_definition)
               ) = 0
            THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'audit_sequence default is invalid';
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_depend AS dependency
                JOIN pg_catalog.pg_attribute AS attribute
                  ON attribute.attrelid =
                     dependency.refobjid
                 AND attribute.attnum =
                     dependency.refobjsubid
                WHERE dependency.objid = v_sequence_oid
                  AND dependency.refobjid = v_parent_oid
                  AND dependency.deptype IN ('a', 'i')
                  AND attribute.attname =
                      'audit_sequence'
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'sequence ownership dependency is missing';
            END IF;

            FOR v_missing IN
                SELECT requirement.column_name
                FROM (
                    VALUES
                        ('actor_snapshot', '{}'),
                        ('actor_permissions', '{}'),
                        ('reason_code', 'unspecified')
                ) AS requirement(
                    column_name,
                    expected_fragment
                )
                LEFT JOIN pg_catalog.pg_attribute AS attribute
                  ON attribute.attrelid = v_parent_oid
                 AND attribute.attname =
                     requirement.column_name
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
                LEFT JOIN pg_catalog.pg_attrdef AS default_value
                  ON default_value.adrelid =
                     attribute.attrelid
                 AND default_value.adnum =
                     attribute.attnum
                WHERE default_value.oid IS NULL
                   OR position(
                          requirement.expected_fragment
                          IN lower(
                              pg_catalog.pg_get_expr(
                                  default_value.adbin,
                                  default_value.adrelid
                              )
                          )
                      ) = 0
            LOOP
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'default contract mismatch for %',
                    v_missing;
            END LOOP;

            -- Predecessor constraints.
            FOR v_missing IN
                SELECT required.constraint_name
                FROM (
                    VALUES
                        ('branch_audit_log_pkey'),
                        ('fk_audit_branch'),
                        ('chk_reason_on_destructive'),
                        ('chk_prev_hash_chain')
                ) AS required(constraint_name)
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_constraint
                    AS constraint_record
                    WHERE constraint_record.conrelid =
                          v_parent_oid
                      AND constraint_record.conname =
                          required.constraint_name
                )
            LOOP
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'constraint % is missing',
                    v_missing;
            END LOOP;

            SELECT lower(
                pg_catalog.pg_get_constraintdef(
                    constraint_record.oid,
                    TRUE
                )
            )
            INTO v_definition
            FROM pg_catalog.pg_constraint
            AS constraint_record
            WHERE constraint_record.conrelid =
                  v_parent_oid
              AND constraint_record.conname =
                  'chk_prev_hash_chain';

            IF v_definition IS NULL
               OR position(
                    'event_hash is null'
                    IN v_definition
               ) = 0
               OR position(
                    'previous_event_hash is not null'
                    IN v_definition
               ) = 0
               OR position(
                    'system.bootstrap'
                    IN v_definition
               ) = 0
            THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'hash-chain compatibility constraint is invalid';
            END IF;

            -- Required monthly partitions. Extra future partitions are valid.
            FOR v_partition IN
                SELECT *
                FROM (
                    VALUES
                        (
                            'branch_audit_log_y2026_m05',
                            '2026-05-01',
                            '2026-06-01'
                        ),
                        (
                            'branch_audit_log_y2026_m06',
                            '2026-06-01',
                            '2026-07-01'
                        ),
                        (
                            'branch_audit_log_y2026_m07',
                            '2026-07-01',
                            '2026-08-01'
                        ),
                        (
                            'branch_audit_log_y2026_m08',
                            '2026-08-01',
                            '2026-09-01'
                        )
                ) AS partition_contract(
                    partition_name,
                    start_date,
                    end_date
                )
            LOOP
                v_bound := NULL;
                v_child_owner := NULL;

                SELECT
                    pg_catalog.pg_get_expr(
                        child.relpartbound,
                        child.oid
                    ),
                    pg_catalog.pg_get_userbyid(
                        child.relowner
                    )
                INTO
                    v_bound,
                    v_child_owner
                FROM pg_catalog.pg_inherits AS inheritance
                JOIN pg_catalog.pg_class AS child
                  ON child.oid = inheritance.inhrelid
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid =
                     child.relnamespace
                WHERE inheritance.inhparent =
                      v_parent_oid
                  AND namespace.nspname = 'public'
                  AND child.relname =
                      v_partition.partition_name;

                IF v_bound IS NULL
                   OR position(
                        v_partition.start_date IN v_bound
                      ) = 0
                   OR position(
                        v_partition.end_date IN v_bound
                      ) = 0
                THEN
                    RAISE EXCEPTION
                        'f71 audit preflight failed: '
                        'partition % is missing or has wrong bounds',
                        v_partition.partition_name;
                END IF;

                IF v_child_owner <> v_parent_owner THEN
                    RAISE EXCEPTION
                        'f71 audit preflight failed: '
                        'partition % has owner %, expected %',
                        v_partition.partition_name,
                        v_child_owner,
                        v_parent_owner;
                END IF;
            END LOOP;

            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_inherits AS inheritance
                JOIN pg_catalog.pg_class AS child
                  ON child.oid = inheritance.inhrelid
                WHERE inheritance.inhparent =
                      v_parent_oid
                  AND pg_catalog.pg_get_userbyid(
                        child.relowner
                      ) <> v_parent_owner
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'one or more future partitions have wrong ownership';
            END IF;

            FOR v_missing IN
                SELECT required.index_name
                FROM (
                    VALUES
                        ('ix_audit_event_id'),
                        ('ix_audit_org_sequence'),
                        ('ix_audit_org_category')
                ) AS required(index_name)
                WHERE to_regclass(
                    'public.' || required.index_name
                ) IS NULL
            LOOP
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'index % is missing',
                    v_missing;
            END LOOP;

            -- Exact predecessor tenant-policy inventory.
            --
            -- Two approved fail-closed policies exist in the historical chain:
            --
            -- 1. Legacy tenant_isolation_audit:
            --      current_setting(..., true)::uuid. A missing tenant setting
            --      produces NULL, which cannot match an org row; malformed
            --      UUID text is rejected by the cast.
            --
            -- 2. Revision-0026 tenant_isolation_audit_log:
            --      current_setting(..., false)::uuid in USING and WITH CHECK.
            --      Missing tenant context raises immediately; malformed UUID
            --      text fails the UUID cast. Both are fail-closed.
            --
            -- Do not collapse "fail closed" into one implementation token.
            SELECT count(*)
            INTO v_policy_count
            FROM pg_catalog.pg_policy AS policy_record
            WHERE policy_record.polrelid = v_parent_oid
              AND policy_record.polname IN (
                  'tenant_isolation_audit',
                  'tenant_isolation_audit_log'
              );

            IF v_policy_count <> 2 THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'required tenant policies are missing';
            END IF;

            -- Legacy policy: USING only. Validate the historically approved
            -- current_setting(..., true)::uuid semantics without requiring
            -- optional hardening wrappers that the predecessor never created.
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_policy AS policy_record
                CROSS JOIN LATERAL (
                    SELECT regexp_replace(
                        lower(
                            pg_catalog.pg_get_expr(
                                policy_record.polqual,
                                policy_record.polrelid
                            )
                        ),
                        '[[:space:]]+',
                        '',
                        'g'
                    ) AS expression_text
                ) AS normalized
                WHERE policy_record.polrelid = v_parent_oid
                  AND policy_record.polname =
                      'tenant_isolation_audit'
                  AND (
                      policy_record.polqual IS NULL
                      OR position(
                           'org_id='
                           IN normalized.expression_text
                         ) = 0
                      OR position(
                           'app.current_org_id'
                           IN normalized.expression_text
                         ) = 0
                      OR position(
                           'current_setting'
                           IN normalized.expression_text
                         ) = 0
                      OR position(
                           ',true)'
                           IN normalized.expression_text
                         ) = 0
                      OR position(
                           '::uuid'
                           IN normalized.expression_text
                         ) = 0
                      OR position(
                           ' or '
                           IN lower(
                               pg_catalog.pg_get_expr(
                                   policy_record.polqual,
                                   policy_record.polrelid
                               )
                           )
                         ) > 0
                  )
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'legacy tenant policy is not fail-closed';
            END IF;

            -- Revision-0026 policy: strict missing_ok=false in both USING
            -- and WITH CHECK. The expressions may include PostgreSQL's
            -- deparser casts, so validate semantic tokens after whitespace
            -- normalization rather than brittle byte-for-byte text.
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_policy AS policy_record
                CROSS JOIN LATERAL (
                    SELECT
                        regexp_replace(
                            lower(
                                pg_catalog.pg_get_expr(
                                    policy_record.polqual,
                                    policy_record.polrelid
                                )
                            ),
                            '[[:space:]]+',
                            '',
                            'g'
                        ) AS using_text,
                        regexp_replace(
                            lower(
                                pg_catalog.pg_get_expr(
                                    policy_record.polwithcheck,
                                    policy_record.polrelid
                                )
                            ),
                            '[[:space:]]+',
                            '',
                            'g'
                        ) AS check_text
                ) AS normalized
                WHERE policy_record.polrelid = v_parent_oid
                  AND policy_record.polname =
                      'tenant_isolation_audit_log'
                  AND (
                      policy_record.polqual IS NULL
                      OR policy_record.polwithcheck IS NULL
                      OR position(
                           'org_id='
                           IN normalized.using_text
                         ) = 0
                      OR position(
                           'app.current_org_id'
                           IN normalized.using_text
                         ) = 0
                      OR position(
                           'current_setting'
                           IN normalized.using_text
                         ) = 0
                      OR position(
                           ',false)'
                           IN normalized.using_text
                         ) = 0
                      OR position(
                           '::uuid'
                           IN normalized.using_text
                         ) = 0
                      OR position(
                           'org_id='
                           IN normalized.check_text
                         ) = 0
                      OR position(
                           'app.current_org_id'
                           IN normalized.check_text
                         ) = 0
                      OR position(
                           'current_setting'
                           IN normalized.check_text
                         ) = 0
                      OR position(
                           ',false)'
                           IN normalized.check_text
                         ) = 0
                      OR position(
                           '::uuid'
                           IN normalized.check_text
                         ) = 0
                      OR position(
                           ' or '
                           IN lower(
                               pg_catalog.pg_get_expr(
                                   policy_record.polqual,
                                   policy_record.polrelid
                               )
                           )
                         ) > 0
                      OR position(
                           ' or '
                           IN lower(
                               pg_catalog.pg_get_expr(
                                   policy_record.polwithcheck,
                                   policy_record.polrelid
                               )
                           )
                         ) > 0
                  )
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'strict tenant policy is not fail-closed';
            END IF;

            -- Parent immutable trigger and every partition clone.
            v_function_oid := to_regprocedure(
                'app_private.'
                'raise_immutable_audit_violation()'
            );

            IF v_function_oid IS NULL THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'immutable trigger function is missing';
            END IF;

            SELECT trigger_record.oid
            INTO v_parent_trigger_oid
            FROM pg_catalog.pg_trigger AS trigger_record
            WHERE trigger_record.tgrelid = v_parent_oid
              AND trigger_record.tgname =
                  'trg_deny_audit_mutation'
              AND trigger_record.tgparentid = 0
              AND trigger_record.tgfoid =
                  v_function_oid
              AND NOT trigger_record.tgisinternal;

            IF v_parent_trigger_oid IS NULL THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'parent immutable trigger is invalid';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_inherits AS inheritance
                JOIN pg_catalog.pg_class AS child
                  ON child.oid = inheritance.inhrelid
                WHERE inheritance.inhparent =
                      v_parent_oid
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_trigger
                      AS clone_trigger
                      WHERE clone_trigger.tgrelid =
                            child.oid
                        AND clone_trigger.tgname =
                            'trg_deny_audit_mutation'
                        AND clone_trigger.tgparentid =
                            v_parent_trigger_oid
                        AND clone_trigger.tgfoid =
                            v_function_oid
                        AND NOT clone_trigger.tgisinternal
                  )
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'partition trigger clone contract is invalid';
            END IF;

            SELECT count(*)
            INTO v_partition_count
            FROM pg_catalog.pg_inherits
            WHERE inhparent = v_parent_oid;

            SELECT count(*)
            INTO v_trigger_count
            FROM pg_catalog.pg_trigger AS trigger_record
            WHERE trigger_record.tgname =
                  'trg_deny_audit_mutation'
              AND NOT trigger_record.tgisinternal
              AND (
                  trigger_record.tgrelid =
                      v_parent_oid
                  OR trigger_record.tgrelid IN (
                      SELECT inheritance.inhrelid
                      FROM pg_catalog.pg_inherits
                      AS inheritance
                      WHERE inheritance.inhparent =
                            v_parent_oid
                  )
              );

            IF v_trigger_count <> v_partition_count + 1 THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'immutable trigger inventory is not exact';
            END IF;

            IF to_regprocedure(
                'app_private.raise_immutable_violation()'
            ) IS NOT NULL THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'duplicate immutable function exists';
            END IF;

            -- Exact predecessor functions, owners, security and EXECUTE ACLs.
            FOR v_function IN
                SELECT *
                FROM (
                    VALUES
                        (
                            'app_private.'
                            'raise_immutable_audit_violation()',
                            FALSE
                        ),
                        (
                            'app_private.'
                            'org_advisory_lock_key(uuid)',
                            TRUE
                        ),
                        (
                            'app_private.append_audit_event('
                            'uuid,uuid,uuid,varchar,varchar,text,'
                            'jsonb,uuid,jsonb,jsonb,text,varchar,'
                            'varchar,uuid)',
                            TRUE
                        ),
                        (
                            'app_private.'
                            'ensure_future_partition(text,integer)',
                            FALSE
                        )
                ) AS function_contract(
                    function_identity,
                    writer_may_execute
                )
            LOOP
                v_function_oid :=
                    to_regprocedure(
                        v_function.function_identity
                    );

                IF v_function_oid IS NULL THEN
                    RAISE EXCEPTION
                        'f71 audit preflight failed: '
                        'function % is missing',
                        v_function.function_identity;
                END IF;

                IF (
                    SELECT pg_catalog.pg_get_userbyid(
                        function_record.proowner
                    )
                    FROM pg_catalog.pg_proc
                    AS function_record
                    WHERE function_record.oid =
                          v_function_oid
                ) <> 'app_security_owner' THEN
                    RAISE EXCEPTION
                        'f71 audit preflight failed: '
                        'function % has wrong owner',
                        v_function.function_identity;
                END IF;

                IF NOT (
                    SELECT function_record.prosecdef
                    FROM pg_catalog.pg_proc
                    AS function_record
                    WHERE function_record.oid =
                          v_function_oid
                ) THEN
                    RAISE EXCEPTION
                        'f71 audit preflight failed: '
                        'function % is not SECURITY DEFINER',
                        v_function.function_identity;
                END IF;

                IF position(
                    'search_path=pg_catalog'
                    IN COALESCE(
                        (
                            SELECT array_to_string(
                                function_record.proconfig,
                                ','
                            )
                            FROM pg_catalog.pg_proc
                            AS function_record
                            WHERE function_record.oid =
                                  v_function_oid
                        ),
                        ''
                    )
                ) = 0 THEN
                    RAISE EXCEPTION
                        'f71 audit preflight failed: '
                        'function % has unsafe search_path',
                        v_function.function_identity;
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_proc
                    AS function_record
                    CROSS JOIN LATERAL
                    pg_catalog.aclexplode(
                        COALESCE(
                            function_record.proacl,
                            pg_catalog.acldefault(
                                'f',
                                function_record.proowner
                            )
                        )
                    ) AS acl
                    WHERE function_record.oid =
                          v_function_oid
                      AND acl.grantee = 0
                      AND acl.privilege_type =
                          'EXECUTE'
                ) THEN
                    RAISE EXCEPTION
                        'f71 audit preflight failed: '
                        'PUBLIC can execute %',
                        v_function.function_identity;
                END IF;

                IF pg_catalog.has_function_privilege(
                    'audit_writer',
                    v_function.function_identity,
                    'EXECUTE'
                ) IS DISTINCT FROM
                   v_function.writer_may_execute
                THEN
                    RAISE EXCEPTION
                        'f71 audit preflight failed: '
                        'audit_writer EXECUTE differs for %',
                        v_function.function_identity;
                END IF;
            END LOOP;

            -- Exact capabilities established by revisions 0026 and 0029.
            IF NOT pg_catalog.has_table_privilege(
                'audit_writer',
                v_parent_oid,
                'INSERT'
            )
               OR NOT pg_catalog.has_table_privilege(
                    'audit_writer',
                    v_parent_oid,
                    'SELECT'
               )
               OR NOT pg_catalog.has_table_privilege(
                    'app_runtime',
                    v_parent_oid,
                    'SELECT'
               )
               OR NOT pg_catalog.has_table_privilege(
                    'readonly_analytics',
                    v_parent_oid,
                    'SELECT'
               )
               OR NOT pg_catalog.has_table_privilege(
                    'app_rls_executor',
                    v_parent_oid,
                    'INSERT'
               )
            THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'required table privileges are missing';
            END IF;

            IF pg_catalog.has_table_privilege(
                'audit_writer',
                v_parent_oid,
                'UPDATE'
            )
               OR pg_catalog.has_table_privilege(
                    'audit_writer',
                    v_parent_oid,
                    'DELETE'
               )
               OR pg_catalog.has_table_privilege(
                    'app_runtime',
                    v_parent_oid,
                    'INSERT'
               )
               OR pg_catalog.has_table_privilege(
                    'app_runtime',
                    v_parent_oid,
                    'UPDATE'
               )
               OR pg_catalog.has_table_privilege(
                    'app_runtime',
                    v_parent_oid,
                    'DELETE'
               )
               OR pg_catalog.has_table_privilege(
                    'readonly_analytics',
                    v_parent_oid,
                    'INSERT'
               )
               OR pg_catalog.has_table_privilege(
                    'readonly_analytics',
                    v_parent_oid,
                    'UPDATE'
               )
               OR pg_catalog.has_table_privilege(
                    'readonly_analytics',
                    v_parent_oid,
                    'DELETE'
               )
               OR pg_catalog.has_table_privilege(
                    'app_rls_executor',
                    v_parent_oid,
                    'UPDATE'
               )
               OR pg_catalog.has_table_privilege(
                    'app_rls_executor',
                    v_parent_oid,
                    'DELETE'
               )
            THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'forbidden mutable privilege exists';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class AS relation
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        relation.relacl,
                        pg_catalog.acldefault(
                            'r',
                            relation.relowner
                        )
                    )
                ) AS acl
                WHERE relation.oid = v_parent_oid
                  AND acl.grantee = 0
                  AND acl.privilege_type IN (
                      'SELECT',
                      'INSERT',
                      'UPDATE',
                      'DELETE'
                  )
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'PUBLIC table privilege exists';
            END IF;

            IF NOT pg_catalog.has_sequence_privilege(
                'app_rls_executor',
                v_sequence_oid,
                'USAGE'
            ) THEN
                RAISE EXCEPTION
                    'f71 audit preflight failed: '
                    'executor sequence USAGE is missing';
            END IF;

            RAISE NOTICE
                'f71 audit-ledger preservation contract validated';
        END
        $f71_audit_contract$;
        """
    )


def upgrade() -> None:
    _validate_predecessor_audit_contract()


def downgrade() -> None:
    _validate_predecessor_audit_contract()

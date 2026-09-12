"""Allow runtime read access to the legacy branch-transition catalog.

Revision ID: a3b4c5d6e7f8
Revises: 92a3b4c5d6e7
Create Date: 2026-08-11

The legacy ``validate_branch_transition()`` trigger executes for legitimate
``org_branch_state.branch_status`` updates and reads
``public.allowed_branch_transitions``. The reduced runtime boundary granted
read access to the newer lifecycle reference catalogs, but this older catalog
was omitted. As a result an authorized owner could reach the protected UPDATE
and then fail inside the trigger with ``permission denied``.

This revision adds only the missing read dependency. ``app_runtime`` receives
SELECT and no write/destructive privilege; auth/bootstrap roles are unchanged.
Downgrade revokes exactly the revision-owned SELECT grant.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, None] = "92a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_RUNTIME_ROLE = "app_runtime"
_REFERENCE_TABLE = "public.allowed_branch_transitions"
_FORBIDDEN = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _preflight() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            relation_owner text;
        BEGIN
            IF to_regrole('app_runtime') IS NULL THEN
                RAISE EXCEPTION 'a3b4 legacy transition ACL requires role app_runtime';
            END IF;

            IF to_regclass('public.allowed_branch_transitions') IS NULL THEN
                RAISE EXCEPTION
                    'a3b4 legacy transition ACL requires public.allowed_branch_transitions';
            END IF;

            SELECT pg_catalog.pg_get_userbyid(c.relowner)::text
              INTO relation_owner
              FROM pg_catalog.pg_class AS c
             WHERE c.oid = 'public.allowed_branch_transitions'::regclass;

            IF relation_owner <> 'migration_owner' THEN
                RAISE EXCEPTION
                    'a3b4 predecessor drift: unexpected allowed_branch_transitions owner %',
                    relation_owner;
            END IF;

            IF has_table_privilege(
                'app_runtime', 'public.allowed_branch_transitions', 'SELECT'
            ) THEN
                RAISE EXCEPTION
                    'a3b4 predecessor drift: app_runtime already has transition-catalog SELECT';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM pg_catalog.pg_class AS c
                  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                  CROSS JOIN LATERAL pg_catalog.aclexplode(
                      COALESCE(c.relacl, pg_catalog.acldefault('r', c.relowner))
                  ) AS acl
                 WHERE n.nspname = 'public'
                   AND c.relname = 'allowed_branch_transitions'
                   AND acl.grantee = 0
                   AND acl.privilege_type::text IN (
                       'SELECT', 'INSERT', 'UPDATE', 'DELETE',
                       'TRUNCATE', 'REFERENCES', 'TRIGGER'
                   )
            ) THEN
                RAISE EXCEPTION
                    'a3b4 predecessor drift: PUBLIC has unexpected transition-catalog privilege';
            END IF;
        END
        $$;
        """
    )


def _verify_forward() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT has_table_privilege(
                'app_runtime', 'public.allowed_branch_transitions', 'SELECT'
            ) THEN
                RAISE EXCEPTION
                    'a3b4 postcondition failed: app_runtime lacks transition-catalog SELECT';
            END IF;

            IF has_table_privilege(
                'app_runtime', 'public.allowed_branch_transitions', 'INSERT'
            ) OR has_table_privilege(
                'app_runtime', 'public.allowed_branch_transitions', 'UPDATE'
            ) OR has_table_privilege(
                'app_runtime', 'public.allowed_branch_transitions', 'DELETE'
            ) OR has_table_privilege(
                'app_runtime', 'public.allowed_branch_transitions', 'TRUNCATE'
            ) OR has_table_privilege(
                'app_runtime', 'public.allowed_branch_transitions', 'REFERENCES'
            ) OR has_table_privilege(
                'app_runtime', 'public.allowed_branch_transitions', 'TRIGGER'
            ) THEN
                RAISE EXCEPTION
                    'a3b4 postcondition failed: app_runtime has non-SELECT transition-catalog privilege';
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    _preflight()
    op.execute(
        "GRANT SELECT ON TABLE public.allowed_branch_transitions TO app_runtime;"
    )
    _verify_forward()


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT ON TABLE public.allowed_branch_transitions FROM app_runtime;"
    )

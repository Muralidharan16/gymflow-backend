"""organization creation idempotency

Revision ID: 2b3c4d5e6f70
Revises: 1a2b3c4d5e7f
Create Date: 2026-07-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "2b3c4d5e6f70"
down_revision: Union[str, Sequence[str], None] = "1a2b3c4d5e7f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.organization_creation_idempotency (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            operation VARCHAR(80) NOT NULL,
            idempotency_key VARCHAR(200) NOT NULL,
            request_hash_sha256 CHAR(64) NOT NULL,
            canonicalization_version SMALLINT NOT NULL DEFAULT 1,
            organization_id UUID NOT NULL,
            trusted_source VARCHAR(80) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            completed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_org_creation_idem_operation_key UNIQUE (operation, idempotency_key),
            CONSTRAINT uq_org_creation_idem_operation_org UNIQUE (operation, organization_id),
            CONSTRAINT fk_org_creation_idem_organization
                FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT,
            CONSTRAINT chk_org_creation_idem_operation CHECK (operation = 'synthetic_organization_create'),
            CONSTRAINT chk_org_creation_idem_key_format CHECK (idempotency_key ~ '^[a-z0-9:_-]{1,200}$'),
            CONSTRAINT chk_org_creation_idem_request_hash CHECK (request_hash_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_org_creation_idem_canonical_version CHECK (canonicalization_version >= 1),
            CONSTRAINT chk_org_creation_idem_trusted_source
                CHECK (trusted_source = 'finance_razorpay_test_precondition'),
            CONSTRAINT chk_org_creation_idem_completed_after_created CHECK (completed_at >= created_at)
        );
        """
    )
    op.execute(
        """
        COMMENT ON TABLE public.organization_creation_idempotency IS
        'Append-only pre-tenant idempotency evidence for internal synthetic organization creation.';
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.prevent_organization_creation_idempotency_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'organization creation idempotency evidence is immutable'
                USING ERRCODE = 'check_violation';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_organization_creation_idempotency_immutable
        BEFORE UPDATE OR DELETE ON public.organization_creation_idempotency
        FOR EACH ROW
        EXECUTE FUNCTION public.prevent_organization_creation_idempotency_mutation();
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.prevent_organization_creation_idempotency_mutation() FROM PUBLIC;")
    op.execute("REVOKE ALL ON TABLE public.organization_creation_idempotency FROM PUBLIC;")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'test_runner') THEN
                GRANT SELECT, INSERT ON TABLE public.organization_creation_idempotency TO test_runner;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'organization_creation_idempotency'
            ) AND EXISTS (SELECT 1 FROM public.organization_creation_idempotency) THEN
                RAISE EXCEPTION 'refusing to downgrade with organization creation idempotency evidence present';
            END IF;
        END $$;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_organization_creation_idempotency_immutable ON public.organization_creation_idempotency;")
    op.execute("DROP TABLE IF EXISTS public.organization_creation_idempotency;")
    op.execute("DROP FUNCTION IF EXISTS public.prevent_organization_creation_idempotency_mutation();")

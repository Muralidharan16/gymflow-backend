"""Resolve lifecycle refund-required events into authoritative refund commands.

Revision ID: zd07d8e9f0a3e
Revises: zc07d8e9f0a3d
Create Date: 2026-08-26

P4D-2 activates only the lifecycle-to-Finance obligation boundary.  A
``branch.refund_required`` event means "evaluate persisted Finance state for a
possible refund obligation"; it is not provider execution authority and this
revision performs no provider refund API call, webhook handling, or provider
reconciliation.

Historical Finance invoices are not backfilled into refund_obligation_bindings by
this migration.  Invoices without a trusted persisted member_subscriptions_v2
correlation remain unbound, and the resolver fails closed/no-ops when no
authoritative binding exists.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zd07d8e9f0a3e"
down_revision = "zc07d8e9f0a3d"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_WORKER_ROLE = "worker_runtime"
_CONFIG_ROLE = "finance_config_runtime"
_RESOLVER_FUNCTION = "app_secure.resolve_branch_refund_required(uuid,uuid)"
_PRODUCER_FUNCTION = "app_secure.record_refund_obligation_binding(uuid,uuid)"
_MEMBER_BILLING_PARTY_FUNCTION = "app_secure.upsert_member_billing_party(uuid,text,text)"
_CHECKOUT_RESOLVER_FUNCTION = "app_secure.resolve_member_subscription_checkout_inputs(uuid)"
_CHECKOUT_BINDING_FUNCTION = "app_secure.record_member_subscription_checkout_binding(uuid,uuid,uuid)"
_PROVIDER_ORDER_FUNCTION = "app_secure.attach_member_subscription_checkout_provider_order(uuid,uuid,text)"
_TAX_CODE_CONFIG_FUNCTION = "app_secure.establish_finance_tax_code(uuid,text,text,text,text,integer)"
_BRANCH_ACCOUNTING_CONFIG_FUNCTION = "app_secure.establish_branch_accounting_profile(uuid,uuid,uuid,uuid,uuid,uuid,date,date)"
_PLAN_TAX_CONFIG_FUNCTION = "app_secure.establish_membership_plan_tax_profile(uuid,uuid,uuid,text,date,date)"
_INVOICE_ORG_FUNCTION = "app_secure.resolve_finance_invoice_organization(uuid)"
_BILLING_PARTY_ORG_FUNCTION = "app_secure.resolve_finance_billing_party_organization(uuid)"
_INVOICE_ACCOUNTING_MASTER_DATA_FUNCTION = "app_secure.resolve_finance_invoice_accounting_master_data(uuid,uuid,uuid,uuid,uuid,uuid,date)"
_FINANCE_IDEMPOTENCY_RESERVE_FUNCTION = "app_secure.reserve_finance_idempotency(text,text,text,uuid,timestamp with time zone)"
_FINANCE_IDEMPOTENCY_COMPLETE_FUNCTION = "app_secure.complete_finance_idempotency(uuid,text)"
_FINANCE_INVOICE_RESULT_FUNCTION = "app_secure.resolve_finance_invoice_result(uuid)"
_FINANCE_DRAFT_PERSIST_FUNCTION = "app_secure.persist_finance_draft_invoice(uuid,uuid,uuid,uuid,uuid,uuid,uuid,date,text,text,text,text,text,text,text,text,text,text,text,text,text,text,numeric,numeric,numeric,numeric,numeric,jsonb)"
_RUNTIME_ROLES = (
    "app_runtime",
    "auth_runtime",
    "worker_runtime",
    "lifecycle_maintenance_runtime",
    "finance_config_runtime",
)


def _require_reduced_role(bind, role_name: str) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT rolsuper,rolinherit,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname=:role_name
            """
        ),
        {"role_name": role_name},
    ).mappings().one_or_none()
    if row is None or any(bool(row[key]) for key in row):
        raise RuntimeError(f"zd07 reduced-role contract drift: {role_name}")


def _function_oids(bind, schema_name: str, function_name: str, normalized_args: str) -> list[int]:
    rows = bind.execute(
        sa.text(
            """
            SELECT p.oid AS function_oid
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = :schema_name
              AND p.proname = :function_name
              AND replace(pg_catalog.oidvectortypes(p.proargtypes), ' ', '') = :normalized_args
            ORDER BY p.oid
            """
        ),
        {
            "schema_name": schema_name,
            "function_name": function_name,
            "normalized_args": normalized_args,
        },
    ).scalars().all()
    return [int(row) for row in rows]


def _require_exact_function_count(
    bind,
    *,
    schema_name: str,
    function_name: str,
    normalized_args: str,
    expected_count: int,
    error_label: str,
) -> int | None:
    oids = _function_oids(bind, schema_name, function_name, normalized_args)
    if len(oids) != expected_count:
        raise RuntimeError(f"{error_label}: expected {expected_count}, observed {len(oids)}")
    if oids and not isinstance(oids[0], int):
        raise RuntimeError(f"{error_label}: catalog OID must remain an integer")
    return oids[0] if oids else None




def _constraint_exists(bind, schema_name: str, table_name: str, constraint_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_constraint con
                    JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = :schema_name
                      AND c.relname = :table_name
                      AND con.conname = :constraint_name
                )
                """
            ),
            {
                "schema_name": schema_name,
                "table_name": table_name,
                "constraint_name": constraint_name,
            },
        ).scalar_one()
    )


def _policy_exists(bind, schema_name: str, table_name: str, policy_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_policy pol
                    JOIN pg_catalog.pg_class c ON c.oid = pol.polrelid
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = :schema_name
                      AND c.relname = :table_name
                      AND pol.polname = :policy_name
                )
                """
            ),
            {
                "schema_name": schema_name,
                "table_name": table_name,
                "policy_name": policy_name,
            },
        ).scalar_one()
    )


def _has_table_privilege(bind, grantee: str, relation: str, privilege: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT CASE WHEN pg_catalog.to_regclass(:relation) IS NULL THEN false "
                "ELSE pg_catalog.has_table_privilege(:grantee, :relation, :privilege) END"
            ),
            {"grantee": grantee, "relation": relation, "privilege": privilege},
        ).scalar_one()
    )


def _require_predecessor_present_acl(bind, grantee: str, relation: str, privilege: str) -> None:
    if not _has_table_privilege(bind, grantee, relation, privilege):
        raise RuntimeError(
            f"zd07 predecessor privilege drift: missing {privilege} on {relation} for {grantee}"
        )


def _require_predecessor_absent_acl(bind, grantee: str, relation: str, privilege: str) -> None:
    if _has_table_privilege(bind, grantee, relation, privilege):
        raise RuntimeError(
            f"zd07 refuses to claim preexisting ACL {privilege} on {relation} for {grantee}"
        )


def _require_exact_constraint(bind, schema_name: str, table_name: str, constraint_name: str) -> None:
    if not _constraint_exists(bind, schema_name, table_name, constraint_name):
        raise RuntimeError(
            f"zd07 required constraint missing: {schema_name}.{table_name}.{constraint_name}"
        )


def _require_identity_contract(bind) -> None:
    row = bind.execute(
        sa.text(
            """
            SELECT session_user::text AS session_name,current_user::text AS current_name,
                   rolsuper,rolinherit,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname=current_user
            """
        )
    ).mappings().one()
    if row["session_name"] != _MIGRATION_OWNER or row["current_name"] != _MIGRATION_OWNER:
        raise RuntimeError("zd07 P4D migration requires migration_owner")
    if any(bool(row[key]) for key in (
        "rolsuper", "rolinherit", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"
    )):
        raise RuntimeError("zd07 migration_owner violates reduced-role contract")
    if not bind.execute(
        sa.text("SELECT pg_catalog.pg_has_role('migration_owner','app_security_owner','SET')")
    ).scalar_one():
        raise RuntimeError("zd07 requires migration_owner SET edge to app_security_owner")
    if bind.execute(
        sa.text("SELECT pg_catalog.has_schema_privilege('migration_owner', 'app_secure', 'USAGE')")
    ).scalar_one():
        raise RuntimeError("zd07 migration_owner must not have app_secure USAGE")
    _require_reduced_role(bind, _SECURITY_OWNER)
    for role_name in _RUNTIME_ROLES:
        _require_reduced_role(bind, role_name)
        if bind.execute(
            sa.text("SELECT pg_catalog.pg_has_role(:member,:target,'SET')"),
            {"member": role_name, "target": _SECURITY_OWNER},
        ).scalar_one():
            raise RuntimeError(f"zd07 runtime may SET ROLE app_security_owner: {role_name}")


def _require_predecessor(bind) -> None:
    for relation in (
        "public.branch_outbox_events",
        "public.org_branches",
        "public.members",
        "public.membership_plans",
        "finance.payments",
        "finance.payment_allocations",
        "finance.invoices",
        "finance.refunds",
        "finance.refund_execution_commands",
        "finance.billing_parties",
        "finance.legal_entities",
        "finance.gst_registrations",
        "finance.divisions",
        "finance.brands",
        "finance.tax_codes",
        "public.member_subscriptions_v2",
    ):
        if bind.execute(
            sa.text("SELECT pg_catalog.to_regclass(:relation) IS NULL"),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(f"zd07 missing predecessor relation {relation}")
    _require_exact_function_count(
        bind,
        schema_name="app_secure",
        function_name="materialize_refund_execution_command",
        normalized_args="uuid,text,uuid,text",
        expected_count=1,
        error_label="zd07 missing or ambiguous predecessor materialize_refund_execution_command",
    )
    _require_exact_function_count(
        bind,
        schema_name="app_secure",
        function_name="resolve_branch_refund_required",
        normalized_args="uuid,uuid",
        expected_count=0,
        error_label="zd07 refund obligation resolver already exists",
    )
    _require_exact_function_count(
        bind,
        schema_name="app_secure",
        function_name="record_refund_obligation_binding",
        normalized_args="uuid,uuid",
        expected_count=0,
        error_label="zd07 refund obligation producer already exists",
    )
    for function_name, normalized_args in (
        ("upsert_member_billing_party", "uuid,text,text"),
        ("resolve_member_subscription_checkout_inputs", "uuid"),
        ("record_member_subscription_checkout_binding", "uuid,uuid,uuid"),
        ("attach_member_subscription_checkout_provider_order", "uuid,uuid,text"),
        ("resolve_finance_invoice_organization", "uuid"),
        ("resolve_finance_billing_party_organization", "uuid"),
        ("resolve_finance_invoice_accounting_master_data", "uuid,uuid,uuid,uuid,uuid,uuid,date"),
        ("reserve_finance_idempotency", "text,text,text,uuid,timestampwithtimezone"),
        ("complete_finance_idempotency", "uuid,text"),
        ("persist_finance_draft_invoice", "uuid,uuid,uuid,uuid,uuid,uuid,uuid,date,text,text,text,text,text,text,text,text,text,text,text,text,text,text,numeric,numeric,numeric,numeric,numeric,jsonb"),
        ("resolve_finance_invoice_result", "uuid"),
        ("establish_finance_tax_code", "uuid,text,text,text,text,integer"),
        ("establish_branch_accounting_profile", "uuid,uuid,uuid,uuid,uuid,uuid,date,date"),
        ("establish_membership_plan_tax_profile", "uuid,uuid,uuid,text,date,date"),
    ):
        _require_exact_function_count(
            bind,
            schema_name="app_secure",
            function_name=function_name,
            normalized_args=normalized_args,
            expected_count=0,
            error_label=f"zd07 P4D-2 function already exists: {function_name}",
        )
    for role_name in (_WORKER_ROLE, "app_runtime"):
        if not bind.execute(
            sa.text("SELECT pg_catalog.has_schema_privilege(:role_name, 'app_secure', 'USAGE')"),
            {"role_name": role_name},
        ).scalar_one():
            raise RuntimeError(f"zd07 requires existing app_secure USAGE for {role_name}")
    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.has_column_privilege('app_security_owner','public.org_branches','id','SELECT') "
            "AND pg_catalog.has_column_privilege('app_security_owner','public.org_branches','org_id','SELECT')"
        )
    ).scalar_one():
        raise RuntimeError("zd07 requires predecessor app_security_owner branch id/org read")
    for relation, privilege in (
        ("finance.payments", "SELECT"),
        ("finance.payments", "UPDATE"),
        ("finance.refunds", "SELECT"),
        ("finance.refunds", "UPDATE"),
        ("public.branch_outbox_events", "SELECT"),
    ):
        _require_predecessor_present_acl(bind, "app_security_owner", relation, privilege)
    _require_exact_constraint(bind, "public", "org_branches", "uq_org_branch_pair")
    if _constraint_exists(bind, "public", "member_subscriptions_v2", "fk_member_subscriptions_v2_branch_org"):
        raise RuntimeError("zd07 refuses to claim predecessor member subscription branch/org FK")
    if _constraint_exists(bind, "finance", "invoices", "uq_finance_invoices_id_org"):
        raise RuntimeError("zd07 refuses to claim predecessor invoice id/org unique constraint")
    if _constraint_exists(bind, "public", "members", "uq_members_id_org"):
        raise RuntimeError("zd07 refuses to claim predecessor members id/org unique constraint")
    if _constraint_exists(bind, "public", "membership_plans", "uq_membership_plans_id_org"):
        raise RuntimeError("zd07 refuses to claim predecessor membership plans id/org unique constraint")
    if _constraint_exists(bind, "public", "member_subscriptions_v2", "uq_member_subscriptions_v2_id_org"):
        raise RuntimeError("zd07 refuses to claim predecessor member subscription id/org unique constraint")
    if _constraint_exists(bind, "finance", "payments", "uq_finance_payments_id_org"):
        raise RuntimeError("zd07 refuses to claim predecessor payment id/org unique constraint")
    if not _constraint_exists(bind, "finance", "billing_parties", "uq_finance_billing_parties_organization"):
        raise RuntimeError("zd07 requires predecessor organization billing-party uniqueness")
    if bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM public.member_subscriptions_v2 s
                LEFT JOIN public.org_branches b
                  ON b.id = s.branch_id
                 AND b.org_id = s.org_id
                WHERE b.id IS NULL
                LIMIT 1
            )
            """
        )
    ).scalar_one():
        raise RuntimeError("zd07 member subscription branch/org history violates source-bound provenance")
    if _policy_exists(
        bind,
        "public",
        "member_subscriptions_v2",
        "p4d_member_subscriptions_v2_security_owner_select",
    ):
        raise RuntimeError("zd07 refuses to claim predecessor member subscription security-owner policy")
    if _policy_exists(
        bind,
        "public",
        "membership_plans",
        "p4d_membership_plans_security_owner_select",
    ):
        raise RuntimeError("zd07 refuses to claim predecessor membership plan security-owner policy")
    for relation, privilege in (
        ("finance.payment_allocations", "SELECT"),
        ("finance.invoices", "SELECT"),
        ("finance.refunds", "INSERT"),
        ("finance.refund_obligation_bindings", "SELECT"),
        ("finance.refund_obligation_bindings", "INSERT"),
        ("public.member_subscriptions_v2", "SELECT"),
        ("finance.billing_parties", "SELECT"),
        ("finance.billing_parties", "INSERT"),
        ("finance.billing_parties", "UPDATE"),
        ("finance.legal_entities", "SELECT"),
        ("finance.gst_registrations", "SELECT"),
        ("finance.divisions", "SELECT"),
        ("finance.brands", "SELECT"),
        ("finance.tax_codes", "SELECT"),
        ("finance.invoices", "INSERT"),
        ("finance.invoice_lines", "INSERT"),
        ("public.members", "SELECT"),
        ("public.membership_plans", "SELECT"),
    ):
        _require_predecessor_absent_acl(bind, "app_security_owner", relation, privilege)


def _install_function(bind) -> None:
    if not _constraint_exists(bind, "public", "member_subscriptions_v2", "fk_member_subscriptions_v2_branch_org"):
        op.execute(
            """
            ALTER TABLE public.member_subscriptions_v2
            ADD CONSTRAINT fk_member_subscriptions_v2_branch_org
            FOREIGN KEY (branch_id, org_id) REFERENCES public.org_branches(id, org_id) ON DELETE RESTRICT
            """
        )
    if not _constraint_exists(bind, "finance", "invoices", "uq_finance_invoices_id_org"):
        op.execute(
            """
            ALTER TABLE finance.invoices
            ADD CONSTRAINT uq_finance_invoices_id_org UNIQUE (id, organization_id)
            """
        )
    if not _constraint_exists(bind, "public", "members", "uq_members_id_org"):
        op.execute("ALTER TABLE public.members ADD CONSTRAINT uq_members_id_org UNIQUE (id, org_id)")
    if not _constraint_exists(bind, "public", "membership_plans", "uq_membership_plans_id_org"):
        op.execute("ALTER TABLE public.membership_plans ADD CONSTRAINT uq_membership_plans_id_org UNIQUE (id, org_id)")
    if not _constraint_exists(bind, "public", "member_subscriptions_v2", "uq_member_subscriptions_v2_id_org"):
        op.execute("ALTER TABLE public.member_subscriptions_v2 ADD CONSTRAINT uq_member_subscriptions_v2_id_org UNIQUE (id, org_id)")
    if not _constraint_exists(bind, "finance", "payments", "uq_finance_payments_id_org"):
        op.execute("ALTER TABLE finance.payments ADD CONSTRAINT uq_finance_payments_id_org UNIQUE (id, organization_id)")
    op.execute("ALTER TABLE finance.billing_parties ADD COLUMN buyer_kind TEXT NOT NULL DEFAULT 'organization'")
    op.execute("ALTER TABLE finance.billing_parties ADD COLUMN member_id UUID NULL")
    op.execute("ALTER TABLE finance.billing_parties ALTER COLUMN buyer_kind DROP DEFAULT")
    op.execute("ALTER TABLE finance.billing_parties DROP CONSTRAINT uq_finance_billing_parties_organization")
    op.execute("ALTER TABLE finance.billing_parties ADD CONSTRAINT chk_finance_billing_parties_buyer_kind CHECK (buyer_kind IN ('organization','member'))")
    op.execute(
        """
        ALTER TABLE finance.billing_parties
        ADD CONSTRAINT chk_finance_billing_parties_buyer_shape
        CHECK (
            (buyer_kind = 'organization' AND member_id IS NULL)
            OR (
                buyer_kind = 'member'
                AND member_id IS NOT NULL
                AND organization_id IS NOT NULL
                AND party_type = 'individual'
                AND gst_treatment = 'b2c'
            )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE finance.billing_parties
        ADD CONSTRAINT fk_finance_billing_parties_member_org
        FOREIGN KEY (member_id, organization_id) REFERENCES public.members(id, org_id) ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_finance_billing_parties_org_buyer
        ON finance.billing_parties(organization_id)
        WHERE buyer_kind = 'organization'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_finance_billing_parties_member_buyer
        ON finance.billing_parties(organization_id, member_id)
        WHERE buyer_kind = 'member'
        """
    )
    op.execute(
        """
        CREATE TABLE finance.branch_accounting_profiles (
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            branch_id UUID NOT NULL,
            legal_entity_id UUID NOT NULL REFERENCES finance.legal_entities(id) ON DELETE RESTRICT,
            gst_registration_id UUID NOT NULL REFERENCES finance.gst_registrations(id) ON DELETE RESTRICT,
            division_id UUID NOT NULL REFERENCES finance.divisions(id) ON DELETE RESTRICT,
            brand_id UUID NOT NULL REFERENCES finance.brands(id) ON DELETE RESTRICT,
            effective_from DATE NOT NULL,
            effective_until DATE NULL,
            status TEXT NOT NULL DEFAULT 'active',
            configured_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_branch_accounting_profiles_branch_org
                FOREIGN KEY (branch_id, organization_id) REFERENCES public.org_branches(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT chk_branch_accounting_profiles_status CHECK (status IN ('active','inactive')),
            CONSTRAINT chk_branch_accounting_profiles_window CHECK (effective_until IS NULL OR effective_until > effective_from)
        )
        """
    )
    op.execute("CREATE INDEX ix_branch_accounting_profiles_lookup ON finance.branch_accounting_profiles(organization_id, branch_id, status, effective_from, effective_until)")
    op.execute(
        """
        ALTER TABLE finance.branch_accounting_profiles
        ADD CONSTRAINT ex_branch_accounting_profiles_active_window
        EXCLUDE USING gist (
            organization_id WITH =,
            branch_id WITH =,
            daterange(effective_from, coalesce(effective_until, 'infinity'::date), '[)') WITH &&
        )
        WHERE (status = 'active')
        """
    )
    op.execute(
        """
        CREATE TABLE finance.membership_plan_tax_profiles (
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            membership_plan_id UUID NOT NULL,
            tax_code_id UUID NOT NULL REFERENCES finance.tax_codes(id) ON DELETE RESTRICT,
            pricing_mode TEXT NOT NULL,
            effective_from DATE NOT NULL,
            effective_until DATE NULL,
            status TEXT NOT NULL DEFAULT 'active',
            configured_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_membership_plan_tax_profiles_plan_org
                FOREIGN KEY (membership_plan_id, organization_id) REFERENCES public.membership_plans(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT chk_membership_plan_tax_profiles_status CHECK (status IN ('active','inactive')),
            CONSTRAINT chk_membership_plan_tax_profiles_pricing_mode CHECK (pricing_mode IN ('tax_exclusive','tax_inclusive')),
            CONSTRAINT chk_membership_plan_tax_profiles_window CHECK (effective_until IS NULL OR effective_until > effective_from)
        )
        """
    )
    op.execute("CREATE INDEX ix_membership_plan_tax_profiles_lookup ON finance.membership_plan_tax_profiles(organization_id, membership_plan_id, status, effective_from, effective_until)")
    op.execute(
        """
        ALTER TABLE finance.membership_plan_tax_profiles
        ADD CONSTRAINT ex_membership_plan_tax_profiles_active_window
        EXCLUDE USING gist (
            organization_id WITH =,
            membership_plan_id WITH =,
            daterange(effective_from, coalesce(effective_until, 'infinity'::date), '[)') WITH &&
        )
        WHERE (status = 'active')
        """
    )
    op.execute(
        """
        CREATE FUNCTION finance.prevent_branch_accounting_profile_invalidity()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path=pg_catalog,public,finance,public
        AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM finance.gst_registrations g
                JOIN finance.divisions d ON d.id = NEW.division_id
                JOIN finance.brands b ON b.id = NEW.brand_id
                WHERE g.id = NEW.gst_registration_id
                  AND g.legal_entity_id = NEW.legal_entity_id
                  AND d.legal_entity_id = NEW.legal_entity_id
                  AND b.legal_entity_id = NEW.legal_entity_id
                  AND b.division_id = NEW.division_id
            ) THEN
                RAISE EXCEPTION 'P4D branch accounting profile master data mismatch' USING ERRCODE='23514';
            END IF;
            IF NEW.status = 'active' AND EXISTS (
                SELECT 1
                FROM finance.branch_accounting_profiles existing
                WHERE existing.organization_id = NEW.organization_id
                  AND existing.branch_id = NEW.branch_id
                  AND existing.status = 'active'
                  AND existing.id <> NEW.id
                  AND existing.effective_from < coalesce(NEW.effective_until, 'infinity'::date)
                  AND NEW.effective_from < coalesce(existing.effective_until, 'infinity'::date)
            ) THEN
                RAISE EXCEPTION 'P4D branch accounting profile active window overlaps' USING ERRCODE='23505';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_branch_accounting_profiles_no_overlap
        BEFORE INSERT OR UPDATE ON finance.branch_accounting_profiles
        FOR EACH ROW EXECUTE FUNCTION finance.prevent_branch_accounting_profile_invalidity();
        """
    )
    op.execute(
        """
        CREATE FUNCTION finance.prevent_membership_plan_tax_profile_overlap()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path=pg_catalog,public,finance,public
        AS $$
        BEGIN
            IF NEW.status = 'active' AND EXISTS (
                SELECT 1
                FROM finance.membership_plan_tax_profiles existing
                WHERE existing.organization_id = NEW.organization_id
                  AND existing.membership_plan_id = NEW.membership_plan_id
                  AND existing.status = 'active'
                  AND existing.id <> NEW.id
                  AND existing.effective_from < coalesce(NEW.effective_until, 'infinity'::date)
                  AND NEW.effective_from < coalesce(existing.effective_until, 'infinity'::date)
            ) THEN
                RAISE EXCEPTION 'P4D membership plan tax profile active window overlaps' USING ERRCODE='23505';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_membership_plan_tax_profiles_no_overlap
        BEFORE INSERT OR UPDATE ON finance.membership_plan_tax_profiles
        FOR EACH ROW EXECUTE FUNCTION finance.prevent_membership_plan_tax_profile_overlap();
        """
    )
    op.execute(
        """
        CREATE TABLE finance.member_subscription_checkout_bindings (
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            subscription_id UUID NOT NULL,
            invoice_id UUID NOT NULL,
            checkout_intent_id UUID NOT NULL,
            source_table TEXT NOT NULL DEFAULT 'member_subscriptions_v2',
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_member_subscription_checkout_bindings_subscription_org
                FOREIGN KEY (subscription_id, organization_id) REFERENCES public.member_subscriptions_v2(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_member_subscription_checkout_bindings_invoice_org
                FOREIGN KEY (invoice_id, organization_id) REFERENCES finance.invoices(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_member_subscription_checkout_bindings_intent_org
                FOREIGN KEY (checkout_intent_id, organization_id) REFERENCES finance.payments(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT uq_member_subscription_checkout_bindings_subscription UNIQUE (subscription_id),
            CONSTRAINT uq_member_subscription_checkout_bindings_invoice UNIQUE (invoice_id),
            CONSTRAINT uq_member_subscription_checkout_bindings_intent UNIQUE (checkout_intent_id),
            CONSTRAINT chk_member_subscription_checkout_bindings_source_table CHECK (source_table = 'member_subscriptions_v2')
        )
        """
    )
    op.execute("ALTER TABLE finance.branch_accounting_profiles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE finance.branch_accounting_profiles FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE finance.branch_accounting_profiles FROM PUBLIC")
    op.execute("ALTER TABLE finance.membership_plan_tax_profiles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE finance.membership_plan_tax_profiles FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE finance.membership_plan_tax_profiles FROM PUBLIC")
    op.execute("ALTER TABLE finance.member_subscription_checkout_bindings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE finance.member_subscription_checkout_bindings FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE finance.member_subscription_checkout_bindings FROM PUBLIC")
    op.execute("ALTER TABLE finance.tax_codes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE finance.tax_codes FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE finance.tax_codes FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE finance.billing_parties TO app_security_owner")
    op.execute("GRANT SELECT ON TABLE finance.legal_entities TO app_security_owner")
    op.execute("GRANT SELECT ON TABLE finance.gst_registrations TO app_security_owner")
    op.execute("GRANT SELECT ON TABLE finance.divisions TO app_security_owner")
    op.execute("GRANT SELECT ON TABLE finance.brands TO app_security_owner")
    op.execute("GRANT SELECT, INSERT ON TABLE finance.tax_codes TO app_security_owner")
    op.execute("GRANT SELECT ON TABLE public.members TO app_security_owner")
    op.execute("GRANT SELECT ON TABLE public.membership_plans TO app_security_owner")
    op.execute("CREATE POLICY p4d_membership_plans_security_owner_select ON public.membership_plans FOR SELECT TO app_security_owner USING (true)")
    op.execute("GRANT SELECT, INSERT ON TABLE finance.branch_accounting_profiles TO app_security_owner")
    op.execute("GRANT SELECT, INSERT ON TABLE finance.membership_plan_tax_profiles TO app_security_owner")
    op.execute("GRANT SELECT, INSERT ON TABLE finance.member_subscription_checkout_bindings TO app_security_owner")
    op.execute("GRANT SELECT (id, organization_id, scope, idempotency_key, request_hash_sha256, status, response_ref, created_at, expires_at) ON TABLE finance.idempotency_keys TO app_security_owner")
    op.execute("GRANT INSERT (organization_id, scope, idempotency_key, request_hash_sha256, status, expires_at) ON TABLE finance.idempotency_keys TO app_security_owner")
    op.execute("GRANT UPDATE (status, response_ref) ON TABLE finance.idempotency_keys TO app_security_owner")
    op.execute("ALTER TABLE finance.idempotency_keys DROP CONSTRAINT uq_finance_idempotency_keys_scope_key")
    op.execute("ALTER TABLE finance.idempotency_keys ADD CONSTRAINT uq_finance_idempotency_keys_scope_key UNIQUE (organization_id, scope, idempotency_key)")
    op.execute("GRANT SELECT (id, organization_id, status, official_invoice_number, brand_reference) ON TABLE finance.invoices TO app_security_owner")
    op.execute("GRANT INSERT (organization_id, billing_party_id, legal_entity_id, gst_registration_id, division_id, brand_id, financial_year, status, currency_code, seller_legal_name, seller_gstin, seller_pan, seller_registered_address, seller_state_code, buyer_billing_name, buyer_address, buyer_gstin, buyer_pan, buyer_place_of_supply_state_code, buyer_gst_treatment, gst_supply_type, subtotal_amount, discount_amount, taxable_amount, total_tax_amount, grand_total_amount, metadata_json) ON TABLE finance.invoices TO app_security_owner")
    op.execute("GRANT INSERT (invoice_id, line_number, description, hsn_sac, quantity, unit_amount, discount_amount, taxable_amount, gst_rate_basis_points, cgst_amount, sgst_amount, igst_amount, total_tax_amount, line_total_amount, pricing_mode) ON TABLE finance.invoice_lines TO app_security_owner")
    op.execute("CREATE POLICY p4d_tax_codes_security_owner_select ON finance.tax_codes FOR SELECT TO app_security_owner USING (true)")
    op.execute("CREATE POLICY p4d_tax_codes_security_owner_insert ON finance.tax_codes FOR INSERT TO app_security_owner WITH CHECK (status = 'active' AND code ~ '^[A-Z0-9_]+$' AND description IS NOT NULL AND pg_catalog.btrim(description) <> '' AND tax_type IN ('gst','exempt','non_gst') AND gst_rate_basis_points >= 0)")
    op.execute("CREATE POLICY p4d_branch_accounting_profiles_security_owner_select ON finance.branch_accounting_profiles FOR SELECT TO app_security_owner USING (true)")
    op.execute("CREATE POLICY p4d_branch_accounting_profiles_security_owner_insert ON finance.branch_accounting_profiles FOR INSERT TO app_security_owner WITH CHECK (status = 'active' AND (effective_until IS NULL OR effective_until > effective_from))")
    op.execute("CREATE POLICY p4d_membership_plan_tax_profiles_security_owner_select ON finance.membership_plan_tax_profiles FOR SELECT TO app_security_owner USING (true)")
    op.execute("CREATE POLICY p4d_membership_plan_tax_profiles_security_owner_insert ON finance.membership_plan_tax_profiles FOR INSERT TO app_security_owner WITH CHECK (status = 'active' AND pricing_mode IN ('tax_exclusive','tax_inclusive') AND (effective_until IS NULL OR effective_until > effective_from))")
    op.execute("CREATE POLICY p4d_member_subscription_checkout_bindings_security_owner_select ON finance.member_subscription_checkout_bindings FOR SELECT TO app_security_owner USING (true)")
    op.execute("CREATE POLICY p4d_member_subscription_checkout_bindings_security_owner_insert ON finance.member_subscription_checkout_bindings FOR INSERT TO app_security_owner WITH CHECK (true)")
    op.execute(
        """
        CREATE TABLE finance.refund_obligation_bindings (
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE RESTRICT,
            branch_id UUID NOT NULL,
            invoice_id UUID NOT NULL,
            source_table TEXT NOT NULL,
            source_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_refund_obligation_bindings_branch_org
                FOREIGN KEY (branch_id, organization_id) REFERENCES public.org_branches(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_refund_obligation_bindings_invoice_org
                FOREIGN KEY (invoice_id, organization_id) REFERENCES finance.invoices(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT uq_refund_obligation_bindings_invoice UNIQUE (invoice_id),
            CONSTRAINT chk_refund_obligation_bindings_source_table
                CHECK (source_table = 'member_subscriptions_v2')
        )
        """
    )
    op.execute("ALTER TABLE finance.refund_obligation_bindings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE finance.refund_obligation_bindings FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE finance.refund_obligation_bindings FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT ON TABLE finance.refund_obligation_bindings TO app_security_owner")
    op.execute(
        """
        CREATE POLICY p4d_refund_obligation_security_owner_select
        ON finance.refund_obligation_bindings
        FOR SELECT
        TO app_security_owner
        USING (true)
        """
    )
    op.execute(
        """
        CREATE POLICY p4d_refund_obligation_security_owner_insert
        ON finance.refund_obligation_bindings
        FOR INSERT
        TO app_security_owner
        WITH CHECK (true)
        """
    )
    op.execute("GRANT SELECT ON TABLE public.member_subscriptions_v2 TO app_security_owner")
    op.execute(
        """
        CREATE POLICY p4d_member_subscriptions_v2_security_owner_select
        ON public.member_subscriptions_v2
        FOR SELECT
        TO app_security_owner
        USING (true)
        """
    )
    op.execute("GRANT SELECT ON TABLE finance.payment_allocations TO app_security_owner")
    op.execute("GRANT SELECT ON TABLE finance.invoices TO app_security_owner")
    op.execute("GRANT INSERT ON TABLE finance.refunds TO app_security_owner")
    op.execute("GRANT SELECT (is_active, name, business_type, description) ON TABLE public.organizations TO app_security_owner")
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute(
        """
        CREATE FUNCTION app_secure.record_refund_obligation_binding(
            p_invoice_id uuid,
            p_source_id uuid
        )
        RETURNS TABLE(
            invoice_id uuid,
            organization_id uuid,
            branch_id uuid,
            source_table text,
            source_id uuid,
            inserted boolean,
            replayed boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_source public.member_subscriptions_v2%ROWTYPE;
            v_invoice_org_id uuid;
            v_current_org_id uuid;
            v_row_count integer := 0;
            v_binding record;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D refund obligation binding requires app_runtime' USING ERRCODE='42501';
            END IF;
            IF p_invoice_id IS NULL OR p_source_id IS NULL THEN
                RAISE EXCEPTION 'P4D refund obligation binding requires invoice and member subscription source' USING ERRCODE='22023';
            END IF;
            BEGIN
                v_current_org_id := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'P4D refund obligation binding requires valid tenant context' USING ERRCODE='22023';
            END;
            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D refund obligation binding requires tenant context' USING ERRCODE='22023';
            END IF;

            SELECT s.* INTO v_source
            FROM public.member_subscriptions_v2 s
            WHERE s.id = p_source_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D refund obligation source subscription not found' USING ERRCODE='P0002';
            END IF;
            IF v_source.org_id IS NULL OR v_source.branch_id IS NULL THEN
                RAISE EXCEPTION 'P4D refund obligation source subscription is not branch-bound' USING ERRCODE='23514';
            END IF;
            IF v_current_org_id IS DISTINCT FROM v_source.org_id THEN
                RAISE EXCEPTION 'P4D refund obligation tenant context does not own source subscription' USING ERRCODE='42501';
            END IF;

            SELECT i.organization_id INTO v_invoice_org_id
            FROM finance.invoices i
            WHERE i.id = p_invoice_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D refund obligation invoice not found' USING ERRCODE='P0002';
            END IF;
            IF v_invoice_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D refund obligation invoice is not organization-bound' USING ERRCODE='23514';
            END IF;
            IF v_invoice_org_id IS DISTINCT FROM v_source.org_id THEN
                RAISE EXCEPTION 'P4D refund obligation invoice/source organization mismatch' USING ERRCODE='23514';
            END IF;

            INSERT INTO finance.refund_obligation_bindings(
                organization_id, branch_id, invoice_id, source_table, source_id
            )
            VALUES (
                v_source.org_id, v_source.branch_id, p_invoice_id, 'member_subscriptions_v2', p_source_id
            )
            ON CONFLICT ON CONSTRAINT uq_refund_obligation_bindings_invoice DO NOTHING;
            GET DIAGNOSTICS v_row_count = ROW_COUNT;

            SELECT b.invoice_id, b.organization_id, b.branch_id, b.source_table, b.source_id
            INTO v_binding
            FROM finance.refund_obligation_bindings b
            WHERE b.invoice_id = p_invoice_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D refund obligation binding was not persisted' USING ERRCODE='23514';
            END IF;
            IF v_binding.organization_id IS DISTINCT FROM v_source.org_id
               OR v_binding.branch_id IS DISTINCT FROM v_source.branch_id
               OR v_binding.source_table IS DISTINCT FROM 'member_subscriptions_v2'
               OR v_binding.source_id IS DISTINCT FROM p_source_id THEN
                RAISE EXCEPTION 'P4D refund obligation binding replay conflict' USING ERRCODE='23505';
            END IF;

            RETURN QUERY SELECT v_binding.invoice_id, v_binding.organization_id, v_binding.branch_id,
                v_binding.source_table, v_binding.source_id, v_row_count = 1, v_row_count = 0;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.establish_finance_tax_code(
            p_tax_code_id uuid,
            p_code text,
            p_description text,
            p_hsn_sac text,
            p_tax_type text,
            p_gst_rate_basis_points integer
        )
        RETURNS TABLE(
            tax_code_id uuid,
            code text,
            inserted boolean,
            replayed boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_existing finance.tax_codes%ROWTYPE;
            v_row_count integer := 0;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'finance_config_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D finance tax code configuration requires finance_config_runtime' USING ERRCODE='42501';
            END IF;
            IF p_tax_code_id IS NULL OR p_code IS NULL OR p_code !~ '^[A-Z0-9_]+$'
               OR p_description IS NULL OR pg_catalog.btrim(p_description) = ''
               OR p_tax_type NOT IN ('gst','exempt','non_gst')
               OR p_gst_rate_basis_points IS NULL OR p_gst_rate_basis_points < 0 THEN
                RAISE EXCEPTION 'P4D finance tax code configuration invalid' USING ERRCODE='22023';
            END IF;

            INSERT INTO finance.tax_codes(id, code, description, hsn_sac, tax_type, gst_rate_basis_points, status)
            VALUES (p_tax_code_id, p_code, p_description, p_hsn_sac, p_tax_type, p_gst_rate_basis_points, 'active')
            ON CONFLICT (id) DO NOTHING;
            GET DIAGNOSTICS v_row_count = ROW_COUNT;

            SELECT * INTO v_existing FROM finance.tax_codes WHERE id = p_tax_code_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D finance tax code configuration was not persisted' USING ERRCODE='23514';
            END IF;
            IF v_existing.code IS DISTINCT FROM p_code
               OR v_existing.description IS DISTINCT FROM p_description
               OR v_existing.hsn_sac IS DISTINCT FROM p_hsn_sac
               OR v_existing.tax_type IS DISTINCT FROM p_tax_type
               OR v_existing.gst_rate_basis_points IS DISTINCT FROM p_gst_rate_basis_points
               OR v_existing.status IS DISTINCT FROM 'active' THEN
                RAISE EXCEPTION 'P4D finance tax code configuration replay conflict' USING ERRCODE='23505';
            END IF;
            RETURN QUERY SELECT p_tax_code_id, p_code::text, v_row_count = 1, v_row_count = 0;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.establish_branch_accounting_profile(
            p_profile_id uuid,
            p_branch_id uuid,
            p_legal_entity_id uuid,
            p_gst_registration_id uuid,
            p_division_id uuid,
            p_brand_id uuid,
            p_effective_from date,
            p_effective_until date
        )
        RETURNS TABLE(
            profile_id uuid,
            organization_id uuid,
            branch_id uuid,
            inserted boolean,
            replayed boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_org_id uuid;
            v_existing finance.branch_accounting_profiles%ROWTYPE;
            v_row_count integer := 0;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'finance_config_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D branch accounting configuration requires finance_config_runtime' USING ERRCODE='42501';
            END IF;
            IF p_profile_id IS NULL OR p_branch_id IS NULL OR p_legal_entity_id IS NULL
               OR p_gst_registration_id IS NULL OR p_division_id IS NULL OR p_brand_id IS NULL
               OR p_effective_from IS NULL OR (p_effective_until IS NOT NULL AND p_effective_until <= p_effective_from) THEN
                RAISE EXCEPTION 'P4D branch accounting configuration invalid' USING ERRCODE='22023';
            END IF;

            SELECT b.org_id INTO v_org_id
            FROM public.org_branches b
            WHERE b.id = p_branch_id;
            IF v_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D branch accounting configuration branch not found' USING ERRCODE='P0002';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM finance.gst_registrations g
                JOIN finance.divisions d ON d.id = p_division_id
                JOIN finance.brands br ON br.id = p_brand_id
                WHERE g.id = p_gst_registration_id
                  AND g.legal_entity_id = p_legal_entity_id
                  AND g.status = 'active'
                  AND d.legal_entity_id = p_legal_entity_id
                  AND d.status = 'active'
                  AND br.legal_entity_id = p_legal_entity_id
                  AND br.division_id = p_division_id
                  AND br.status = 'active'
                  AND EXISTS (SELECT 1 FROM finance.legal_entities le WHERE le.id = p_legal_entity_id AND le.status = 'active')
            ) THEN
                RAISE EXCEPTION 'P4D branch accounting configuration master data mismatch' USING ERRCODE='23514';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM finance.payments p
                WHERE p.legal_entity_id = p_legal_entity_id
                  AND p.organization_id <> v_org_id
                UNION ALL
                SELECT 1
                FROM finance.invoices i
                WHERE i.legal_entity_id = p_legal_entity_id
                  AND i.organization_id <> v_org_id
                UNION ALL
                SELECT 1
                FROM finance.branch_accounting_profiles bap
                WHERE bap.legal_entity_id = p_legal_entity_id
                  AND bap.organization_id <> v_org_id
                LIMIT 1
            ) THEN
                RAISE EXCEPTION 'P4D branch accounting configuration legal entity organization mismatch' USING ERRCODE='23514';
            END IF;

            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('p4d2-branch-accounting:' || v_org_id::text || ':' || p_branch_id::text, 0));

            INSERT INTO finance.branch_accounting_profiles(
                id, organization_id, branch_id, legal_entity_id, gst_registration_id,
                division_id, brand_id, effective_from, effective_until, status, configured_by
            )
            VALUES (
                p_profile_id, v_org_id, p_branch_id, p_legal_entity_id, p_gst_registration_id,
                p_division_id, p_brand_id, p_effective_from, p_effective_until, 'active', session_user::text
            )
            ON CONFLICT (id) DO NOTHING;
            GET DIAGNOSTICS v_row_count = ROW_COUNT;

            SELECT * INTO v_existing FROM finance.branch_accounting_profiles WHERE id = p_profile_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D branch accounting configuration was not persisted' USING ERRCODE='23514';
            END IF;
            IF v_existing.organization_id IS DISTINCT FROM v_org_id
               OR v_existing.branch_id IS DISTINCT FROM p_branch_id
               OR v_existing.legal_entity_id IS DISTINCT FROM p_legal_entity_id
               OR v_existing.gst_registration_id IS DISTINCT FROM p_gst_registration_id
               OR v_existing.division_id IS DISTINCT FROM p_division_id
               OR v_existing.brand_id IS DISTINCT FROM p_brand_id
               OR v_existing.effective_from IS DISTINCT FROM p_effective_from
               OR v_existing.effective_until IS DISTINCT FROM p_effective_until
               OR v_existing.status IS DISTINCT FROM 'active' THEN
                RAISE EXCEPTION 'P4D branch accounting configuration replay conflict' USING ERRCODE='23505';
            END IF;
            RETURN QUERY SELECT p_profile_id, v_org_id, p_branch_id, v_row_count = 1, v_row_count = 0;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.establish_membership_plan_tax_profile(
            p_profile_id uuid,
            p_membership_plan_id uuid,
            p_tax_code_id uuid,
            p_pricing_mode text,
            p_effective_from date,
            p_effective_until date
        )
        RETURNS TABLE(
            profile_id uuid,
            organization_id uuid,
            membership_plan_id uuid,
            inserted boolean,
            replayed boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_org_id uuid;
            v_existing finance.membership_plan_tax_profiles%ROWTYPE;
            v_row_count integer := 0;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'finance_config_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D membership plan tax configuration requires finance_config_runtime' USING ERRCODE='42501';
            END IF;
            IF p_profile_id IS NULL OR p_membership_plan_id IS NULL OR p_tax_code_id IS NULL
               OR p_pricing_mode NOT IN ('tax_exclusive','tax_inclusive')
               OR p_effective_from IS NULL OR (p_effective_until IS NOT NULL AND p_effective_until <= p_effective_from) THEN
                RAISE EXCEPTION 'P4D membership plan tax configuration invalid' USING ERRCODE='22023';
            END IF;

            SELECT p.org_id INTO v_org_id
            FROM public.membership_plans p
            WHERE p.id = p_membership_plan_id;
            IF v_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D membership plan tax configuration plan not found' USING ERRCODE='P0002';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM finance.tax_codes t WHERE t.id = p_tax_code_id AND t.status = 'active') THEN
                RAISE EXCEPTION 'P4D membership plan tax configuration tax code not active' USING ERRCODE='23514';
            END IF;

            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('p4d2-plan-tax:' || v_org_id::text || ':' || p_membership_plan_id::text, 0));

            INSERT INTO finance.membership_plan_tax_profiles(
                id, organization_id, membership_plan_id, tax_code_id, pricing_mode,
                effective_from, effective_until, status, configured_by
            )
            VALUES (
                p_profile_id, v_org_id, p_membership_plan_id, p_tax_code_id, p_pricing_mode,
                p_effective_from, p_effective_until, 'active', session_user::text
            )
            ON CONFLICT (id) DO NOTHING;
            GET DIAGNOSTICS v_row_count = ROW_COUNT;

            SELECT * INTO v_existing FROM finance.membership_plan_tax_profiles WHERE id = p_profile_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D membership plan tax configuration was not persisted' USING ERRCODE='23514';
            END IF;
            IF v_existing.organization_id IS DISTINCT FROM v_org_id
               OR v_existing.membership_plan_id IS DISTINCT FROM p_membership_plan_id
               OR v_existing.tax_code_id IS DISTINCT FROM p_tax_code_id
               OR v_existing.pricing_mode IS DISTINCT FROM p_pricing_mode
               OR v_existing.effective_from IS DISTINCT FROM p_effective_from
               OR v_existing.effective_until IS DISTINCT FROM p_effective_until
               OR v_existing.status IS DISTINCT FROM 'active' THEN
                RAISE EXCEPTION 'P4D membership plan tax configuration replay conflict' USING ERRCODE='23505';
            END IF;
            RETURN QUERY SELECT p_profile_id, v_org_id, p_membership_plan_id, v_row_count = 1, v_row_count = 0;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.upsert_member_billing_party(
            p_member_id uuid,
            p_billing_address text,
            p_place_of_supply_state_code text
        )
        RETURNS TABLE(
            billing_party_id uuid,
            organization_id uuid,
            member_id uuid,
            buyer_kind text,
            party_type text,
            gst_treatment text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_member public.members%ROWTYPE;
            v_current_org_id uuid;
            v_billing_party_id uuid;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D member billing party requires app_runtime' USING ERRCODE='42501';
            END IF;
            BEGIN
                v_current_org_id := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'P4D member billing party requires valid tenant context' USING ERRCODE='22023';
            END;
            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D member billing party requires tenant context' USING ERRCODE='22023';
            END IF;
            IF p_member_id IS NULL THEN
                RAISE EXCEPTION 'MEMBER_BILLING_PROFILE_REQUIRED' USING ERRCODE='22023';
            END IF;
            IF p_billing_address IS NULL OR pg_catalog.btrim(p_billing_address) = '' THEN
                RAISE EXCEPTION 'MEMBER_BILLING_PROFILE_REQUIRED' USING ERRCODE='22023';
            END IF;
            IF p_place_of_supply_state_code IS NULL OR p_place_of_supply_state_code !~ '^[0-9]{2}$' THEN
                RAISE EXCEPTION 'MEMBER_BILLING_PROFILE_REQUIRED' USING ERRCODE='22023';
            END IF;

            SELECT m.* INTO v_member
            FROM public.members m
            WHERE m.id = p_member_id
              AND m.org_id = v_current_org_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D member billing party member not found in tenant' USING ERRCODE='P0002';
            END IF;
            IF v_member.name IS NULL OR pg_catalog.btrim(v_member.name) = '' THEN
                RAISE EXCEPTION 'MEMBER_BILLING_PROFILE_REQUIRED' USING ERRCODE='22023';
            END IF;

            SELECT bp.id INTO v_billing_party_id
            FROM finance.billing_parties bp
            WHERE bp.organization_id = v_current_org_id
              AND bp.member_id = p_member_id
              AND bp.buyer_kind = 'member'
            FOR UPDATE;

            IF FOUND THEN
                UPDATE finance.billing_parties bp
                SET billing_name = v_member.name,
                    party_type = 'individual',
                    gst_treatment = 'b2c',
                    billing_address = pg_catalog.btrim(p_billing_address),
                    place_of_supply_state_code = p_place_of_supply_state_code,
                    status = 'active',
                    updated_at = clock_timestamp()
                WHERE bp.id = v_billing_party_id
                RETURNING bp.id INTO v_billing_party_id;
            ELSE
                INSERT INTO finance.billing_parties(
                    organization_id, buyer_kind, member_id, billing_name, party_type, gst_treatment,
                    billing_address, place_of_supply_state_code, status, metadata_json
                )
                VALUES (
                    v_current_org_id, 'member', p_member_id, v_member.name, 'individual', 'b2c',
                    pg_catalog.btrim(p_billing_address), p_place_of_supply_state_code, 'active',
                    jsonb_build_object('source', 'member_billing_party_admin')
                )
                RETURNING billing_parties.id INTO v_billing_party_id;
            END IF;

            RETURN QUERY SELECT v_billing_party_id, v_current_org_id, p_member_id, 'member'::text, 'individual'::text, 'b2c'::text;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.resolve_member_subscription_checkout_inputs(
            p_subscription_id uuid
        )
        RETURNS TABLE(
            organization_id uuid,
            subscription_id uuid,
            branch_id uuid,
            membership_plan_id uuid,
            primary_member_id uuid,
            billing_party_id uuid,
            legal_entity_id uuid,
            gst_registration_id uuid,
            division_id uuid,
            brand_id uuid,
            tax_code_id uuid,
            currency_code char(3),
            supply_date date,
            line_description text,
            quantity numeric,
            unit_price numeric,
            hsn_sac text,
            gst_rate_basis_points integer,
            pricing_mode text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_source public.member_subscriptions_v2%ROWTYPE;
            v_party finance.billing_parties%ROWTYPE;
            v_branch finance.branch_accounting_profiles%ROWTYPE;
            v_tax_profile finance.membership_plan_tax_profiles%ROWTYPE;
            v_tax finance.tax_codes%ROWTYPE;
            v_current_org_id uuid;
            v_count integer := 0;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D member subscription checkout requires app_runtime' USING ERRCODE='42501';
            END IF;
            BEGIN
                v_current_org_id := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'P4D member subscription checkout requires valid tenant context' USING ERRCODE='22023';
            END;
            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D member subscription checkout requires tenant context' USING ERRCODE='22023';
            END IF;

            SELECT s.* INTO v_source
            FROM public.member_subscriptions_v2 s
            WHERE s.id = p_subscription_id
              AND s.org_id = v_current_org_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D member subscription checkout source not found in tenant' USING ERRCODE='P0002';
            END IF;
            IF v_source.price_snapshot IS NULL OR v_source.price_snapshot < 0 OR v_source.currency_code IS NULL OR v_source.currency_code !~ '^[A-Z]{3}$' THEN
                RAISE EXCEPTION 'P4D member subscription checkout source amount/currency invalid' USING ERRCODE='23514';
            END IF;

            SELECT bp.* INTO v_party
            FROM finance.billing_parties bp
            WHERE bp.organization_id = v_source.org_id
              AND bp.member_id = v_source.primary_member_id
              AND bp.buyer_kind = 'member'
              AND bp.party_type = 'individual'
              AND bp.gst_treatment = 'b2c'
              AND bp.status = 'active';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'MEMBER_BILLING_PROFILE_REQUIRED' USING ERRCODE='P0002';
            END IF;

            SELECT count(*) INTO v_count
            FROM finance.branch_accounting_profiles p
            JOIN finance.gst_registrations g ON g.id = p.gst_registration_id
            JOIN finance.divisions d ON d.id = p.division_id
            JOIN finance.brands b ON b.id = p.brand_id
            WHERE p.organization_id = v_source.org_id
              AND p.branch_id = v_source.branch_id
              AND p.status = 'active'
              AND p.effective_from <= v_source.start_date
              AND (p.effective_until IS NULL OR v_source.start_date < p.effective_until)
              AND g.legal_entity_id = p.legal_entity_id
              AND d.legal_entity_id = p.legal_entity_id
              AND b.legal_entity_id = p.legal_entity_id
              AND b.division_id = p.division_id;
            IF v_count = 0 THEN
                RAISE EXCEPTION 'CONFIGURATION_REQUIRED' USING ERRCODE='P0002';
            END IF;
            IF v_count > 1 THEN
                RAISE EXCEPTION 'CONFIGURATION_AMBIGUOUS' USING ERRCODE='23514';
            END IF;
            SELECT p.* INTO v_branch
            FROM finance.branch_accounting_profiles p
            JOIN finance.gst_registrations g ON g.id = p.gst_registration_id
            JOIN finance.divisions d ON d.id = p.division_id
            JOIN finance.brands b ON b.id = p.brand_id
            WHERE p.organization_id = v_source.org_id
              AND p.branch_id = v_source.branch_id
              AND p.status = 'active'
              AND p.effective_from <= v_source.start_date
              AND (p.effective_until IS NULL OR v_source.start_date < p.effective_until)
              AND g.legal_entity_id = p.legal_entity_id
              AND d.legal_entity_id = p.legal_entity_id
              AND b.legal_entity_id = p.legal_entity_id
              AND b.division_id = p.division_id;

            SELECT count(*) INTO v_count
            FROM finance.membership_plan_tax_profiles p
            JOIN finance.tax_codes t ON t.id = p.tax_code_id
            WHERE p.organization_id = v_source.org_id
              AND p.membership_plan_id = v_source.membership_plan_id
              AND p.status = 'active'
              AND p.effective_from <= v_source.start_date
              AND (p.effective_until IS NULL OR v_source.start_date < p.effective_until)
              AND t.status = 'active';
            IF v_count = 0 THEN
                RAISE EXCEPTION 'CONFIGURATION_REQUIRED' USING ERRCODE='P0002';
            END IF;
            IF v_count > 1 THEN
                RAISE EXCEPTION 'CONFIGURATION_AMBIGUOUS' USING ERRCODE='23514';
            END IF;
            SELECT p.* INTO v_tax_profile
            FROM finance.membership_plan_tax_profiles p
            JOIN finance.tax_codes t ON t.id = p.tax_code_id
            WHERE p.organization_id = v_source.org_id
              AND p.membership_plan_id = v_source.membership_plan_id
              AND p.status = 'active'
              AND p.effective_from <= v_source.start_date
              AND (p.effective_until IS NULL OR v_source.start_date < p.effective_until)
              AND t.status = 'active';
            SELECT t.* INTO v_tax
            FROM finance.tax_codes t
            WHERE t.id = v_tax_profile.tax_code_id
              AND t.status = 'active';

            RETURN QUERY SELECT
                v_source.org_id, v_source.id, v_source.branch_id, v_source.membership_plan_id, v_source.primary_member_id,
                v_party.id, v_branch.legal_entity_id, v_branch.gst_registration_id, v_branch.division_id, v_branch.brand_id,
                v_tax_profile.tax_code_id, v_source.currency_code::char(3), v_source.start_date,
                ('Membership subscription ' || v_source.subscription_code)::text, 1::numeric, v_source.price_snapshot::numeric,
                v_tax.hsn_sac::text, v_tax.gst_rate_basis_points, v_tax_profile.pricing_mode;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.record_member_subscription_checkout_binding(
            p_subscription_id uuid,
            p_invoice_id uuid,
            p_checkout_intent_id uuid
        )
        RETURNS TABLE(
            subscription_id uuid,
            invoice_id uuid,
            checkout_intent_id uuid,
            organization_id uuid,
            inserted boolean,
            replayed boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_source_org uuid;
            v_invoice_org uuid;
            v_intent_org uuid;
            v_current_org_id uuid;
            v_row_count integer := 0;
            v_binding record;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D member subscription checkout binding requires app_runtime' USING ERRCODE='42501';
            END IF;
            BEGIN
                v_current_org_id := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'P4D member subscription checkout binding requires valid tenant context' USING ERRCODE='22023';
            END;
            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D member subscription checkout binding requires tenant context' USING ERRCODE='22023';
            END IF;
            SELECT s.org_id INTO v_source_org FROM public.member_subscriptions_v2 s WHERE s.id = p_subscription_id;
            SELECT i.organization_id INTO v_invoice_org FROM finance.invoices i WHERE i.id = p_invoice_id;
            SELECT p.organization_id INTO v_intent_org FROM finance.payments p WHERE p.id = p_checkout_intent_id;
            IF v_source_org IS NULL OR v_invoice_org IS NULL OR v_intent_org IS NULL THEN
                RAISE EXCEPTION 'P4D member subscription checkout binding target missing or unbound' USING ERRCODE='P0002';
            END IF;
            IF v_current_org_id IS DISTINCT FROM v_source_org OR v_invoice_org IS DISTINCT FROM v_source_org OR v_intent_org IS DISTINCT FROM v_source_org THEN
                RAISE EXCEPTION 'P4D member subscription checkout binding tenant mismatch' USING ERRCODE='23514';
            END IF;

            INSERT INTO finance.member_subscription_checkout_bindings(organization_id, subscription_id, invoice_id, checkout_intent_id)
            VALUES (v_source_org, p_subscription_id, p_invoice_id, p_checkout_intent_id)
            ON CONFLICT ON CONSTRAINT uq_member_subscription_checkout_bindings_subscription DO NOTHING;
            GET DIAGNOSTICS v_row_count = ROW_COUNT;

            SELECT b.organization_id, b.subscription_id, b.invoice_id, b.checkout_intent_id
            INTO v_binding
            FROM finance.member_subscription_checkout_bindings b
            WHERE b.subscription_id = p_subscription_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D member subscription checkout binding was not persisted' USING ERRCODE='23514';
            END IF;
            IF v_binding.organization_id IS DISTINCT FROM v_source_org
               OR v_binding.invoice_id IS DISTINCT FROM p_invoice_id
               OR v_binding.checkout_intent_id IS DISTINCT FROM p_checkout_intent_id THEN
                RAISE EXCEPTION 'P4D member subscription checkout binding replay conflict' USING ERRCODE='23505';
            END IF;
            RETURN QUERY SELECT p_subscription_id, p_invoice_id, p_checkout_intent_id, v_source_org, v_row_count = 1, v_row_count = 0;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.attach_member_subscription_checkout_provider_order(
            p_subscription_id uuid,
            p_checkout_intent_id uuid,
            p_provider_order_ref text
        )
        RETURNS TABLE(
            checkout_intent_id uuid,
            provider_order_ref text,
            attached boolean,
            replayed boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_current_org_id uuid;
            v_payment finance.payments%ROWTYPE;
            v_binding record;
            v_row_count integer := 0;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D member subscription checkout provider binding requires app_runtime' USING ERRCODE='42501';
            END IF;
            BEGIN
                v_current_org_id := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'P4D member subscription checkout provider binding requires valid tenant context' USING ERRCODE='22023';
            END;
            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D member subscription checkout provider binding requires tenant context' USING ERRCODE='22023';
            END IF;
            IF p_provider_order_ref IS NULL OR pg_catalog.btrim(p_provider_order_ref) = '' OR p_provider_order_ref LIKE 'intent_%' THEN
                RAISE EXCEPTION 'P4D member subscription checkout provider order ref invalid' USING ERRCODE='22023';
            END IF;

            SELECT b.organization_id, b.subscription_id, b.checkout_intent_id
            INTO v_binding
            FROM finance.member_subscription_checkout_bindings b
            WHERE b.subscription_id = p_subscription_id
              AND b.checkout_intent_id = p_checkout_intent_id
              AND b.organization_id = v_current_org_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D member subscription checkout binding not found for provider attach' USING ERRCODE='P0002';
            END IF;

            SELECT p.* INTO v_payment
            FROM finance.payments p
            WHERE p.id = p_checkout_intent_id
              AND p.organization_id = v_current_org_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D member subscription checkout intent not found for provider attach' USING ERRCODE='P0002';
            END IF;
            IF v_payment.status <> 'created' THEN
                RAISE EXCEPTION 'P4D member subscription checkout intent is not provider-attach eligible' USING ERRCODE='23514';
            END IF;
            IF v_payment.provider_order_ref = p_provider_order_ref THEN
                RETURN QUERY SELECT p_checkout_intent_id, p_provider_order_ref, false, true;
                RETURN;
            END IF;
            IF v_payment.provider_order_ref IS NOT NULL AND v_payment.provider_order_ref NOT LIKE 'intent_%' THEN
                RAISE EXCEPTION 'P4D member subscription checkout provider replay conflict' USING ERRCODE='23505';
            END IF;

            UPDATE finance.payments p
            SET provider_order_ref = p_provider_order_ref,
                updated_at = clock_timestamp()
            WHERE p.id = p_checkout_intent_id
              AND p.organization_id = v_current_org_id;
            GET DIAGNOSTICS v_row_count = ROW_COUNT;
            IF v_row_count <> 1 THEN
                RAISE EXCEPTION 'P4D member subscription checkout provider attach did not update exactly one row' USING ERRCODE='23514';
            END IF;
            RETURN QUERY SELECT p_checkout_intent_id, p_provider_order_ref, true, false;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.reserve_finance_idempotency(
            p_scope text,
            p_idempotency_key text,
            p_request_hash_sha256 text,
            p_organization_id uuid,
            p_expires_at timestamptz
        )
        RETURNS TABLE(
            id uuid,
            organization_id uuid,
            scope text,
            idempotency_key text,
            request_hash_sha256 text,
            status text,
            response_ref text,
            created_at timestamptz,
            expires_at timestamptz,
            inserted boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_current_org_id uuid;
            v_inserted finance.idempotency_keys%ROWTYPE;
            v_existing finance.idempotency_keys%ROWTYPE;
            v_inserted_count integer := 0;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D finance idempotency requires app_runtime' USING ERRCODE='42501';
            END IF;
            BEGIN
                v_current_org_id := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'P4D finance idempotency requires valid tenant context' USING ERRCODE='22023';
            END;
            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D finance idempotency requires tenant context' USING ERRCODE='22023';
            END IF;
            IF p_organization_id IS NOT NULL AND p_organization_id IS DISTINCT FROM v_current_org_id THEN
                RAISE EXCEPTION 'P4D finance idempotency unavailable' USING ERRCODE='42501';
            END IF;
            IF p_scope NOT IN (
                'finance.invoice.create',
                'finance.invoice.issue',
                'finance.billing_party.create',
                'finance.checkout_intent.create',
                'finance.checkout_callback.record',
                'finance.provider.capture.confirm',
                'finance.payment.record',
                'finance.payment.event.record',
                'finance.payment.allocate',
                'finance.payment.apply',
                'finance.payment.reconcile_settlement',
                'finance.credit_note.create',
                'finance.refund_intent.create',
                'finance.ledger.post'
            ) THEN
                RAISE EXCEPTION 'P4D finance idempotency scope unavailable' USING ERRCODE='42501';
            END IF;
            IF p_idempotency_key IS NULL OR pg_catalog.btrim(p_idempotency_key) = '' OR pg_catalog.length(p_idempotency_key) > 200 THEN
                RAISE EXCEPTION 'P4D finance idempotency key invalid' USING ERRCODE='22023';
            END IF;
            IF p_request_hash_sha256 IS NULL OR p_request_hash_sha256 !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'P4D finance idempotency request hash invalid' USING ERRCODE='22023';
            END IF;
            IF p_expires_at IS NULL OR p_expires_at <= pg_catalog.clock_timestamp() THEN
                RAISE EXCEPTION 'P4D finance idempotency expiry invalid' USING ERRCODE='22023';
            END IF;

            INSERT INTO finance.idempotency_keys(
                organization_id, scope, idempotency_key, request_hash_sha256, status, expires_at
            )
            VALUES (
                v_current_org_id, p_scope, p_idempotency_key, p_request_hash_sha256::char(64), 'processing', p_expires_at
            )
            ON CONFLICT ON CONSTRAINT uq_finance_idempotency_keys_scope_key DO NOTHING
            RETURNING * INTO v_inserted;
            GET DIAGNOSTICS v_inserted_count = ROW_COUNT;
            IF v_inserted_count = 1 THEN
                RETURN QUERY SELECT
                    v_inserted.id, v_inserted.organization_id, v_inserted.scope::text, v_inserted.idempotency_key::text,
                    v_inserted.request_hash_sha256::text, v_inserted.status::text, v_inserted.response_ref::text,
                    v_inserted.created_at, v_inserted.expires_at, true;
                RETURN;
            END IF;

            SELECT * INTO v_existing
            FROM finance.idempotency_keys key_data
            WHERE key_data.organization_id = v_current_org_id
              AND key_data.scope = p_scope
              AND key_data.idempotency_key = p_idempotency_key
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D finance idempotency reservation disappeared' USING ERRCODE='40001';
            END IF;
            IF v_existing.organization_id IS DISTINCT FROM v_current_org_id THEN
                RAISE EXCEPTION 'P4D finance idempotency unavailable' USING ERRCODE='42501';
            END IF;
            IF v_existing.request_hash_sha256::text IS DISTINCT FROM p_request_hash_sha256 THEN
                RAISE EXCEPTION 'P4D finance idempotency request conflict' USING ERRCODE='23505';
            END IF;
            RETURN QUERY SELECT
                v_existing.id, v_existing.organization_id, v_existing.scope::text, v_existing.idempotency_key::text,
                v_existing.request_hash_sha256::text, v_existing.status::text, v_existing.response_ref::text,
                v_existing.created_at, v_existing.expires_at, false;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.complete_finance_idempotency(
            p_id uuid,
            p_response_ref text
        )
        RETURNS TABLE(
            id uuid,
            organization_id uuid,
            scope text,
            idempotency_key text,
            request_hash_sha256 text,
            status text,
            response_ref text,
            created_at timestamptz,
            expires_at timestamptz
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_current_org_id uuid;
            v_updated finance.idempotency_keys%ROWTYPE;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D finance idempotency completion requires app_runtime' USING ERRCODE='42501';
            END IF;
            BEGIN
                v_current_org_id := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'P4D finance idempotency completion requires valid tenant context' USING ERRCODE='22023';
            END;
            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D finance idempotency completion requires tenant context' USING ERRCODE='22023';
            END IF;
            IF p_id IS NULL OR p_response_ref IS NULL OR pg_catalog.btrim(p_response_ref) = '' THEN
                RAISE EXCEPTION 'P4D finance idempotency completion invalid' USING ERRCODE='22023';
            END IF;

            UPDATE finance.idempotency_keys key_data
            SET status = 'succeeded', response_ref = p_response_ref
            WHERE key_data.id = p_id
              AND key_data.organization_id = v_current_org_id
              AND key_data.status = 'processing'
            RETURNING * INTO v_updated;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D finance idempotency completion unavailable' USING ERRCODE='42501';
            END IF;
            RETURN QUERY SELECT
                v_updated.id, v_updated.organization_id, v_updated.scope::text, v_updated.idempotency_key::text,
                v_updated.request_hash_sha256::text, v_updated.status::text, v_updated.response_ref::text,
                v_updated.created_at, v_updated.expires_at;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_secure.resolve_finance_invoice_result(
            p_invoice_id uuid
        )
        RETURNS TABLE(
            invoice_id uuid,
            status text,
            official_invoice_number text,
            brand_reference text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_current_org_id uuid;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D finance invoice result requires app_runtime' USING ERRCODE='42501';
            END IF;
            BEGIN
                v_current_org_id := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'P4D finance invoice result requires valid tenant context' USING ERRCODE='22023';
            END;
            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D finance invoice result requires tenant context' USING ERRCODE='22023';
            END IF;
            RETURN QUERY
            SELECT i.id, i.status::text, i.official_invoice_number::text, i.brand_reference::text
            FROM finance.invoices i
            WHERE i.id = p_invoice_id
              AND i.organization_id = v_current_org_id;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.persist_finance_draft_invoice(
            p_idempotency_id uuid,
            p_organization_id uuid,
            p_billing_party_id uuid,
            p_legal_entity_id uuid,
            p_gst_registration_id uuid,
            p_division_id uuid,
            p_brand_id uuid,
            p_supply_date date,
            p_financial_year text,
            p_currency_code text,
            p_seller_legal_name text,
            p_seller_gstin text,
            p_seller_pan text,
            p_seller_registered_address text,
            p_seller_state_code text,
            p_buyer_billing_name text,
            p_buyer_address text,
            p_buyer_gstin text,
            p_buyer_pan text,
            p_buyer_place_of_supply_state_code text,
            p_buyer_gst_treatment text,
            p_gst_supply_type text,
            p_subtotal_amount numeric,
            p_discount_amount numeric,
            p_taxable_amount numeric,
            p_total_tax_amount numeric,
            p_grand_total_amount numeric,
            p_lines jsonb
        )
        RETURNS TABLE(
            id uuid,
            organization_id uuid,
            billing_party_id uuid,
            legal_entity_id uuid,
            gst_registration_id uuid,
            division_id uuid,
            brand_id uuid,
            invoice_series_id uuid,
            brand_ref_series_id uuid,
            financial_year text,
            official_invoice_number text,
            brand_reference text,
            status text,
            currency_code text,
            seller_legal_name text,
            seller_gstin text,
            seller_pan text,
            seller_registered_address text,
            seller_state_code text,
            buyer_billing_name text,
            buyer_address text,
            buyer_gstin text,
            buyer_pan text,
            buyer_place_of_supply_state_code text,
            buyer_gst_treatment text,
            gst_supply_type text,
            subtotal_amount numeric,
            discount_amount numeric,
            taxable_amount numeric,
            total_tax_amount numeric,
            grand_total_amount numeric,
            issued_at timestamptz,
            cancelled_at timestamptz,
            metadata_json jsonb,
            created_at timestamptz,
            updated_at timestamptz
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_current_org_id uuid;
            v_invoice finance.invoices%ROWTYPE;
            v_line_count integer;
            v_sum_subtotal numeric(14,2);
            v_sum_discount numeric(14,2);
            v_sum_taxable numeric(14,2);
            v_sum_tax numeric(14,2);
            v_sum_grand numeric(14,2);
            v_allowed_keys text[] := ARRAY[
                'line_number','description','hsn_sac','quantity','unit_amount','discount_amount','taxable_amount',
                'gst_rate_basis_points','cgst_amount','sgst_amount','igst_amount','total_tax_amount','line_total_amount','pricing_mode'
            ];
            v_key text;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D finance draft persistence requires app_runtime' USING ERRCODE='42501';
            END IF;
            BEGIN
                v_current_org_id := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'P4D finance draft persistence requires valid tenant context' USING ERRCODE='22023';
            END;
            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D finance draft persistence requires tenant context' USING ERRCODE='22023';
            END IF;
            IF p_organization_id IS NULL OR p_organization_id IS DISTINCT FROM v_current_org_id THEN
                RAISE EXCEPTION 'P4D finance draft persistence unavailable' USING ERRCODE='42501';
            END IF;
            IF p_idempotency_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM finance.idempotency_keys idem
                WHERE idem.id = p_idempotency_id
                  AND idem.organization_id = v_current_org_id
                  AND idem.scope = 'finance.invoice.create'
                  AND idem.status = 'processing'
                FOR UPDATE
            ) THEN
                RAISE EXCEPTION 'P4D finance draft persistence requires active idempotency reservation' USING ERRCODE='42501';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM finance.billing_parties bp
                WHERE bp.id = p_billing_party_id
                  AND bp.organization_id = v_current_org_id
            ) THEN
                RAISE EXCEPTION 'P4D finance draft billing party unavailable' USING ERRCODE='42501';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM finance.branch_accounting_profiles profile
                JOIN public.org_branches br
                  ON br.id = profile.branch_id
                 AND br.org_id = profile.organization_id
                JOIN finance.legal_entities le
                  ON le.id = profile.legal_entity_id
                 AND le.status = 'active'
                JOIN finance.gst_registrations gr
                  ON gr.id = profile.gst_registration_id
                 AND gr.legal_entity_id = profile.legal_entity_id
                 AND gr.status = 'active'
                JOIN finance.divisions dv
                  ON dv.id = profile.division_id
                 AND dv.legal_entity_id = profile.legal_entity_id
                 AND dv.status = 'active'
                JOIN finance.brands bd
                  ON bd.id = profile.brand_id
                 AND bd.legal_entity_id = profile.legal_entity_id
                 AND bd.division_id = profile.division_id
                 AND bd.status = 'active'
                JOIN public.organizations org
                  ON org.id = profile.organization_id
                 AND org.is_active
                WHERE profile.organization_id = v_current_org_id
                  AND profile.legal_entity_id = p_legal_entity_id
                  AND profile.gst_registration_id = p_gst_registration_id
                  AND profile.division_id = p_division_id
                  AND profile.brand_id = p_brand_id
                  AND profile.status = 'active'
                  AND profile.effective_from <= p_supply_date
                  AND (profile.effective_until IS NULL OR p_supply_date < profile.effective_until)
            ) THEN
                RAISE EXCEPTION 'P4D finance draft accounting master data unavailable' USING ERRCODE='42501';
            END IF;
            IF p_financial_year !~ '^[0-9]{4}$' OR p_currency_code !~ '^[A-Z]{3}$' THEN
                RAISE EXCEPTION 'P4D finance draft code fields invalid' USING ERRCODE='22023';
            END IF;
            IF p_seller_legal_name IS NULL OR pg_catalog.btrim(p_seller_legal_name) = ''
               OR p_seller_gstin IS NULL OR p_seller_gstin !~ '^[0-9]{2}[A-Z0-9]{13}$'
               OR p_seller_registered_address IS NULL OR pg_catalog.btrim(p_seller_registered_address) = ''
               OR p_seller_state_code IS NULL OR p_seller_state_code !~ '^[0-9]{2}$'
               OR p_buyer_billing_name IS NULL OR pg_catalog.btrim(p_buyer_billing_name) = ''
               OR p_buyer_address IS NULL OR pg_catalog.btrim(p_buyer_address) = ''
               OR p_buyer_place_of_supply_state_code IS NULL OR p_buyer_place_of_supply_state_code !~ '^[0-9]{2}$'
               OR p_buyer_gst_treatment NOT IN ('b2c','b2b')
               OR p_gst_supply_type NOT IN ('intra_state','inter_state') THEN
                RAISE EXCEPTION 'P4D finance draft snapshot invalid' USING ERRCODE='22023';
            END IF;
            IF p_buyer_gst_treatment = 'b2b' AND (p_buyer_gstin IS NULL OR p_buyer_gstin !~ '^[0-9]{2}[A-Z0-9]{13}$') THEN
                RAISE EXCEPTION 'P4D finance draft b2b buyer GSTIN required' USING ERRCODE='22023';
            END IF;
            IF p_subtotal_amount < 0 OR p_discount_amount < 0 OR p_taxable_amount < 0 OR p_total_tax_amount < 0 OR p_grand_total_amount < 0 THEN
                RAISE EXCEPTION 'P4D finance draft totals invalid' USING ERRCODE='22023';
            END IF;
            IF p_lines IS NULL OR pg_catalog.jsonb_typeof(p_lines) <> 'array' OR pg_catalog.jsonb_array_length(p_lines) = 0 THEN
                RAISE EXCEPTION 'P4D finance draft requires line array' USING ERRCODE='22023';
            END IF;
            FOR v_key IN
                SELECT key_name
                FROM pg_catalog.jsonb_array_elements(p_lines) AS line_data(line_json)
                CROSS JOIN LATERAL pg_catalog.jsonb_object_keys(line_data.line_json) AS keys(key_name)
                WHERE NOT (keys.key_name = ANY(v_allowed_keys))
            LOOP
                RAISE EXCEPTION 'P4D finance draft line contains unknown field' USING ERRCODE='22023';
            END LOOP;
            WITH parsed AS (
                SELECT *
                FROM pg_catalog.jsonb_to_recordset(p_lines) AS line_data(
                    line_number integer,
                    description text,
                    hsn_sac text,
                    quantity numeric,
                    unit_amount numeric,
                    discount_amount numeric,
                    taxable_amount numeric,
                    gst_rate_basis_points integer,
                    cgst_amount numeric,
                    sgst_amount numeric,
                    igst_amount numeric,
                    total_tax_amount numeric,
                    line_total_amount numeric,
                    pricing_mode text
                )
            ), assertions AS (
                SELECT pg_catalog.count(*)::integer AS line_count,
                       pg_catalog.count(DISTINCT parsed.line_number)::integer AS distinct_line_count,
                       pg_catalog.min(parsed.line_number) AS min_line_number,
                       pg_catalog.max(parsed.line_number) AS max_line_number,
                       coalesce(pg_catalog.sum(parsed.quantity * parsed.unit_amount), 0::numeric)::numeric(14,2) AS sum_subtotal,
                       coalesce(pg_catalog.sum(parsed.discount_amount), 0::numeric)::numeric(14,2) AS sum_discount,
                       coalesce(pg_catalog.sum(parsed.taxable_amount), 0::numeric)::numeric(14,2) AS sum_taxable,
                       coalesce(pg_catalog.sum(parsed.total_tax_amount), 0::numeric)::numeric(14,2) AS sum_tax,
                       coalesce(pg_catalog.sum(parsed.line_total_amount), 0::numeric)::numeric(14,2) AS sum_grand,
                       pg_catalog.count(*) FILTER (
                         WHERE parsed.line_number IS NULL OR parsed.description IS NULL OR pg_catalog.btrim(parsed.description) = ''
                            OR parsed.quantity IS NULL OR parsed.quantity <= 0
                            OR parsed.unit_amount IS NULL OR parsed.unit_amount < 0
                            OR parsed.discount_amount IS NULL OR parsed.discount_amount < 0
                            OR parsed.taxable_amount IS NULL OR parsed.taxable_amount < 0
                            OR parsed.gst_rate_basis_points IS NULL OR parsed.gst_rate_basis_points < 0
                            OR parsed.cgst_amount IS NULL OR parsed.cgst_amount < 0
                            OR parsed.sgst_amount IS NULL OR parsed.sgst_amount < 0
                            OR parsed.igst_amount IS NULL OR parsed.igst_amount < 0
                            OR parsed.total_tax_amount IS NULL OR parsed.total_tax_amount < 0
                            OR parsed.line_total_amount IS NULL OR parsed.line_total_amount < 0
                            OR parsed.pricing_mode NOT IN ('tax_exclusive','tax_inclusive')
                            OR ((parsed.cgst_amount = 0 AND parsed.sgst_amount = 0) OR parsed.igst_amount = 0) IS NOT TRUE
                            OR (parsed.cgst_amount + parsed.sgst_amount + parsed.igst_amount)::numeric(14,2) <> parsed.total_tax_amount::numeric(14,2)
                            OR (parsed.taxable_amount + parsed.total_tax_amount)::numeric(14,2) <> parsed.line_total_amount::numeric(14,2)
                       ) AS invalid_line_count
                FROM parsed
            )
            SELECT assertions.line_count, assertions.sum_subtotal, assertions.sum_discount, assertions.sum_taxable, assertions.sum_tax, assertions.sum_grand
            INTO v_line_count, v_sum_subtotal, v_sum_discount, v_sum_taxable, v_sum_tax, v_sum_grand
            FROM assertions
            WHERE assertions.line_count = assertions.distinct_line_count
              AND assertions.min_line_number = 1
              AND assertions.max_line_number = assertions.line_count
              AND assertions.invalid_line_count = 0;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D finance draft line structure invalid' USING ERRCODE='22023';
            END IF;
            IF v_sum_subtotal IS DISTINCT FROM p_subtotal_amount::numeric(14,2)
               OR v_sum_discount IS DISTINCT FROM p_discount_amount::numeric(14,2)
               OR v_sum_taxable IS DISTINCT FROM p_taxable_amount::numeric(14,2)
               OR v_sum_tax IS DISTINCT FROM p_total_tax_amount::numeric(14,2)
               OR v_sum_grand IS DISTINCT FROM p_grand_total_amount::numeric(14,2) THEN
                RAISE EXCEPTION 'P4D finance draft totals mismatch' USING ERRCODE='23514';
            END IF;

            INSERT INTO finance.invoices(
                organization_id, billing_party_id, legal_entity_id, gst_registration_id, division_id, brand_id,
                financial_year, status, currency_code, seller_legal_name, seller_gstin, seller_pan,
                seller_registered_address, seller_state_code, buyer_billing_name, buyer_address, buyer_gstin,
                buyer_pan, buyer_place_of_supply_state_code, buyer_gst_treatment, gst_supply_type,
                subtotal_amount, discount_amount, taxable_amount, total_tax_amount, grand_total_amount, metadata_json
            ) VALUES (
                v_current_org_id, p_billing_party_id, p_legal_entity_id, p_gst_registration_id, p_division_id, p_brand_id,
                p_financial_year::char(4), 'draft', p_currency_code::char(3), p_seller_legal_name, p_seller_gstin, p_seller_pan,
                p_seller_registered_address, p_seller_state_code::char(2), p_buyer_billing_name, p_buyer_address, p_buyer_gstin,
                p_buyer_pan, p_buyer_place_of_supply_state_code::char(2), p_buyer_gst_treatment, p_gst_supply_type,
                p_subtotal_amount, p_discount_amount, p_taxable_amount, p_total_tax_amount, p_grand_total_amount,
                pg_catalog.jsonb_build_object('supply_date', p_supply_date::text)
            ) RETURNING * INTO v_invoice;

            INSERT INTO finance.invoice_lines(
                invoice_id, line_number, description, hsn_sac, quantity, unit_amount, discount_amount, taxable_amount,
                gst_rate_basis_points, cgst_amount, sgst_amount, igst_amount, total_tax_amount, line_total_amount, pricing_mode
            )
            SELECT v_invoice.id, line_data.line_number, line_data.description, line_data.hsn_sac, line_data.quantity, line_data.unit_amount, line_data.discount_amount, line_data.taxable_amount,
                   line_data.gst_rate_basis_points, line_data.cgst_amount, line_data.sgst_amount, line_data.igst_amount, line_data.total_tax_amount, line_data.line_total_amount, line_data.pricing_mode
            FROM pg_catalog.jsonb_to_recordset(p_lines) AS line_data(
                line_number integer,
                description text,
                hsn_sac text,
                quantity numeric,
                unit_amount numeric,
                discount_amount numeric,
                taxable_amount numeric,
                gst_rate_basis_points integer,
                cgst_amount numeric,
                sgst_amount numeric,
                igst_amount numeric,
                total_tax_amount numeric,
                line_total_amount numeric,
                pricing_mode text
            )
            ORDER BY line_data.line_number;

            RETURN QUERY SELECT
                v_invoice.id, v_invoice.organization_id, v_invoice.billing_party_id, v_invoice.legal_entity_id,
                v_invoice.gst_registration_id, v_invoice.division_id, v_invoice.brand_id, v_invoice.invoice_series_id,
                v_invoice.brand_ref_series_id, v_invoice.financial_year::text, v_invoice.official_invoice_number::text,
                v_invoice.brand_reference::text, v_invoice.status::text, v_invoice.currency_code::text,
                v_invoice.seller_legal_name::text, v_invoice.seller_gstin::text, v_invoice.seller_pan::text,
                v_invoice.seller_registered_address::text, v_invoice.seller_state_code::text,
                v_invoice.buyer_billing_name::text, v_invoice.buyer_address::text, v_invoice.buyer_gstin::text,
                v_invoice.buyer_pan::text, v_invoice.buyer_place_of_supply_state_code::text,
                v_invoice.buyer_gst_treatment::text, v_invoice.gst_supply_type::text,
                v_invoice.subtotal_amount, v_invoice.discount_amount, v_invoice.taxable_amount,
                v_invoice.total_tax_amount, v_invoice.grand_total_amount, v_invoice.issued_at,
                v_invoice.cancelled_at, v_invoice.metadata_json, v_invoice.created_at, v_invoice.updated_at;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.resolve_finance_invoice_accounting_master_data(
            p_organization_id uuid,
            p_legal_entity_id uuid,
            p_gst_registration_id uuid,
            p_division_id uuid,
            p_brand_id uuid,
            p_billing_party_id uuid,
            p_supply_date date
        )
        RETURNS TABLE(
            organization_id uuid,
            legal_entity_id uuid,
            gst_registration_id uuid,
            division_id uuid,
            brand_id uuid,
            billing_party_id uuid,
            seller_legal_name text,
            seller_pan text,
            seller_gstin text,
            seller_registered_address text,
            seller_state_code text,
            division_code text,
            brand_code text,
            buyer_billing_name text,
            buyer_address text,
            buyer_gstin text,
            buyer_pan text,
            buyer_place_of_supply_state_code text,
            buyer_gst_treatment text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_current_org_id uuid;
            v_profile_count integer := 0;
            v_master record;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D finance invoice accounting master data requires app_runtime' USING ERRCODE='42501';
            END IF;
            BEGIN
                v_current_org_id := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'P4D finance invoice accounting master data requires valid tenant context' USING ERRCODE='22023';
            END;
            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D finance invoice accounting master data requires tenant context' USING ERRCODE='22023';
            END IF;
            IF p_organization_id IS NULL
                OR p_legal_entity_id IS NULL
                OR p_gst_registration_id IS NULL
                OR p_division_id IS NULL
                OR p_brand_id IS NULL
                OR p_billing_party_id IS NULL
                OR p_supply_date IS NULL
            THEN
                RAISE EXCEPTION 'P4D finance invoice accounting master data requires complete identifiers' USING ERRCODE='22023';
            END IF;
            IF p_organization_id IS DISTINCT FROM v_current_org_id THEN
                RAISE EXCEPTION 'P4D finance invoice accounting master data unavailable' USING ERRCODE='42501';
            END IF;

            SELECT count(*) INTO v_profile_count
            FROM finance.branch_accounting_profiles p
            JOIN public.org_branches br
              ON br.id = p.branch_id
             AND br.org_id = p.organization_id
            JOIN finance.legal_entities le
              ON le.id = p.legal_entity_id
            JOIN finance.gst_registrations g
              ON g.id = p.gst_registration_id
             AND g.legal_entity_id = p.legal_entity_id
            JOIN finance.divisions d
              ON d.id = p.division_id
             AND d.legal_entity_id = p.legal_entity_id
            JOIN finance.brands b
              ON b.id = p.brand_id
             AND b.legal_entity_id = p.legal_entity_id
             AND b.division_id = p.division_id
            JOIN finance.billing_parties bp
              ON bp.id = p_billing_party_id
             AND bp.organization_id = p.organization_id
            JOIN public.organizations o
              ON o.id = p.organization_id
            WHERE p.organization_id = v_current_org_id
              AND p.legal_entity_id = p_legal_entity_id
              AND p.gst_registration_id = p_gst_registration_id
              AND p.division_id = p_division_id
              AND p.brand_id = p_brand_id
              AND p.status = 'active'
              AND p.effective_from <= p_supply_date
              AND (p.effective_until IS NULL OR p_supply_date < p.effective_until)
              AND o.is_active
              AND le.status = 'active'
              AND g.status = 'active'
              AND d.status = 'active'
              AND b.status = 'active'
              AND bp.status = 'active';
            IF v_profile_count = 0 THEN
                RAISE EXCEPTION 'P4D finance invoice accounting master data unavailable' USING ERRCODE='42501';
            END IF;
            IF v_profile_count > 1 THEN
                RAISE EXCEPTION 'P4D finance invoice accounting master data ambiguous' USING ERRCODE='23514';
            END IF;

            SELECT
                p.organization_id, p.legal_entity_id, p.gst_registration_id, p.division_id, p.brand_id,
                bp.id AS billing_party_id,
                le.legal_name AS seller_legal_name, le.pan AS seller_pan,
                g.gstin AS seller_gstin, g.registered_address AS seller_registered_address, g.state_code::text AS seller_state_code,
                d.code AS division_code, b.code AS brand_code,
                bp.billing_name AS buyer_billing_name, bp.billing_address AS buyer_address, bp.gstin AS buyer_gstin, bp.pan AS buyer_pan,
                bp.place_of_supply_state_code::text AS buyer_place_of_supply_state_code, bp.gst_treatment AS buyer_gst_treatment
            INTO v_master
            FROM finance.branch_accounting_profiles p
            JOIN public.org_branches br
              ON br.id = p.branch_id
             AND br.org_id = p.organization_id
            JOIN finance.legal_entities le
              ON le.id = p.legal_entity_id
            JOIN finance.gst_registrations g
              ON g.id = p.gst_registration_id
             AND g.legal_entity_id = p.legal_entity_id
            JOIN finance.divisions d
              ON d.id = p.division_id
             AND d.legal_entity_id = p.legal_entity_id
            JOIN finance.brands b
              ON b.id = p.brand_id
             AND b.legal_entity_id = p.legal_entity_id
             AND b.division_id = p.division_id
            JOIN finance.billing_parties bp
              ON bp.id = p_billing_party_id
             AND bp.organization_id = p.organization_id
            JOIN public.organizations o
              ON o.id = p.organization_id
            WHERE p.organization_id = v_current_org_id
              AND p.legal_entity_id = p_legal_entity_id
              AND p.gst_registration_id = p_gst_registration_id
              AND p.division_id = p_division_id
              AND p.brand_id = p_brand_id
              AND p.status = 'active'
              AND p.effective_from <= p_supply_date
              AND (p.effective_until IS NULL OR p_supply_date < p.effective_until)
              AND o.is_active
              AND le.status = 'active'
              AND g.status = 'active'
              AND d.status = 'active'
              AND b.status = 'active'
              AND bp.status = 'active';

            RETURN QUERY SELECT
                v_master.organization_id, v_master.legal_entity_id, v_master.gst_registration_id, v_master.division_id,
                v_master.brand_id, v_master.billing_party_id, v_master.seller_legal_name::text, v_master.seller_pan::text,
                v_master.seller_gstin::text, v_master.seller_registered_address::text, v_master.seller_state_code::text,
                v_master.division_code::text, v_master.brand_code::text, v_master.buyer_billing_name::text, v_master.buyer_address::text,
                v_master.buyer_gstin::text, v_master.buyer_pan::text, v_master.buyer_place_of_supply_state_code::text, v_master.buyer_gst_treatment::text;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.resolve_finance_invoice_organization(
            p_organization_id uuid
        )
        RETURNS TABLE(
            organization_id uuid,
            is_active boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_current_org_id uuid;
            v_is_active boolean;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D finance invoice organization requires app_runtime' USING ERRCODE='42501';
            END IF;
            BEGIN
                v_current_org_id := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'P4D finance invoice organization requires valid tenant context' USING ERRCODE='22023';
            END;
            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D finance invoice organization requires tenant context' USING ERRCODE='22023';
            END IF;
            IF p_organization_id IS NULL OR p_organization_id IS DISTINCT FROM v_current_org_id THEN
                RAISE EXCEPTION 'P4D finance invoice organization unavailable' USING ERRCODE='42501';
            END IF;

            SELECT o.is_active
            INTO v_is_active
            FROM public.organizations o
            WHERE o.id = p_organization_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;

            RETURN QUERY SELECT p_organization_id, v_is_active;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.resolve_finance_billing_party_organization(
            p_organization_id uuid
        )
        RETURNS TABLE(
            organization_id uuid,
            is_active boolean,
            synthetic_billing_party_allowed boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_current_org_id uuid;
            v_org record;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'app_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D finance billing-party organization requires app_runtime' USING ERRCODE='42501';
            END IF;
            BEGIN
                v_current_org_id := NULLIF(pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'P4D finance billing-party organization requires valid tenant context' USING ERRCODE='22023';
            END;
            IF v_current_org_id IS NULL THEN
                RAISE EXCEPTION 'P4D finance billing-party organization requires tenant context' USING ERRCODE='22023';
            END IF;
            IF p_organization_id IS NULL OR p_organization_id IS DISTINCT FROM v_current_org_id THEN
                RAISE EXCEPTION 'P4D finance billing-party organization unavailable' USING ERRCODE='42501';
            END IF;

            SELECT o.id, o.is_active, o.name, o.slug, o.business_type, o.description
            INTO v_org
            FROM public.organizations o
            WHERE o.id = p_organization_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;

            RETURN QUERY SELECT
                v_org.id,
                v_org.is_active,
                lower(coalesce(v_org.name, '') || ' ' || coalesce(v_org.slug, '') || ' ' || coalesce(v_org.business_type, '') || ' ' || coalesce(v_org.description, '')) ~ '(test|sandbox|dummy|demo|dev|local|mock|staging|qa)';
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_secure.resolve_branch_refund_required(
            p_source_outbox_id uuid,
            p_worker_id uuid
        )
        RETURNS TABLE(
            outcome text,
            refund_id uuid,
            command_id uuid,
            payment_id uuid,
            organization_id uuid,
            amount numeric,
            currency_code char(3),
            provider_code text,
            provider_payment_ref text,
            eligible_payment_count integer,
            reused_refund boolean,
            reused_command boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public,finance
        SET row_security=on
        AS $$
        DECLARE
            v_source public.branch_outbox_events%ROWTYPE;
            v_payment finance.payments%ROWTYPE;
            v_selected_payment finance.payments%ROWTYPE;
            v_existing_refund finance.refunds%ROWTYPE;
            v_materialized record;
            v_allocated numeric(14,2);
            v_in_scope_allocated numeric(14,2);
            v_refunded numeric(14,2);
            v_remaining numeric(14,2);
            v_selected_remaining numeric(14,2);
            v_existing_refund_count integer := 0;
            v_count integer := 0;
            v_refund_ref text;
            v_reused_refund boolean := false;
            v_found_existing_obligation boolean := false;
        BEGIN
            IF NOT pg_catalog.pg_has_role(session_user, 'worker_runtime', 'MEMBER') THEN
                RAISE EXCEPTION 'P4D refund obligation resolution requires worker_runtime' USING ERRCODE='42501';
            END IF;
            IF p_source_outbox_id IS NULL OR p_worker_id IS NULL THEN
                RAISE EXCEPTION 'P4D refund obligation resolution requires source and worker identity' USING ERRCODE='22023';
            END IF;

            SELECT o.* INTO v_source
            FROM public.branch_outbox_events o
            WHERE o.outbox_id = p_source_outbox_id
              AND o.status = 'processing'
              AND o.leased_by = p_worker_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D refund source lease is not owned by this worker' USING ERRCODE='40001';
            END IF;
            IF v_source.event_type <> 'branch.refund_required' THEN
                RAISE EXCEPTION 'P4D refund obligation requires branch.refund_required source' USING ERRCODE='23514';
            END IF;
            PERFORM 1
            FROM public.org_branches b
            WHERE b.id = v_source.branch_id
              AND b.org_id = v_source.tenant_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'P4D refund source branch tenant mismatch' USING ERRCODE='23514';
            END IF;

            v_refund_ref := 'branch-refund:' || p_source_outbox_id::text;
            SELECT count(*) INTO v_existing_refund_count
            FROM finance.refunds r
            WHERE r.reason_code = v_refund_ref;
            IF v_existing_refund_count > 1 THEN
                RAISE EXCEPTION 'P4D deterministic source refund reason is ambiguous across payments' USING ERRCODE='23514';
            END IF;
            IF v_existing_refund_count = 1 THEN
                SELECT p.* INTO v_selected_payment
                FROM finance.refunds r
                JOIN finance.payments p ON p.id = r.payment_id
                WHERE r.reason_code = v_refund_ref
                  AND p.organization_id = v_source.tenant_id
                  AND p.status IN ('captured','settled')
                FOR UPDATE OF p;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'P4D existing refund obligation is not tenant/payment eligible' USING ERRCODE='23514';
                END IF;
                v_found_existing_obligation := true;
                v_count := 1;
                SELECT r.* INTO v_existing_refund
                FROM finance.refunds r
                WHERE r.payment_id = v_selected_payment.id
                  AND r.reason_code = v_refund_ref
                FOR UPDATE;

                SELECT coalesce(sum(a.allocated_amount), 0)::numeric(14,2),
                       coalesce(sum(a.allocated_amount) FILTER (WHERE b.branch_id = v_source.branch_id AND b.organization_id = v_source.tenant_id), 0)::numeric(14,2)
                INTO v_allocated, v_in_scope_allocated
                FROM finance.payment_allocations a
                JOIN finance.invoices i ON i.id = a.invoice_id
                LEFT JOIN finance.refund_obligation_bindings b ON b.invoice_id = i.id
                WHERE a.payment_id = v_selected_payment.id
                  AND i.organization_id = v_source.tenant_id;

                IF v_allocated <= 0 OR v_in_scope_allocated <= 0 OR v_in_scope_allocated IS DISTINCT FROM v_allocated THEN
                    RAISE EXCEPTION 'P4D existing refund obligation payment is not exclusively bound to source branch' USING ERRCODE='23514';
                END IF;

                SELECT coalesce(sum(r.amount), 0)::numeric(14,2)
                INTO v_refunded
                FROM finance.refunds r
                WHERE r.payment_id = v_selected_payment.id
                  AND r.status <> 'cancelled'
                  AND r.id <> v_existing_refund.id;

                IF v_existing_refund.status <> 'cancelled'
                   AND v_existing_refund.amount > (v_allocated - v_refunded) THEN
                    RAISE EXCEPTION 'P4D existing refund obligation exceeds authoritative refundable balance' USING ERRCODE='23514';
                END IF;
                v_selected_remaining := v_existing_refund.amount;
            END IF;
            IF NOT v_found_existing_obligation THEN
            FOR v_payment IN
                SELECT p.*
                FROM finance.payments p
                WHERE p.organization_id = v_source.tenant_id
                  AND p.status IN ('captured','settled')
                ORDER BY p.created_at, p.id
                FOR UPDATE
            LOOP
                SELECT coalesce(sum(a.allocated_amount), 0)::numeric(14,2),
                       coalesce(sum(a.allocated_amount) FILTER (WHERE b.branch_id = v_source.branch_id AND b.organization_id = v_source.tenant_id), 0)::numeric(14,2)
                INTO v_allocated, v_in_scope_allocated
                FROM finance.payment_allocations a
                JOIN finance.invoices i ON i.id = a.invoice_id
                LEFT JOIN finance.refund_obligation_bindings b ON b.invoice_id = i.id
                WHERE a.payment_id = v_payment.id
                  AND i.organization_id = v_source.tenant_id;

                IF v_in_scope_allocated > 0 AND v_in_scope_allocated IS DISTINCT FROM v_allocated THEN
                    RAISE EXCEPTION 'P4D refund obligation payment allocations span source branch and unrelated business context' USING ERRCODE='23514';
                END IF;

                SELECT coalesce(sum(r.amount), 0)::numeric(14,2)
                INTO v_refunded
                FROM finance.refunds r
                WHERE r.payment_id = v_payment.id
                  AND r.status <> 'cancelled';

                v_remaining := (v_in_scope_allocated - v_refunded)::numeric(14,2);
                IF v_remaining > 0 THEN
                    v_count := v_count + 1;
                    IF v_count = 1 THEN
                        v_selected_payment := v_payment;
                        v_selected_remaining := v_remaining;
                    ELSE
                        RAISE EXCEPTION 'P4D refund obligation is ambiguous across multiple refundable payments' USING ERRCODE='23514';
                    END IF;
                END IF;
            END LOOP;
            END IF;

            IF v_count = 0 THEN
                RETURN QUERY SELECT 'no_refundable_payment'::text,NULL::uuid,NULL::uuid,NULL::uuid,
                    v_source.tenant_id,0::numeric,NULL::char(3),NULL::text,NULL::text,0,false,false;
                RETURN;
            END IF;

            IF v_found_existing_obligation THEN
                v_reused_refund := true;
            ELSE
                SELECT r.* INTO v_existing_refund
                FROM finance.refunds r
                WHERE r.payment_id = v_selected_payment.id
                  AND r.reason_code = v_refund_ref
                FOR UPDATE;
                IF FOUND THEN
                    v_reused_refund := true;
                END IF;
            END IF;
            IF v_reused_refund THEN
                IF v_existing_refund.organization_id IS DISTINCT FROM v_selected_payment.organization_id
                   OR v_existing_refund.currency_code IS DISTINCT FROM v_selected_payment.currency_code
                   OR v_existing_refund.amount IS DISTINCT FROM v_selected_remaining THEN
                    RAISE EXCEPTION 'P4D existing refund obligation authority drift' USING ERRCODE='23514';
                END IF;
                IF v_existing_refund.status = 'cancelled' THEN
                    RETURN QUERY SELECT 'cancelled_refund_suppressed'::text,v_existing_refund.id,NULL::uuid,
                        v_selected_payment.id,v_selected_payment.organization_id,v_existing_refund.amount,v_existing_refund.currency_code,
                        v_selected_payment.provider_code::text,v_selected_payment.provider_payment_ref::text,v_count,true,false;
                    RETURN;
                END IF;
                IF v_existing_refund.status NOT IN ('requested','approved','processing') THEN
                    RAISE EXCEPTION 'P4D existing refund obligation is not execution-eligible' USING ERRCODE='23514';
                END IF;
            ELSE
                INSERT INTO finance.refunds(
                    organization_id,payment_id,legal_entity_id,division_id,brand_id,
                    amount,currency_code,status,reason_code
                )
                VALUES (
                    v_selected_payment.organization_id,v_selected_payment.id,v_selected_payment.legal_entity_id,
                    v_selected_payment.division_id,v_selected_payment.brand_id,v_selected_remaining,
                    v_selected_payment.currency_code,'requested',v_refund_ref
                )
                RETURNING * INTO v_existing_refund;
            END IF;

            SELECT *
            INTO v_materialized
            FROM app_secure.materialize_refund_execution_command(
                v_existing_refund.id,
                'branch.refund_required',
                p_source_outbox_id,
                'branch-refund-required/' || p_source_outbox_id::text
            );

            RETURN QUERY SELECT 'command_materialized'::text,v_existing_refund.id,
                v_materialized.command_id,v_selected_payment.id,v_selected_payment.organization_id,
                v_existing_refund.amount,v_selected_payment.currency_code,
                v_selected_payment.provider_code::text,v_selected_payment.provider_payment_ref::text,
                v_count,v_reused_refund,v_materialized.reused;
        END;
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION app_secure.record_refund_obligation_binding(uuid,uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.record_refund_obligation_binding(uuid,uuid) TO app_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.resolve_branch_refund_required(uuid,uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.resolve_branch_refund_required(uuid,uuid) TO worker_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.upsert_member_billing_party(uuid,text,text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.upsert_member_billing_party(uuid,text,text) TO app_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.resolve_member_subscription_checkout_inputs(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.resolve_member_subscription_checkout_inputs(uuid) TO app_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.record_member_subscription_checkout_binding(uuid,uuid,uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.record_member_subscription_checkout_binding(uuid,uuid,uuid) TO app_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.attach_member_subscription_checkout_provider_order(uuid,uuid,text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.attach_member_subscription_checkout_provider_order(uuid,uuid,text) TO app_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.resolve_finance_invoice_organization(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.resolve_finance_invoice_organization(uuid) TO app_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.resolve_finance_billing_party_organization(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.resolve_finance_billing_party_organization(uuid) TO app_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.resolve_finance_invoice_accounting_master_data(uuid,uuid,uuid,uuid,uuid,uuid,date) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.resolve_finance_invoice_accounting_master_data(uuid,uuid,uuid,uuid,uuid,uuid,date) TO app_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.reserve_finance_idempotency(text,text,text,uuid,timestamp with time zone) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.reserve_finance_idempotency(text,text,text,uuid,timestamp with time zone) TO app_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.complete_finance_idempotency(uuid,text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.complete_finance_idempotency(uuid,text) TO app_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.persist_finance_draft_invoice(uuid,uuid,uuid,uuid,uuid,uuid,uuid,date,text,text,text,text,text,text,text,text,text,text,text,text,text,text,numeric,numeric,numeric,numeric,numeric,jsonb) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION app_secure.resolve_finance_invoice_result(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.resolve_finance_invoice_result(uuid) TO app_runtime")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.persist_finance_draft_invoice(uuid,uuid,uuid,uuid,uuid,uuid,uuid,date,text,text,text,text,text,text,text,text,text,text,text,text,text,text,numeric,numeric,numeric,numeric,numeric,jsonb) TO app_runtime")
    op.execute("GRANT USAGE ON SCHEMA app_secure TO finance_config_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.establish_finance_tax_code(uuid,text,text,text,text,integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.establish_finance_tax_code(uuid,text,text,text,text,integer) TO finance_config_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.establish_branch_accounting_profile(uuid,uuid,uuid,uuid,uuid,uuid,date,date) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.establish_branch_accounting_profile(uuid,uuid,uuid,uuid,uuid,uuid,date,date) TO finance_config_runtime")
    op.execute("REVOKE ALL ON FUNCTION app_secure.establish_membership_plan_tax_profile(uuid,uuid,uuid,text,date,date) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_secure.establish_membership_plan_tax_profile(uuid,uuid,uuid,text,date,date) TO finance_config_runtime")
    op.execute("RESET ROLE")


def _require_function_contract(
    bind,
    *,
    function_name: str,
    normalized_args: str,
    allowed_role: str,
    blocked_roles: tuple[str, ...],
    error_label: str,
) -> int:
    function_oid = _require_exact_function_count(
        bind,
        schema_name="app_secure",
        function_name=function_name,
        normalized_args=normalized_args,
        expected_count=1,
        error_label=f"zd07 {error_label} catalog identity drift",
    )
    row = bind.execute(
        sa.text(
            """
            SELECT p.oid AS function_oid, pg_get_userbyid(p.proowner) AS owner, p.prosecdef,
                   coalesce(p.proconfig::text,'') AS config,
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.aclexplode(coalesce(p.proacl, pg_catalog.acldefault('f', p.proowner))) acl
                       WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                   ) AS public_execute
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE p.oid = CAST(:function_oid AS oid)
            """
        ),
        {"function_oid": function_oid},
    ).mappings().one_or_none()
    if row is None or row["owner"] != _SECURITY_OWNER or not row["prosecdef"]:
        raise RuntimeError(f"zd07 {error_label} owner/security drift")
    if "search_path=pg_catalog, public, finance" not in row["config"] and "search_path=pg_catalog,public,finance" not in row["config"]:
        raise RuntimeError(f"zd07 {error_label} search_path drift")
    if "row_security=on" not in row["config"]:
        raise RuntimeError(f"zd07 {error_label} row_security drift")
    if row["public_execute"]:
        raise RuntimeError(f"zd07 {error_label} PUBLIC execute leaked")
    if not bind.execute(
        sa.text("SELECT pg_catalog.has_function_privilege(:role_name, CAST(:function_oid AS oid), 'EXECUTE')"),
        {"role_name": allowed_role, "function_oid": function_oid},
    ).scalar_one():
        raise RuntimeError(f"zd07 {allowed_role} lacks {error_label} execute")
    for role_name in blocked_roles:
        if bind.execute(
            sa.text("SELECT pg_catalog.has_function_privilege(:role_name, CAST(:function_oid AS oid), 'EXECUTE')"),
            {"role_name": role_name, "function_oid": function_oid},
        ).scalar_one():
            raise RuntimeError(f"zd07 {error_label} execute leaked to {role_name}")
    return function_oid


def _post_install_proof(bind) -> None:
    _require_function_contract(
        bind,
        function_name="resolve_branch_refund_required",
        normalized_args="uuid,uuid",
        allowed_role="worker_runtime",
        blocked_roles=("app_runtime", "auth_runtime", "lifecycle_maintenance_runtime", "finance_config_runtime"),
        error_label="resolver",
    )
    _require_function_contract(
        bind,
        function_name="record_refund_obligation_binding",
        normalized_args="uuid,uuid",
        allowed_role="app_runtime",
        blocked_roles=("auth_runtime", "worker_runtime", "lifecycle_maintenance_runtime", "finance_config_runtime"),
        error_label="producer",
    )
    for function_name, normalized_args, label in (
        ("upsert_member_billing_party", "uuid,text,text", "member billing party capability"),
        ("resolve_member_subscription_checkout_inputs", "uuid", "member subscription checkout resolver"),
        ("record_member_subscription_checkout_binding", "uuid,uuid,uuid", "member subscription checkout binding"),
        ("attach_member_subscription_checkout_provider_order", "uuid,uuid,text", "member subscription provider order attach"),
        ("resolve_finance_invoice_organization", "uuid", "finance invoice organization resolver"),
        ("resolve_finance_billing_party_organization", "uuid", "finance billing-party organization resolver"),
        ("resolve_finance_invoice_accounting_master_data", "uuid,uuid,uuid,uuid,uuid,uuid,date", "finance invoice accounting master-data resolver"),
        ("reserve_finance_idempotency", "text,text,text,uuid,timestampwithtimezone", "finance idempotency reservation"),
        ("complete_finance_idempotency", "uuid,text", "finance idempotency completion"),
        ("persist_finance_draft_invoice", "uuid,uuid,uuid,uuid,uuid,uuid,uuid,date,text,text,text,text,text,text,text,text,text,text,text,text,text,text,numeric,numeric,numeric,numeric,numeric,jsonb", "finance draft invoice persistence"),
        ("resolve_finance_invoice_result", "uuid", "finance invoice result resolver"),
    ):
        _require_function_contract(
            bind,
            function_name=function_name,
            normalized_args=normalized_args,
            allowed_role="app_runtime",
            blocked_roles=("auth_runtime", "worker_runtime", "lifecycle_maintenance_runtime", "finance_config_runtime"),
            error_label=label,
        )
    if not bind.execute(
        sa.text("SELECT pg_catalog.has_schema_privilege('finance_config_runtime', 'app_secure', 'USAGE')")
    ).scalar_one():
        raise RuntimeError("zd07 finance_config_runtime lacks app_secure USAGE")
    for function_name, normalized_args, label in (
        ("establish_finance_tax_code", "uuid,text,text,text,text,integer", "finance tax code configuration"),
        ("establish_branch_accounting_profile", "uuid,uuid,uuid,uuid,uuid,uuid,date,date", "branch accounting configuration"),
        ("establish_membership_plan_tax_profile", "uuid,uuid,uuid,text,date,date", "membership plan tax configuration"),
    ):
        _require_function_contract(
            bind,
            function_name=function_name,
            normalized_args=normalized_args,
            allowed_role="finance_config_runtime",
            blocked_roles=("app_runtime", "auth_runtime", "worker_runtime", "lifecycle_maintenance_runtime"),
            error_label=label,
        )
    for function_name, normalized_args in (
        ("prevent_branch_accounting_profile_invalidity", ""),
        ("prevent_membership_plan_tax_profile_overlap", ""),
    ):
        _require_exact_function_count(
            bind,
            schema_name="finance",
            function_name=function_name,
            normalized_args=normalized_args,
            expected_count=1,
            error_label=f"zd07 profile trigger function drift: {function_name}",
        )
    for schema_name, table_name, constraint_name in (
        ("public", "member_subscriptions_v2", "fk_member_subscriptions_v2_branch_org"),
        ("finance", "invoices", "uq_finance_invoices_id_org"),
        ("finance", "refund_obligation_bindings", "fk_refund_obligation_bindings_invoice_org"),
        ("finance", "refund_obligation_bindings", "fk_refund_obligation_bindings_branch_org"),
        ("finance", "refund_obligation_bindings", "uq_refund_obligation_bindings_invoice"),
        ("finance", "refund_obligation_bindings", "chk_refund_obligation_bindings_source_table"),
        ("public", "members", "uq_members_id_org"),
        ("public", "membership_plans", "uq_membership_plans_id_org"),
        ("public", "member_subscriptions_v2", "uq_member_subscriptions_v2_id_org"),
        ("finance", "payments", "uq_finance_payments_id_org"),
        ("finance", "billing_parties", "chk_finance_billing_parties_buyer_kind"),
        ("finance", "billing_parties", "chk_finance_billing_parties_buyer_shape"),
        ("finance", "billing_parties", "fk_finance_billing_parties_member_org"),
        ("finance", "branch_accounting_profiles", "fk_branch_accounting_profiles_branch_org"),
        ("finance", "branch_accounting_profiles", "ex_branch_accounting_profiles_active_window"),
        ("finance", "membership_plan_tax_profiles", "fk_membership_plan_tax_profiles_plan_org"),
        ("finance", "membership_plan_tax_profiles", "ex_membership_plan_tax_profiles_active_window"),
        ("finance", "member_subscription_checkout_bindings", "fk_member_subscription_checkout_bindings_subscription_org"),
        ("finance", "member_subscription_checkout_bindings", "fk_member_subscription_checkout_bindings_invoice_org"),
        ("finance", "member_subscription_checkout_bindings", "fk_member_subscription_checkout_bindings_intent_org"),
        ("finance", "member_subscription_checkout_bindings", "uq_member_subscription_checkout_bindings_subscription"),
        ("finance", "member_subscription_checkout_bindings", "uq_member_subscription_checkout_bindings_invoice"),
        ("finance", "member_subscription_checkout_bindings", "uq_member_subscription_checkout_bindings_intent"),
        ("finance", "member_subscription_checkout_bindings", "chk_member_subscription_checkout_bindings_source_table"),
    ):
        _require_exact_constraint(bind, schema_name, table_name, constraint_name)
    source_check = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_constraintdef(con.oid) AS definition
            FROM pg_catalog.pg_constraint con
            JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname='finance'
              AND c.relname='refund_obligation_bindings'
              AND con.conname='chk_refund_obligation_bindings_source_table'
            """
        )
    ).scalar_one()
    if "source_table = 'member_subscriptions_v2'::text" not in source_check:
        raise RuntimeError("zd07 refund obligation source-table check drift")
    binding_row = bind.execute(
        sa.text(
            """
            SELECT pg_get_userbyid(c.relowner) AS owner_name,
                   c.relrowsecurity,
                   c.relforcerowsecurity,
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.aclexplode(coalesce(c.relacl, pg_catalog.acldefault('r', c.relowner))) acl
                       WHERE acl.grantee = 0
                   ) AS public_acl_exists
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname='finance'
              AND c.relname='refund_obligation_bindings'
              AND c.relkind='r'
            """
        )
    ).mappings().one_or_none()
    if (
        binding_row is None
        or binding_row["owner_name"] != _MIGRATION_OWNER
        or not binding_row["relrowsecurity"]
        or not binding_row["relforcerowsecurity"]
        or binding_row["public_acl_exists"]
    ):
        raise RuntimeError("zd07 refund obligation binding table owner/RLS/PUBLIC ACL drift")
    policy_rows = bind.execute(
        sa.text(
            """
            SELECT pol.polname,
                   CASE pol.polcmd
                       WHEN '*' THEN 'ALL'
                       WHEN 'r' THEN 'SELECT'
                       WHEN 'a' THEN 'INSERT'
                       WHEN 'w' THEN 'UPDATE'
                       WHEN 'd' THEN 'DELETE'
                       ELSE NULL
                   END AS command,
                   pg_catalog.pg_get_expr(pol.polqual, pol.polrelid) AS using_expr,
                   pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid) AS check_expr,
                   array_agg(role_data.rolname ORDER BY role_data.rolname) AS roles
            FROM pg_catalog.pg_policy pol
            JOIN pg_catalog.pg_class c ON c.oid = pol.polrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN LATERAL unnest(pol.polroles) AS role_oid(oid) ON true
            LEFT JOIN pg_catalog.pg_roles role_data ON role_data.oid = role_oid.oid
            WHERE n.nspname='finance'
              AND c.relname='refund_obligation_bindings'
            GROUP BY pol.polname, command, pol.polqual, pol.polwithcheck, pol.polrelid
            ORDER BY pol.polname
            """
        )
    ).mappings().all()
    expected_policy = [
        {
            "polname": "p4d_refund_obligation_security_owner_insert",
            "command": "INSERT",
            "using_expr": None,
            "check_expr": "true",
            "roles": [_SECURITY_OWNER],
        },
        {
            "polname": "p4d_refund_obligation_security_owner_select",
            "command": "SELECT",
            "using_expr": "true",
            "check_expr": None,
            "roles": [_SECURITY_OWNER],
        },
    ]
    observed_policy = [dict(row) for row in policy_rows]
    if observed_policy != expected_policy:
        raise RuntimeError(f"zd07 refund obligation binding policy drift: {observed_policy!r}")
    member_policy = bind.execute(
        sa.text(
            """
            SELECT pol.polname,
                   CASE pol.polcmd
                       WHEN '*' THEN 'ALL'
                       WHEN 'r' THEN 'SELECT'
                       WHEN 'a' THEN 'INSERT'
                       WHEN 'w' THEN 'UPDATE'
                       WHEN 'd' THEN 'DELETE'
                       ELSE NULL
                   END AS command,
                   pg_catalog.pg_get_expr(pol.polqual, pol.polrelid) AS using_expr,
                   pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid) AS check_expr,
                   array_agg(role_data.rolname ORDER BY role_data.rolname) AS roles
            FROM pg_catalog.pg_policy pol
            JOIN pg_catalog.pg_class c ON c.oid = pol.polrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN LATERAL unnest(pol.polroles) AS role_oid(oid) ON true
            LEFT JOIN pg_catalog.pg_roles role_data ON role_data.oid = role_oid.oid
            WHERE n.nspname='public'
              AND c.relname='member_subscriptions_v2'
              AND pol.polname='p4d_member_subscriptions_v2_security_owner_select'
            GROUP BY pol.polname, command, pol.polqual, pol.polwithcheck, pol.polrelid
            """
        )
    ).mappings().all()
    if [dict(row) for row in member_policy] != [{
        "polname": "p4d_member_subscriptions_v2_security_owner_select",
        "command": "SELECT",
        "using_expr": "true",
        "check_expr": None,
        "roles": [_SECURITY_OWNER],
    }]:
        raise RuntimeError("zd07 member subscription security-owner policy drift")
    membership_plan_policy = bind.execute(
        sa.text(
            """
            SELECT pol.polname,
                   CASE pol.polcmd
                       WHEN '*' THEN 'ALL'
                       WHEN 'r' THEN 'SELECT'
                       WHEN 'a' THEN 'INSERT'
                       WHEN 'w' THEN 'UPDATE'
                       WHEN 'd' THEN 'DELETE'
                       ELSE NULL
                   END AS command,
                   pg_catalog.pg_get_expr(pol.polqual, pol.polrelid) AS using_expr,
                   pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid) AS check_expr,
                   array_agg(role_data.rolname ORDER BY role_data.rolname) AS roles
            FROM pg_catalog.pg_policy pol
            JOIN pg_catalog.pg_class c ON c.oid = pol.polrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN LATERAL unnest(pol.polroles) AS role_oid(oid) ON true
            LEFT JOIN pg_catalog.pg_roles role_data ON role_data.oid = role_oid.oid
            WHERE n.nspname='public'
              AND c.relname='membership_plans'
              AND pol.polname='p4d_membership_plans_security_owner_select'
            GROUP BY pol.polname, command, pol.polqual, pol.polwithcheck, pol.polrelid
            """
        )
    ).mappings().all()
    if [dict(row) for row in membership_plan_policy] != [{
        "polname": "p4d_membership_plans_security_owner_select",
        "command": "SELECT",
        "using_expr": "true",
        "check_expr": None,
        "roles": [_SECURITY_OWNER],
    }]:
        raise RuntimeError("zd07 membership plan security-owner policy drift")
    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege('app_security_owner','finance.refund_obligation_bindings','SELECT') "
            "AND pg_catalog.has_table_privilege('app_security_owner','finance.refund_obligation_bindings','INSERT') "
            "AND NOT pg_catalog.has_table_privilege('app_security_owner','finance.refund_obligation_bindings','UPDATE') "
            "AND NOT pg_catalog.has_table_privilege('app_security_owner','finance.refund_obligation_bindings','DELETE')"
        )
    ).scalar_one():
        raise RuntimeError("zd07 app_security_owner binding DML ACL drift")
    for relation in ("finance.payment_allocations", "finance.invoices", "public.member_subscriptions_v2"):
        if not bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege('app_security_owner', :relation, 'SELECT') "
                "AND NOT pg_catalog.has_table_privilege('app_security_owner', :relation, 'INSERT') "
                "AND NOT pg_catalog.has_table_privilege('app_security_owner', :relation, 'UPDATE') "
                "AND NOT pg_catalog.has_table_privilege('app_security_owner', :relation, 'DELETE')"
            ),
            {"relation": relation},
        ).scalar_one():
            raise RuntimeError(f"zd07 app_security_owner read-only ACL drift on {relation}")
    for relation, privilege in (
        ("finance.payments", "SELECT"),
        ("finance.payments", "UPDATE"),
        ("finance.refunds", "SELECT"),
        ("finance.refunds", "UPDATE"),
        ("public.branch_outbox_events", "SELECT"),
    ):
        _require_predecessor_present_acl(bind, "app_security_owner", relation, privilege)
    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege('app_security_owner','finance.refunds','INSERT') "
            "AND NOT pg_catalog.has_table_privilege('app_security_owner','finance.refunds','DELETE')"
        )
    ).scalar_one():
        raise RuntimeError("zd07 app_security_owner refund insert ACL drift")
    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.has_column_privilege('app_security_owner','public.organizations','id','SELECT') "
            "AND pg_catalog.has_column_privilege('app_security_owner','public.organizations','is_active','SELECT') "
            "AND pg_catalog.has_column_privilege('app_security_owner','public.organizations','name','SELECT') "
            "AND pg_catalog.has_column_privilege('app_security_owner','public.organizations','slug','SELECT') "
            "AND pg_catalog.has_column_privilege('app_security_owner','public.organizations','business_type','SELECT') "
            "AND pg_catalog.has_column_privilege('app_security_owner','public.organizations','description','SELECT') "
            "AND NOT pg_catalog.has_table_privilege('app_runtime','public.organizations','SELECT') "
            "AND NOT pg_catalog.has_table_privilege('worker_runtime','public.organizations','SELECT') "
            "AND NOT pg_catalog.has_table_privilege('finance_config_runtime','public.organizations','SELECT')"
        )
    ).scalar_one():
        raise RuntimeError("zd07 finance invoice organization ACL drift")
    if not bind.execute(
        sa.text(
            "SELECT pg_catalog.has_table_privilege('app_security_owner','finance.tax_codes','SELECT') "
            "AND pg_catalog.has_table_privilege('app_security_owner','finance.tax_codes','INSERT') "
            "AND NOT pg_catalog.has_table_privilege('app_security_owner','finance.tax_codes','UPDATE') "
            "AND NOT pg_catalog.has_table_privilege('app_security_owner','finance.tax_codes','DELETE') "
            "AND NOT pg_catalog.has_table_privilege('app_security_owner','finance.tax_codes','TRUNCATE') "
            "AND NOT pg_catalog.has_table_privilege('app_security_owner','finance.tax_codes','REFERENCES') "
            "AND NOT pg_catalog.has_table_privilege('app_security_owner','finance.tax_codes','TRIGGER')"
        )
    ).scalar_one():
        raise RuntimeError("zd07 app_security_owner tax code ACL drift")
    tax_code_policy_rows = bind.execute(
        sa.text(
            """
            SELECT pol.polname,
                   CASE pol.polcmd
                       WHEN '*' THEN 'ALL'
                       WHEN 'r' THEN 'SELECT'
                       WHEN 'a' THEN 'INSERT'
                       WHEN 'w' THEN 'UPDATE'
                       WHEN 'd' THEN 'DELETE'
                       ELSE NULL
                   END AS command,
                   pg_catalog.pg_get_expr(pol.polqual, pol.polrelid) AS using_expr,
                   pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid) AS check_expr,
                   array_agg(role_data.rolname ORDER BY role_data.rolname) AS roles
            FROM pg_catalog.pg_policy pol
            JOIN pg_catalog.pg_class c ON c.oid = pol.polrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN LATERAL unnest(pol.polroles) AS role_oid(oid) ON true
            LEFT JOIN pg_catalog.pg_roles role_data ON role_data.oid = role_oid.oid
            WHERE n.nspname='finance'
              AND c.relname='tax_codes'
            GROUP BY pol.polname, command, pol.polqual, pol.polwithcheck, pol.polrelid
            ORDER BY pol.polname
            """
        )
    ).mappings().all()
    expected_tax_code_policy = [
        {
            "polname": "p4d_tax_codes_security_owner_insert",
            "command": "INSERT",
            "using_expr": None,
            "check_expr": "((status = 'active'::text) AND ((code)::text ~ '^[A-Z0-9_]+$'::text) AND (description IS NOT NULL) AND (btrim(description) <> ''::text) AND (tax_type = ANY (ARRAY['gst'::text, 'exempt'::text, 'non_gst'::text])) AND (gst_rate_basis_points >= 0))",
            "roles": [_SECURITY_OWNER],
        },
        {
            "polname": "p4d_tax_codes_security_owner_select",
            "command": "SELECT",
            "using_expr": "true",
            "check_expr": None,
            "roles": [_SECURITY_OWNER],
        },
    ]
    observed_tax_code_policy = [dict(row) for row in tax_code_policy_rows]
    if observed_tax_code_policy != expected_tax_code_policy:
        raise RuntimeError(f"zd07 finance tax code policy drift: {observed_tax_code_policy!r}")
    for relation in (
        "finance.tax_codes",
        "finance.branch_accounting_profiles",
        "finance.membership_plan_tax_profiles",
        "finance.member_subscription_checkout_bindings",
    ):
        row = bind.execute(
            sa.text(
                """
                SELECT pg_get_userbyid(c.relowner) AS owner_name,
                       c.relrowsecurity,
                       c.relforcerowsecurity,
                       EXISTS (
                           SELECT 1
                           FROM pg_catalog.aclexplode(coalesce(c.relacl, pg_catalog.acldefault('r', c.relowner))) acl
                           WHERE acl.grantee = 0
                       ) AS public_acl_exists
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = split_part(:relation, '.', 1)
                  AND c.relname = split_part(:relation, '.', 2)
                  AND c.relkind = 'r'
                """
            ),
            {"relation": relation},
        ).mappings().one_or_none()
        if row is None or row["owner_name"] != _MIGRATION_OWNER or not row["relrowsecurity"] or not row["relforcerowsecurity"] or row["public_acl_exists"]:
            raise RuntimeError(f"zd07 P4D-2 table owner/RLS/PUBLIC ACL drift on {relation}")
    for role_name in ("app_runtime", "worker_runtime", "lifecycle_maintenance_runtime", "finance_config_runtime"):
        if bind.execute(
            sa.text(
                "SELECT pg_catalog.has_table_privilege(:role,'finance.refunds','INSERT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.payment_allocations','SELECT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.invoices','SELECT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.refund_obligation_bindings','SELECT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.refund_obligation_bindings','INSERT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.member_subscription_checkout_bindings','SELECT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.member_subscription_checkout_bindings','INSERT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.branch_accounting_profiles','SELECT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.branch_accounting_profiles','INSERT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.branch_accounting_profiles','UPDATE') "
                "OR pg_catalog.has_table_privilege(:role,'finance.branch_accounting_profiles','DELETE') "
                "OR pg_catalog.has_table_privilege(:role,'finance.membership_plan_tax_profiles','SELECT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.membership_plan_tax_profiles','INSERT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.membership_plan_tax_profiles','UPDATE') "
                "OR pg_catalog.has_table_privilege(:role,'finance.membership_plan_tax_profiles','DELETE') "
                "OR pg_catalog.has_table_privilege(:role,'finance.tax_codes','SELECT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.tax_codes','INSERT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.tax_codes','UPDATE') "
                "OR pg_catalog.has_table_privilege(:role,'finance.tax_codes','DELETE') "
                "OR pg_catalog.has_table_privilege(:role,'public.organizations','SELECT') "
                "OR pg_catalog.has_table_privilege(:role,'finance.payments','UPDATE')"
            ),
            {"role": role_name},
        ).scalar_one():
            raise RuntimeError(f"zd07 direct Finance table privilege leaked to {role_name}")

_P4D2_INVOICE_ISSUE_ACL_STATE = (
    "app_private.p4d2_invoice_issue_acl_delta"
)

_P4D2_INVOICE_ISSUE_COLUMN_PRIVILEGES = (
    (
        "invoices",
        "SELECT",
        (
            "billing_party_id",
            "legal_entity_id",
            "gst_registration_id",
            "division_id",
            "brand_id",
            "financial_year",
            "invoice_series_id",
            "brand_ref_series_id",
            "issued_at",
            "metadata_json",
            "taxable_amount",
            "total_tax_amount",
            "grand_total_amount",
        ),
    ),
    (
        "invoices",
        "UPDATE",
        (
            "invoice_series_id",
            "brand_ref_series_id",
            "official_invoice_number",
            "brand_reference",
            "status",
            "issued_at",
            "updated_at",
        ),
    ),
    (
        "invoice_lines",
        "SELECT",
        (
            "id",
            "invoice_id",
            "line_number",
            "taxable_amount",
            "gst_rate_basis_points",
            "cgst_amount",
            "sgst_amount",
            "igst_amount",
            "total_tax_amount",
            "line_total_amount",
        ),
    ),
    (
        "invoice_series",
        "SELECT",
        (
            "id",
            "legal_entity_id",
            "gst_registration_id",
            "division_id",
            "financial_year",
            "series_code",
            "last_number",
        ),
    ),
    (
        "invoice_series",
        "UPDATE",
        (
            "last_number",
            "updated_at",
        ),
    ),
    (
        "brand_ref_series",
        "SELECT",
        (
            "id",
            "legal_entity_id",
            "division_id",
            "brand_id",
            "financial_year",
            "series_code",
            "last_number",
        ),
    ),
    (
        "brand_ref_series",
        "UPDATE",
        (
            "last_number",
            "updated_at",
        ),
    ),
    (
        "tax_records",
        "SELECT",
        (
            "invoice_id",
        ),
    ),
    (
        "tax_records",
        "INSERT",
        (
            "invoice_id",
            "invoice_line_id",
            "tax_component",
            "taxable_amount",
            "tax_rate_basis_points",
            "tax_amount",
        ),
    ),
    (
        "outbox_events",
        "SELECT",
        (
            "aggregate_type",
            "aggregate_id",
            "event_type",
        ),
    ),
    (
        "outbox_events",
        "INSERT",
        (
            "organization_id",
            "legal_entity_id",
            "division_id",
            "brand_id",
            "aggregate_type",
            "aggregate_id",
            "event_type",
            "idempotency_key",
            "payload_json",
            "payload_sha256",
            "status",
        ),
    ),
)


def _p4d2_grant_invoice_issue_column_if_missing(
    bind,
    *,
    table_name: str,
    column_name: str,
    privilege_name: str,
) -> None:
    qualified = f"finance.{table_name}"

    already_present = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_column_privilege(
                'app_security_owner',
                :qualified,
                :column_name,
                :privilege_name
            )
            """
        ),
        {
            "qualified": qualified,
            "column_name": column_name,
            "privilege_name": privilege_name,
        },
    ).scalar_one()

    if already_present:
        return

    allowed_identifier = (
        table_name.replace("_", "").isalnum()
        and column_name.replace("_", "").isalnum()
        and privilege_name in {"SELECT", "INSERT", "UPDATE"}
    )

    if not allowed_identifier:
        raise RuntimeError(
            "Unsafe P4D-2 invoice-issue ACL identifier"
        )

    bind.execute(
        sa.text(
            f"GRANT {privilege_name} ({column_name}) "
            f"ON TABLE finance.{table_name} "
            "TO app_security_owner"
        )
    )

    bind.execute(
        sa.text(
            f"""
            INSERT INTO {_P4D2_INVOICE_ISSUE_ACL_STATE} (
                table_name,
                column_name,
                privilege_name
            ) VALUES (
                :table_name,
                :column_name,
                :privilege_name
            )
            """
        ),
        {
            "table_name": table_name,
            "column_name": column_name,
            "privilege_name": privilege_name,
        },
    )


def _p4d2_install_invoice_issue_authority(bind) -> None:
    _require_identity_contract(bind)

    if bind.execute(
        sa.text(
            """
            SELECT pg_catalog.to_regnamespace('app_private')
                   IS NOT NULL
            """
        )
    ).scalar_one() is not True:
        raise RuntimeError(
            "P4D-2 invoice issue authority requires app_private"
        )

    bind.execute(
        sa.text(
            f"""
            CREATE TABLE {_P4D2_INVOICE_ISSUE_ACL_STATE} (
                table_name TEXT NOT NULL,
                column_name TEXT NOT NULL,
                privilege_name TEXT NOT NULL,
                PRIMARY KEY (
                    table_name,
                    column_name,
                    privilege_name
                ),
                CONSTRAINT
                    chk_p4d2_invoice_issue_acl_privilege
                CHECK (
                    privilege_name IN (
                        'SELECT',
                        'INSERT',
                        'UPDATE'
                    )
                )
            )
            """
        )
    )

    for (
        table_name,
        privilege_name,
        columns,
    ) in _P4D2_INVOICE_ISSUE_COLUMN_PRIVILEGES:
        for column_name in columns:
            _p4d2_grant_invoice_issue_column_if_missing(
                bind,
                table_name=table_name,
                column_name=column_name,
                privilege_name=privilege_name,
            )

    # P4D2_INVOICE_ISSUE_OWNER_CONTEXT_V3
    #
    # app_secure is intentionally not writable by migration_owner.
    # Follow the existing zd07 contract: enter the bounded security-owner
    # context only for app_secure function DDL/ACL.
    op.execute("SET LOCAL ROLE app_security_owner")

    bind.execute(
        sa.text(
            r"""
            CREATE OR REPLACE FUNCTION
                app_secure.resolve_finance_invoice_issue_context(
                    p_invoice_id uuid
                )
            RETURNS TABLE (
                invoice_id uuid,
                organization_id uuid
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_current_org_id uuid;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'app_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = '42501',
                            MESSAGE =
                                'P4D finance invoice issue context '
                                'requires app_runtime authority';
                END IF;

                v_current_org_id :=
                    NULLIF(
                        pg_catalog.current_setting(
                            'app.current_org_id',
                            true
                        ),
                        ''
                    )::uuid;

                IF v_current_org_id IS NULL THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = '42501',
                            MESSAGE =
                                'P4D finance invoice issue context '
                                'requires current tenant';
                END IF;

                RETURN QUERY
                SELECT
                    i.id,
                    i.organization_id
                FROM finance.invoices AS i
                WHERE i.id = p_invoice_id
                  AND i.organization_id = v_current_org_id;
            END;
            $function$
            """
        )
    )

    bind.execute(
        sa.text(
            """
            REVOKE ALL ON FUNCTION
                app_secure.resolve_finance_invoice_issue_context(uuid)
            FROM PUBLIC
            """
        )
    )

    bind.execute(
        sa.text(
            """
            GRANT EXECUTE ON FUNCTION
                app_secure.resolve_finance_invoice_issue_context(uuid)
            TO app_runtime
            """
        )
    )

    bind.execute(
        sa.text(
            """
            ALTER FUNCTION
                app_secure.resolve_finance_invoice_issue_context(uuid)
            OWNER TO app_security_owner
            """
        )
    )

    bind.execute(
        sa.text(
            r"""
            CREATE OR REPLACE FUNCTION
                app_secure.issue_finance_invoice(
                    p_invoice_id uuid,
                    p_idempotency_key text
                )
            RETURNS TABLE (
                invoice_id uuid,
                invoice_status text,
                official_invoice_number text,
                brand_reference text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            DECLARE
                v_current_org_id uuid;
                v_invoice record;

                v_org_active boolean;

                v_supply_date_text text;
                v_supply_date date;

                v_division_code text;
                v_brand_code text;

                v_invoice_series record;
                v_brand_series record;

                v_next_invoice_number bigint;
                v_next_brand_number bigint;

                v_official_invoice_number text;
                v_brand_reference text;

                v_line_count bigint;
                v_line_taxable numeric;
                v_line_tax numeric;
                v_line_total numeric;
                v_invalid_tax_line_count bigint;

                v_payload jsonb;
                v_payload_canonical text;
                v_payload_sha256 text;

                v_updated_rows bigint;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'app_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = '42501',
                            MESSAGE =
                                'P4D finance invoice issue '
                                'requires app_runtime authority';
                END IF;

                IF p_invoice_id IS NULL THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'invoice id is required';
                END IF;

                IF p_idempotency_key IS NULL
                   OR pg_catalog.btrim(p_idempotency_key) = ''
                THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'idempotency key is required';
                END IF;

                v_current_org_id :=
                    NULLIF(
                        pg_catalog.current_setting(
                            'app.current_org_id',
                            true
                        ),
                        ''
                    )::uuid;

                IF v_current_org_id IS NULL THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = '42501',
                            MESSAGE =
                                'P4D finance invoice issue '
                                'requires current tenant';
                END IF;

                SELECT
                    i.id,
                    i.organization_id,
                    i.billing_party_id,
                    i.legal_entity_id,
                    i.gst_registration_id,
                    i.division_id,
                    i.brand_id,
                    i.financial_year,
                    i.invoice_series_id,
                    i.brand_ref_series_id,
                    i.official_invoice_number,
                    i.brand_reference,
                    i.status,
                    i.issued_at,
                    i.metadata_json,
                    i.taxable_amount,
                    i.total_tax_amount,
                    i.grand_total_amount
                INTO v_invoice
                FROM finance.invoices AS i
                WHERE i.id = p_invoice_id
                FOR UPDATE;

                IF NOT FOUND
                   OR v_invoice.organization_id
                      IS DISTINCT FROM v_current_org_id
                THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D21',
                            MESSAGE =
                                'P4D finance invoice was not found';
                END IF;

                IF v_invoice.status <> 'draft' THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D22',
                            MESSAGE =
                                'P4D finance invoice state conflict: '
                                'only draft invoices can be issued';
                END IF;

                IF v_invoice.invoice_series_id IS NOT NULL
                   OR v_invoice.brand_ref_series_id IS NOT NULL
                   OR v_invoice.official_invoice_number IS NOT NULL
                   OR v_invoice.brand_reference IS NOT NULL
                   OR v_invoice.issued_at IS NOT NULL
                THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'draft contains issuance metadata';
                END IF;

                SELECT r.is_active
                INTO v_org_active
                FROM app_secure.resolve_finance_invoice_organization(
                    v_invoice.organization_id
                ) AS r;

                IF NOT FOUND
                   OR v_org_active IS DISTINCT FROM TRUE
                THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'organization is inactive or unavailable';
                END IF;

                v_supply_date_text :=
                    v_invoice.metadata_json ->> 'supply_date';

                IF v_supply_date_text IS NULL
                   OR v_supply_date_text !~
                      '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'supply date is required';
                END IF;

                BEGIN
                    v_supply_date := v_supply_date_text::date;
                EXCEPTION
                    WHEN invalid_datetime_format
                      OR datetime_field_overflow
                    THEN
                        RAISE EXCEPTION
                            USING
                                ERRCODE = 'P4D23',
                                MESSAGE =
                                    'P4D finance invoice validation '
                                    'failed: supply date is invalid';
                END;

                IF pg_catalog.to_char(
                    v_supply_date,
                    'YYYY-MM-DD'
                ) <> v_supply_date_text
                THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'supply date is invalid';
                END IF;

                SELECT
                    r.division_code,
                    r.brand_code
                INTO
                    v_division_code,
                    v_brand_code
                FROM
                    app_secure
                    .resolve_finance_invoice_accounting_master_data(
                        v_invoice.organization_id,
                        v_invoice.legal_entity_id,
                        v_invoice.gst_registration_id,
                        v_invoice.division_id,
                        v_invoice.brand_id,
                        v_invoice.billing_party_id,
                        v_supply_date
                    ) AS r;

                IF NOT FOUND
                   OR v_division_code IS NULL
                   OR pg_catalog.btrim(v_division_code) = ''
                   OR v_brand_code IS NULL
                   OR pg_catalog.btrim(v_brand_code) = ''
                THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'accounting master data is incomplete';
                END IF;

                SELECT
                    pg_catalog.count(*),
                    COALESCE(
                        pg_catalog.sum(l.taxable_amount),
                        0
                    ),
                    COALESCE(
                        pg_catalog.sum(l.total_tax_amount),
                        0
                    ),
                    COALESCE(
                        pg_catalog.sum(l.line_total_amount),
                        0
                    ),
                    pg_catalog.count(*) FILTER (
                        WHERE
                            l.cgst_amount
                            + l.sgst_amount
                            + l.igst_amount
                            <> l.total_tax_amount
                    )
                INTO
                    v_line_count,
                    v_line_taxable,
                    v_line_tax,
                    v_line_total,
                    v_invalid_tax_line_count
                FROM finance.invoice_lines AS l
                WHERE l.invoice_id = p_invoice_id;

                IF v_line_count = 0 THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'invoice has no lines';
                END IF;

                IF v_invalid_tax_line_count <> 0
                   OR v_line_taxable
                      <> v_invoice.taxable_amount
                   OR v_line_tax
                      <> v_invoice.total_tax_amount
                   OR v_line_total
                      <> v_invoice.grand_total_amount
                THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'persisted line totals do not reconcile';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM finance.tax_records AS tr
                    WHERE tr.invoice_id = p_invoice_id
                ) THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'draft already has tax evidence';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM finance.outbox_events AS oe
                    WHERE oe.aggregate_type = 'invoice'
                      AND oe.aggregate_id = p_invoice_id
                      AND oe.event_type =
                          'finance.invoice.issued'
                ) THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'draft already has issued outbox evidence';
                END IF;

                SELECT
                    s.id,
                    s.series_code,
                    s.last_number
                INTO v_invoice_series
                FROM finance.invoice_series AS s
                WHERE
                    s.legal_entity_id =
                        v_invoice.legal_entity_id
                    AND s.gst_registration_id =
                        v_invoice.gst_registration_id
                    AND s.division_id =
                        v_invoice.division_id
                    AND s.financial_year =
                        v_invoice.financial_year
                    AND s.series_code =
                        v_division_code
                FOR UPDATE;

                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'invoice number series is unavailable';
                END IF;

                v_next_invoice_number :=
                    v_invoice_series.last_number + 1;

                UPDATE finance.invoice_series
                SET
                    last_number = v_next_invoice_number,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE id = v_invoice_series.id;

                v_official_invoice_number :=
                    v_invoice_series.series_code
                    || '/'
                    || pg_catalog.btrim(
                        v_invoice.financial_year
                    )
                    || '/'
                    || pg_catalog.lpad(
                        v_next_invoice_number::text,
                        GREATEST(
                            5,
                            pg_catalog.length(
                                v_next_invoice_number::text
                            )
                        ),
                        '0'
                    );

                IF pg_catalog.length(
                    v_official_invoice_number
                ) > 40 THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'official number exceeds schema limit';
                END IF;

                SELECT
                    s.id,
                    s.series_code,
                    s.last_number
                INTO v_brand_series
                FROM finance.brand_ref_series AS s
                WHERE
                    s.legal_entity_id =
                        v_invoice.legal_entity_id
                    AND s.division_id =
                        v_invoice.division_id
                    AND s.brand_id =
                        v_invoice.brand_id
                    AND s.financial_year =
                        v_invoice.financial_year
                    AND s.series_code =
                        v_brand_code
                FOR UPDATE;

                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'brand reference series is unavailable';
                END IF;

                v_next_brand_number :=
                    v_brand_series.last_number + 1;

                UPDATE finance.brand_ref_series
                SET
                    last_number = v_next_brand_number,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE id = v_brand_series.id;

                v_brand_reference :=
                    v_brand_series.series_code
                    || '/'
                    || pg_catalog.btrim(
                        v_invoice.financial_year
                    )
                    || '/'
                    || pg_catalog.lpad(
                        v_next_brand_number::text,
                        GREATEST(
                            5,
                            pg_catalog.length(
                                v_next_brand_number::text
                            )
                        ),
                        '0'
                    );

                IF pg_catalog.length(v_brand_reference) > 40 THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D23',
                            MESSAGE =
                                'P4D finance invoice validation failed: '
                                'brand reference exceeds schema limit';
                END IF;

                INSERT INTO finance.tax_records (
                    invoice_id,
                    invoice_line_id,
                    tax_component,
                    taxable_amount,
                    tax_rate_basis_points,
                    tax_amount
                )
                SELECT
                    p_invoice_id,
                    l.id,
                    'cgst',
                    l.taxable_amount,
                    l.gst_rate_basis_points / 2,
                    l.cgst_amount
                FROM finance.invoice_lines AS l
                WHERE l.invoice_id = p_invoice_id
                  AND l.cgst_amount > 0;

                INSERT INTO finance.tax_records (
                    invoice_id,
                    invoice_line_id,
                    tax_component,
                    taxable_amount,
                    tax_rate_basis_points,
                    tax_amount
                )
                SELECT
                    p_invoice_id,
                    l.id,
                    'sgst',
                    l.taxable_amount,
                    l.gst_rate_basis_points / 2,
                    l.sgst_amount
                FROM finance.invoice_lines AS l
                WHERE l.invoice_id = p_invoice_id
                  AND l.sgst_amount > 0;

                INSERT INTO finance.tax_records (
                    invoice_id,
                    invoice_line_id,
                    tax_component,
                    taxable_amount,
                    tax_rate_basis_points,
                    tax_amount
                )
                SELECT
                    p_invoice_id,
                    l.id,
                    'igst',
                    l.taxable_amount,
                    l.gst_rate_basis_points,
                    l.igst_amount
                FROM finance.invoice_lines AS l
                WHERE l.invoice_id = p_invoice_id
                  AND l.igst_amount > 0;

                UPDATE finance.invoices
                SET
                    invoice_series_id =
                        v_invoice_series.id,
                    brand_ref_series_id =
                        v_brand_series.id,
                    official_invoice_number =
                        v_official_invoice_number,
                    brand_reference =
                        v_brand_reference,
                    status = 'issued',
                    issued_at =
                        pg_catalog.clock_timestamp(),
                    updated_at =
                        pg_catalog.clock_timestamp()
                WHERE id = p_invoice_id
                  AND organization_id = v_current_org_id
                  AND status = 'draft';

                GET DIAGNOSTICS
                    v_updated_rows = ROW_COUNT;

                IF v_updated_rows <> 1 THEN
                    RAISE EXCEPTION
                        USING
                            ERRCODE = 'P4D22',
                            MESSAGE =
                                'P4D finance invoice state conflict '
                                'during finalization';
                END IF;

                v_payload :=
                    pg_catalog.jsonb_build_object(
                        'invoice_id',
                        p_invoice_id::text,
                        'official_invoice_number',
                        v_official_invoice_number,
                        'brand_reference',
                        v_brand_reference,
                        'status',
                        'issued'
                    );

                /*
                 * Match Python canonical_hash():
                 * json.dumps(sort_keys=True,separators=(",",":"))
                 *
                 * Alphabetical key order:
                 * brand_reference
                 * invoice_id
                 * official_invoice_number
                 * status
                 */
                v_payload_canonical :=
                    '{"brand_reference":'
                    || pg_catalog.to_jsonb(
                        v_brand_reference
                    )::text
                    || ',"invoice_id":'
                    || pg_catalog.to_jsonb(
                        p_invoice_id::text
                    )::text
                    || ',"official_invoice_number":'
                    || pg_catalog.to_jsonb(
                        v_official_invoice_number
                    )::text
                    || ',"status":"issued"}';

                v_payload_sha256 :=
                    pg_catalog.encode(
                        pg_catalog.sha256(
                            pg_catalog.convert_to(
                                v_payload_canonical,
                                'UTF8'
                            )
                        ),
                        'hex'
                    );

                INSERT INTO finance.outbox_events (
                    organization_id,
                    legal_entity_id,
                    division_id,
                    brand_id,
                    aggregate_type,
                    aggregate_id,
                    event_type,
                    idempotency_key,
                    payload_json,
                    payload_sha256,
                    status
                ) VALUES (
                    v_invoice.organization_id,
                    v_invoice.legal_entity_id,
                    v_invoice.division_id,
                    v_invoice.brand_id,
                    'invoice',
                    p_invoice_id,
                    'finance.invoice.issued',
                    p_idempotency_key,
                    v_payload,
                    v_payload_sha256,
                    'pending'
                );

                RETURN QUERY
                SELECT
                    p_invoice_id,
                    'issued'::text,
                    v_official_invoice_number,
                    v_brand_reference;
            END;
            $function$
            """
        )
    )

    bind.execute(
        sa.text(
            """
            REVOKE ALL ON FUNCTION
                app_secure.issue_finance_invoice(uuid,text)
            FROM PUBLIC
            """
        )
    )

    bind.execute(
        sa.text(
            """
            GRANT EXECUTE ON FUNCTION
                app_secure.issue_finance_invoice(uuid,text)
            TO app_runtime
            """
        )
    )

    bind.execute(
        sa.text(
            """
            ALTER FUNCTION
                app_secure.issue_finance_invoice(uuid,text)
            OWNER TO app_security_owner
            """
        )
    )

    op.execute("RESET ROLE")
    _require_identity_contract(bind)


def _p4d2_remove_invoice_issue_authority(bind) -> None:
    _require_identity_contract(bind)

    op.execute("SET LOCAL ROLE app_security_owner")

    bind.execute(
        sa.text(
            """
            DROP FUNCTION IF EXISTS
                app_secure.issue_finance_invoice(uuid,text)
            """
        )
    )

    bind.execute(
        sa.text(
            """
            DROP FUNCTION IF EXISTS
                app_secure.resolve_finance_invoice_issue_context(uuid)
            """
        )
    )

    op.execute("RESET ROLE")
    _require_identity_contract(bind)

    state_exists = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.to_regclass(
                'app_private.p4d2_invoice_issue_acl_delta'
            ) IS NOT NULL
            """
        )
    ).scalar_one()

    if not state_exists:
        return

    rows = bind.execute(
        sa.text(
            """
            SELECT
                table_name,
                column_name,
                privilege_name
            FROM app_private.p4d2_invoice_issue_acl_delta
            ORDER BY
                table_name DESC,
                privilege_name DESC,
                column_name DESC
            """
        )
    ).mappings().all()

    for row in rows:
        table_name = row["table_name"]
        column_name = row["column_name"]
        privilege_name = row["privilege_name"]

        allowed_identifier = (
            table_name.replace("_", "").isalnum()
            and column_name.replace("_", "").isalnum()
            and privilege_name
                in {"SELECT", "INSERT", "UPDATE"}
        )

        if not allowed_identifier:
            raise RuntimeError(
                "Unsafe stored P4D-2 invoice-issue ACL identifier"
            )

        bind.execute(
            sa.text(
                f"REVOKE {privilege_name} ({column_name}) "
                f"ON TABLE finance.{table_name} "
                "FROM app_security_owner"
            )
        )

    bind.execute(
        sa.text(
            """
            DROP TABLE
                app_private.p4d2_invoice_issue_acl_delta
            """
        )
    )


_P4D2_CHECKOUT_CALLBACK_ACL_STATE = (
    "app_private.p4d2_checkout_callback_acl_delta"
)

_P4D2_CHECKOUT_CALLBACK_COLUMN_PRIVILEGES = (
    (
        "payment_events",
        "SELECT",
        (
            "id",
            "payment_id",
            "provider_code",
            "provider_event_id",
            "event_type",
            "event_payload_sha256",
            "received_at",
        ),
    ),
    (
        "payment_events",
        "INSERT",
        (
            "payment_id",
            "provider_code",
            "provider_event_id",
            "event_type",
            "event_payload_sha256",
        ),
    ),
)


def _p4d2_require_checkout_callback_authority(bind) -> None:
    _require_identity_contract(bind)

    _require_function_contract(
        bind,
        function_name="record_finance_checkout_callback",
        normalized_args="text,text,text,text,text",
        allowed_role="app_runtime",
        blocked_roles=(
            "auth_runtime",
            "worker_runtime",
            "lifecycle_maintenance_runtime",
            "finance_config_runtime",
        ),
        error_label="finance checkout callback capability",
    )

    constraint = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_constraintdef(c.oid, true)
            FROM pg_catalog.pg_constraint c
            JOIN pg_catalog.pg_class t
              ON t.oid = c.conrelid
            JOIN pg_catalog.pg_namespace n
              ON n.oid = t.relnamespace
            WHERE n.nspname = 'finance'
              AND t.relname = 'payments'
              AND c.conname = 'uq_finance_payments_provider_order_ref'
            """
        )
    ).scalar_one_or_none()

    if constraint != "UNIQUE (provider_code, provider_order_ref)":
        raise RuntimeError(
            "zd07 checkout callback provider-order uniqueness drift"
        )

    for table_name, privilege_name, columns in (
        _P4D2_CHECKOUT_CALLBACK_COLUMN_PRIVILEGES
    ):
        for column_name in columns:
            if not bind.execute(
                sa.text(
                    """
                    SELECT pg_catalog.has_column_privilege(
                        'app_security_owner',
                        :relation,
                        :column_name,
                        :privilege_name
                    )
                    """
                ),
                {
                    "relation": f"finance.{table_name}",
                    "column_name": column_name,
                    "privilege_name": privilege_name,
                },
            ).scalar_one():
                raise RuntimeError(
                    "zd07 checkout callback owner privilege missing: "
                    f"{privilege_name} "
                    f"finance.{table_name}.{column_name}"
                )

    if bind.execute(
        sa.text(
            """
            SELECT
                   pg_catalog.has_table_privilege(
                       'app_runtime',
                       'finance.payments',
                       'SELECT'
                   )
                OR pg_catalog.has_table_privilege(
                       'app_runtime',
                       'finance.payments',
                       'UPDATE'
                   )
                OR pg_catalog.has_table_privilege(
                       'app_runtime',
                       'finance.payment_events',
                       'SELECT'
                   )
                OR pg_catalog.has_table_privilege(
                       'app_runtime',
                       'finance.payment_events',
                       'INSERT'
                   )
                OR pg_catalog.has_table_privilege(
                       'app_runtime',
                       'finance.outbox_events',
                       'INSERT'
                   )
                OR pg_catalog.has_table_privilege(
                       'app_runtime',
                       'finance.idempotency_keys',
                       'SELECT'
                   )
                OR pg_catalog.has_table_privilege(
                       'app_runtime',
                       'finance.idempotency_keys',
                       'INSERT'
                   )
            """
        )
    ).scalar_one():
        raise RuntimeError(
            "zd07 checkout callback leaked direct Finance authority "
            "to app_runtime"
        )

    state_count = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.count(*)
            FROM app_private.p4d2_checkout_callback_acl_delta
            """
        )
    ).scalar_one()

    if state_count != 12:
        raise RuntimeError(
            "zd07 checkout callback ACL delta is not exact"
        )


def _p4d2_install_checkout_callback_authority(bind) -> None:
    _require_identity_contract(bind)

    _require_exact_function_count(
        bind,
        schema_name="app_secure",
        function_name="record_finance_checkout_callback",
        normalized_args="text,text,text,text,text",
        expected_count=0,
        error_label=(
            "zd07 checkout callback capability already exists"
        ),
    )

    if _constraint_exists(
        bind,
        "finance",
        "payments",
        "uq_finance_payments_provider_order_ref",
    ):
        raise RuntimeError(
            "zd07 refuses to claim predecessor provider-order "
            "uniqueness constraint"
        )

    duplicate = bind.execute(
        sa.text(
            """
            SELECT
                p.provider_code,
                p.provider_order_ref,
                pg_catalog.count(*) AS match_count
            FROM finance.payments p
            WHERE p.provider_order_ref IS NOT NULL
            GROUP BY
                p.provider_code,
                p.provider_order_ref
            HAVING pg_catalog.count(*) > 1
            LIMIT 1
            """
        )
    ).mappings().one_or_none()

    if duplicate is not None:
        raise RuntimeError(
            "zd07 checkout callback provider-order authority "
            "is ambiguous in predecessor data"
        )

    if bind.execute(
        sa.text(
            """
            SELECT pg_catalog.to_regclass(
                'app_private.p4d2_checkout_callback_acl_delta'
            ) IS NOT NULL
            """
        )
    ).scalar_one():
        raise RuntimeError(
            "zd07 checkout callback ACL delta state already exists"
        )

    bind.execute(
        sa.text(
            """
            CREATE TABLE
                app_private.p4d2_checkout_callback_acl_delta (
                    table_name text NOT NULL,
                    column_name text NOT NULL,
                    privilege_name text NOT NULL,
                    CONSTRAINT
                        pk_p4d2_checkout_callback_acl_delta
                    PRIMARY KEY (
                        table_name,
                        column_name,
                        privilege_name
                    ),
                    CONSTRAINT
                        chk_p4d2_checkout_callback_acl_table
                    CHECK (
                        table_name = 'payment_events'
                    ),
                    CONSTRAINT
                        chk_p4d2_checkout_callback_acl_privilege
                    CHECK (
                        privilege_name IN ('SELECT', 'INSERT')
                    )
                )
            """
        )
    )

    bind.execute(
        sa.text(
            """
            REVOKE ALL
            ON TABLE
                app_private.p4d2_checkout_callback_acl_delta
            FROM PUBLIC
            """
        )
    )

    for table_name, privilege_name, columns in (
        _P4D2_CHECKOUT_CALLBACK_COLUMN_PRIVILEGES
    ):
        for column_name in columns:
            already_present = bind.execute(
                sa.text(
                    """
                    SELECT pg_catalog.has_column_privilege(
                        'app_security_owner',
                        :relation,
                        :column_name,
                        :privilege_name
                    )
                    """
                ),
                {
                    "relation": f"finance.{table_name}",
                    "column_name": column_name,
                    "privilege_name": privilege_name,
                },
            ).scalar_one()

            if already_present:
                raise RuntimeError(
                    "zd07 refuses to claim predecessor checkout "
                    "callback column authority: "
                    f"{privilege_name} "
                    f"finance.{table_name}.{column_name}"
                )

            bind.execute(
                sa.text(
                    f"GRANT {privilege_name} ({column_name}) "
                    f"ON TABLE finance.{table_name} "
                    "TO app_security_owner"
                )
            )

            bind.execute(
                sa.text(
                    """
                    INSERT INTO
                        app_private.p4d2_checkout_callback_acl_delta (
                            table_name,
                            column_name,
                            privilege_name
                        )
                    VALUES (
                        :table_name,
                        :column_name,
                        :privilege_name
                    )
                    """
                ),
                {
                    "table_name": table_name,
                    "column_name": column_name,
                    "privilege_name": privilege_name,
                },
            )

    bind.execute(
        sa.text(
            """
            ALTER TABLE finance.payments
            ADD CONSTRAINT
                uq_finance_payments_provider_order_ref
            UNIQUE (
                provider_code,
                provider_order_ref
            )
            """
        )
    )

    op.execute("SET LOCAL ROLE app_security_owner")

    bind.execute(
        sa.text(
            r"""
            CREATE FUNCTION
                app_secure.record_finance_checkout_callback(
                    p_provider_code text,
                    p_provider_order_ref text,
                    p_provider_payment_ref text,
                    p_idempotency_key text,
                    p_request_hash_sha256 text
                )
            RETURNS TABLE(
                payment_id uuid,
                organization_id uuid,
                provider_order_ref text,
                provider_payment_ref text,
                previous_status text,
                payment_status text,
                event_recorded boolean,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_existing_org_id uuid;
                v_payment finance.payments%ROWTYPE;
                v_payment_with_ref finance.payments%ROWTYPE;
                v_idem record;
                v_existing_event finance.payment_events%ROWTYPE;
                v_event finance.payment_events%ROWTYPE;
                v_event_id text;
                v_payload_canonical text;
                v_expected_hash text;
                v_previous_status text;
                v_transition text;
                v_outbox_payload jsonb;
                v_outbox_canonical text;
                v_outbox_hash text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'app_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'P4D checkout callback requires app_runtime'
                        USING ERRCODE='42501';
                END IF;

                IF p_provider_code IS DISTINCT FROM
                    'razorpay_sandbox'
                THEN
                    RAISE EXCEPTION
                        'P4D checkout callback provider unavailable'
                        USING ERRCODE='42501';
                END IF;

                IF p_provider_order_ref IS NULL
                   OR pg_catalog.btrim(
                       p_provider_order_ref
                   ) = ''
                   OR pg_catalog.length(
                       p_provider_order_ref
                   ) > 200
                   OR p_provider_order_ref LIKE 'intent_%'
                THEN
                    RAISE EXCEPTION
                        'P4D checkout callback provider order invalid'
                        USING ERRCODE='22023';
                END IF;

                IF p_provider_payment_ref IS NULL
                   OR pg_catalog.btrim(
                       p_provider_payment_ref
                   ) = ''
                   OR pg_catalog.length(
                       p_provider_payment_ref
                   ) > 200
                THEN
                    RAISE EXCEPTION
                        'P4D checkout callback provider payment invalid'
                        USING ERRCODE='22023';
                END IF;

                IF p_idempotency_key IS NULL
                   OR pg_catalog.btrim(
                       p_idempotency_key
                   ) = ''
                   OR pg_catalog.length(
                       p_idempotency_key
                   ) > 200
                THEN
                    RAISE EXCEPTION
                        'P4D checkout callback idempotency key invalid'
                        USING ERRCODE='22023';
                END IF;

                IF p_request_hash_sha256 IS NULL
                   OR p_request_hash_sha256 !~
                      '^[0-9a-f]{64}$'
                THEN
                    RAISE EXCEPTION
                        'P4D checkout callback request hash invalid'
                        USING ERRCODE='22023';
                END IF;

                v_payload_canonical :=
                    '{"event_type":'
                    || pg_catalog.to_jsonb(
                        'razorpay.checkout.callback.verified'::text
                    )::text
                    || ',"provider_code":'
                    || pg_catalog.to_jsonb(
                        p_provider_code
                    )::text
                    || ',"provider_order_ref":'
                    || pg_catalog.to_jsonb(
                        p_provider_order_ref
                    )::text
                    || ',"provider_payment_ref":'
                    || pg_catalog.to_jsonb(
                        p_provider_payment_ref
                    )::text
                    || ',"source":'
                    || pg_catalog.to_jsonb(
                        'razorpay_checkout_callback'::text
                    )::text
                    || ',"target_status":"authorized"}';

                v_expected_hash :=
                    pg_catalog.encode(
                        pg_catalog.sha256(
                            pg_catalog.convert_to(
                                v_payload_canonical,
                                'UTF8'
                            )
                        ),
                        'hex'
                    );

                IF v_expected_hash IS DISTINCT FROM
                    p_request_hash_sha256
                THEN
                    RAISE EXCEPTION
                        'P4D checkout callback request hash mismatch'
                        USING ERRCODE='23514';
                END IF;

                SELECT p.*
                INTO v_payment
                FROM finance.payments p
                WHERE p.provider_code = p_provider_code
                  AND p.provider_order_ref =
                      p_provider_order_ref
                FOR UPDATE;

                IF NOT FOUND
                   OR v_payment.organization_id IS NULL
                THEN
                    RAISE EXCEPTION
                        'P4D checkout callback order unavailable'
                        USING ERRCODE='P0002';
                END IF;

                BEGIN
                    v_existing_org_id :=
                        NULLIF(
                            pg_catalog.current_setting(
                                'app.current_org_id',
                                true
                            ),
                            ''
                        )::uuid;
                EXCEPTION
                    WHEN invalid_text_representation
                    THEN
                        RAISE EXCEPTION
                            'P4D checkout callback existing '
                            'tenant context invalid'
                            USING ERRCODE='22023';
                END;

                IF v_existing_org_id IS NOT NULL
                   AND v_existing_org_id IS DISTINCT FROM
                       v_payment.organization_id
                THEN
                    RAISE EXCEPTION
                        'P4D checkout callback tenant conflict'
                        USING ERRCODE='42501';
                END IF;

                PERFORM pg_catalog.set_config(
                    'app.current_org_id',
                    v_payment.organization_id::text,
                    true
                );

                v_event_id :=
                    'checkout_callback:'
                    || pg_catalog.encode(
                        pg_catalog.sha256(
                            pg_catalog.convert_to(
                                p_provider_order_ref
                                || '|'
                                || p_provider_payment_ref,
                                'UTF8'
                            )
                        ),
                        'hex'
                    );

                SELECT *
                INTO v_idem
                FROM app_secure.reserve_finance_idempotency(
                    'finance.checkout_callback.record',
                    p_idempotency_key,
                    p_request_hash_sha256,
                    v_payment.organization_id,
                    pg_catalog.clock_timestamp()
                        + interval '7 days'
                );

                SELECT pe.*
                INTO v_existing_event
                FROM finance.payment_events pe
                WHERE pe.provider_code =
                      p_provider_code
                  AND pe.provider_event_id =
                      v_event_id;

                IF NOT v_idem.inserted THEN
                    IF v_idem.response_ref IS NULL THEN
                        RAISE EXCEPTION
                            'P4D checkout callback already processing'
                            USING ERRCODE='23505';
                    END IF;

                    IF v_existing_event.id IS NULL
                       OR v_existing_event.payment_id
                          IS DISTINCT FROM v_payment.id
                       OR v_existing_event
                          .event_payload_sha256::text
                          IS DISTINCT FROM
                          p_request_hash_sha256
                       OR v_existing_event.event_type
                          IS DISTINCT FROM
                          'razorpay.checkout.callback.verified'
                    THEN
                        RAISE EXCEPTION
                            'P4D checkout callback replay conflict'
                            USING ERRCODE='23514';
                    END IF;

                    IF v_payment.provider_payment_ref
                       IS NOT NULL
                       AND v_payment.provider_payment_ref
                           IS DISTINCT FROM
                           p_provider_payment_ref
                    THEN
                        RAISE EXCEPTION
                            'P4D checkout callback payment mismatch'
                            USING ERRCODE='23505';
                    END IF;

                    RETURN QUERY
                    SELECT
                        v_payment.id,
                        v_payment.organization_id,
                        p_provider_order_ref,
                        p_provider_payment_ref,
                        v_payment.status::text,
                        v_payment.status::text,
                        false,
                        true;

                    RETURN;
                END IF;

                IF v_payment.provider_payment_ref
                   IS NOT NULL
                   AND v_payment.provider_payment_ref
                       IS DISTINCT FROM
                       p_provider_payment_ref
                THEN
                    RAISE EXCEPTION
                        'P4D checkout callback payment mismatch'
                        USING ERRCODE='23505';
                END IF;

                SELECT p.*
                INTO v_payment_with_ref
                FROM finance.payments p
                WHERE p.provider_code =
                      p_provider_code
                  AND p.provider_payment_ref =
                      p_provider_payment_ref
                FOR UPDATE;

                IF FOUND
                   AND v_payment_with_ref.id
                       IS DISTINCT FROM v_payment.id
                THEN
                    RAISE EXCEPTION
                        'P4D checkout callback payment conflict'
                        USING ERRCODE='23505';
                END IF;

                IF v_existing_event.id IS NOT NULL THEN
                    IF v_existing_event.payment_id
                          IS DISTINCT FROM v_payment.id
                       OR v_existing_event
                          .event_payload_sha256::text
                          IS DISTINCT FROM
                          p_request_hash_sha256
                       OR v_existing_event.event_type
                          IS DISTINCT FROM
                          'razorpay.checkout.callback.verified'
                    THEN
                        RAISE EXCEPTION
                            'P4D checkout callback replay conflict'
                            USING ERRCODE='23514';
                    END IF;

                    IF v_payment.provider_payment_ref IS NULL
                    THEN
                        RAISE EXCEPTION
                            'P4D checkout callback replay inconsistent'
                            USING ERRCODE='23514';
                    END IF;

                    PERFORM 1
                    FROM app_secure.complete_finance_idempotency(
                        v_idem.id,
                        v_existing_event.id::text
                    );

                    RETURN QUERY
                    SELECT
                        v_payment.id,
                        v_payment.organization_id,
                        p_provider_order_ref,
                        p_provider_payment_ref,
                        v_payment.status::text,
                        v_payment.status::text,
                        false,
                        true;

                    RETURN;
                END IF;

                v_previous_status := v_payment.status::text;

                IF v_payment.status IN (
                    'created',
                    'pending'
                ) THEN
                    v_transition := 'apply';

                ELSIF v_payment.status IN (
                    'authorized',
                    'captured',
                    'partially_refunded',
                    'settled'
                ) THEN
                    v_transition := 'ignore';

                ELSE
                    RAISE EXCEPTION
                        'P4D checkout callback payment state invalid'
                        USING ERRCODE='23514';
                END IF;

                IF v_payment.provider_payment_ref IS NULL THEN
                    BEGIN
                        UPDATE finance.payments p
                        SET
                            provider_payment_ref =
                                p_provider_payment_ref,
                            updated_at =
                                pg_catalog.clock_timestamp()
                        WHERE p.id = v_payment.id;

                    EXCEPTION
                        WHEN unique_violation
                        THEN
                            RAISE EXCEPTION
                                'P4D checkout callback payment conflict'
                                USING ERRCODE='23505';
                    END;

                    v_payment.provider_payment_ref :=
                        p_provider_payment_ref;
                END IF;

                INSERT INTO finance.payment_events (
                    payment_id,
                    provider_code,
                    provider_event_id,
                    event_type,
                    event_payload_sha256
                )
                VALUES (
                    v_payment.id,
                    p_provider_code,
                    v_event_id,
                    'razorpay.checkout.callback.verified',
                    p_request_hash_sha256::char(64)
                )
                RETURNING *
                INTO v_event;

                IF v_transition = 'apply' THEN
                    UPDATE finance.payments p
                    SET
                        status = 'authorized',
                        raw_status = 'authorized',
                        updated_at =
                            pg_catalog.clock_timestamp()
                    WHERE p.id = v_payment.id;

                    v_payment.status := 'authorized';
                    v_payment.raw_status := 'authorized';

                    v_outbox_payload :=
                        pg_catalog.jsonb_build_object(
                            'payment_id',
                            v_payment.id::text,
                            'previous_status',
                            v_previous_status,
                            'status',
                            'authorized',
                            'provider_event_id',
                            v_event_id
                        );

                    v_outbox_canonical :=
                        '{"payment_id":'
                        || pg_catalog.to_jsonb(
                            v_payment.id::text
                        )::text
                        || ',"previous_status":'
                        || pg_catalog.to_jsonb(
                            v_previous_status
                        )::text
                        || ',"provider_event_id":'
                        || pg_catalog.to_jsonb(
                            v_event_id
                        )::text
                        || ',"status":"authorized"}';

                    v_outbox_hash :=
                        pg_catalog.encode(
                            pg_catalog.sha256(
                                pg_catalog.convert_to(
                                    v_outbox_canonical,
                                    'UTF8'
                                )
                            ),
                            'hex'
                        );

                    INSERT INTO finance.outbox_events (
                        organization_id,
                        legal_entity_id,
                        division_id,
                        brand_id,
                        aggregate_type,
                        aggregate_id,
                        event_type,
                        idempotency_key,
                        payload_json,
                        payload_sha256,
                        status
                    )
                    VALUES (
                        v_payment.organization_id,
                        v_payment.legal_entity_id,
                        v_payment.division_id,
                        v_payment.brand_id,
                        'payment',
                        v_payment.id,
                        'finance.payment.state_changed',
                        v_event_id || pg_catalog.chr(58) || 'state',
                        v_outbox_payload,
                        v_outbox_hash,
                        'pending'
                    );
                END IF;

                PERFORM 1
                FROM app_secure.complete_finance_idempotency(
                    v_idem.id,
                    v_event.id::text
                );

                RETURN QUERY
                SELECT
                    v_payment.id,
                    v_payment.organization_id,
                    p_provider_order_ref,
                    p_provider_payment_ref,
                    v_previous_status,
                    v_payment.status::text,
                    true,
                    v_transition <> 'apply';
            END;
            $function$
            """
        )
    )

    bind.execute(
        sa.text(
            """
            REVOKE ALL ON FUNCTION
                app_secure.record_finance_checkout_callback(
                    text,text,text,text,text
                )
            FROM PUBLIC
            """
        )
    )

    bind.execute(
        sa.text(
            """
            GRANT EXECUTE ON FUNCTION
                app_secure.record_finance_checkout_callback(
                    text,text,text,text,text
                )
            TO app_runtime
            """
        )
    )

    bind.execute(
        sa.text(
            """
            ALTER FUNCTION
                app_secure.record_finance_checkout_callback(
                    text,text,text,text,text
                )
            OWNER TO app_security_owner
            """
        )
    )

    op.execute("RESET ROLE")

    _p4d2_require_checkout_callback_authority(bind)


def _p4d2_remove_checkout_callback_authority(bind) -> None:
    _require_identity_contract(bind)

    state_exists = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.to_regclass(
                'app_private.p4d2_checkout_callback_acl_delta'
            ) IS NOT NULL
            """
        )
    ).scalar_one()

    if not state_exists:
        return

    op.execute("SET LOCAL ROLE app_security_owner")

    try:
        has_callback_evidence = bind.execute(
            sa.text(
                """
                SELECT
                       EXISTS (
                           SELECT 1
                           FROM finance.payment_events pe
                           WHERE pe.event_type =
                                 'razorpay.checkout.callback.verified'
                           LIMIT 1
                       )
                    OR EXISTS (
                           SELECT 1
                           FROM finance.idempotency_keys ik
                           WHERE ik.scope =
                                 'finance.checkout_callback.record'
                           LIMIT 1
                       )
                """
            )
        ).scalar_one()
    finally:
        op.execute("RESET ROLE")

    if has_callback_evidence:
        raise RuntimeError(
            "zd07 downgrade blocked: checkout callback "
            "authority/evidence exists"
        )

    op.execute("SET LOCAL ROLE app_security_owner")

    bind.execute(
        sa.text(
            """
            DROP FUNCTION IF EXISTS
                app_secure.record_finance_checkout_callback(
                    text,text,text,text,text
                )
            """
        )
    )

    op.execute("RESET ROLE")

    bind.execute(
        sa.text(
            """
            ALTER TABLE finance.payments
            DROP CONSTRAINT IF EXISTS
                uq_finance_payments_provider_order_ref
            RESTRICT
            """
        )
    )

    rows = bind.execute(
        sa.text(
            """
            SELECT
                table_name,
                column_name,
                privilege_name
            FROM app_private.p4d2_checkout_callback_acl_delta
            ORDER BY
                table_name DESC,
                privilege_name DESC,
                column_name DESC
            """
        )
    ).mappings().all()

    for row in rows:
        table_name = row["table_name"]
        column_name = row["column_name"]
        privilege_name = row["privilege_name"]

        if (
            table_name != "payment_events"
            or privilege_name not in {"SELECT", "INSERT"}
            or not column_name.replace("_", "").isalnum()
        ):
            raise RuntimeError(
                "Unsafe stored P4D-2 callback ACL identifier"
            )

        bind.execute(
            sa.text(
                f"REVOKE {privilege_name} ({column_name}) "
                f"ON TABLE finance.{table_name} "
                "FROM app_security_owner"
            )
        )

    bind.execute(
        sa.text(
            """
            DROP TABLE
                app_private.p4d2_checkout_callback_acl_delta
            """
        )
    )

    _require_exact_function_count(
        bind,
        schema_name="app_secure",
        function_name="record_finance_checkout_callback",
        normalized_args="text,text,text,text,text",
        expected_count=0,
        error_label=(
            "zd07 downgrade failed to remove checkout "
            "callback capability"
        ),
    )

    if _constraint_exists(
        bind,
        "finance",
        "payments",
        "uq_finance_payments_provider_order_ref",
    ):
        raise RuntimeError(
            "zd07 downgrade failed to remove provider-order "
            "uniqueness constraint"
        )

    _require_identity_contract(bind)



_P4D2_PROVIDER_EVIDENCE_FUNCTION = (
    "app_secure.confirm_finance_provider_evidence("
    "text,text,text,text,text,bigint,text,text,text,text)"
)


def _p4d2_require_provider_evidence_authority(bind) -> None:
    _require_identity_contract(bind)

    _require_function_contract(
        bind,
        function_name="confirm_finance_provider_evidence",
        normalized_args=(
            "text,text,text,text,text,bigint,"
            "text,text,text,text"
        ),
        allowed_role="app_runtime",
        blocked_roles=(
            "auth_runtime",
            "worker_runtime",
            "lifecycle_maintenance_runtime",
            "finance_config_runtime",
        ),
        error_label="finance provider evidence capability",
    )

    payment_event_update = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_table_privilege(
                'app_security_owner',
                'finance.payment_events',
                'UPDATE'
            )
            """
        )
    ).scalar_one()

    if payment_event_update:
        raise RuntimeError(
            "zd07 provider evidence authority must not require "
            "payment_events UPDATE"
        )

    function_body = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.pg_get_functiondef(p.oid)
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n
              ON n.oid = p.pronamespace
            WHERE n.nspname = 'app_secure'
              AND p.proname =
                  'confirm_finance_provider_evidence'
            """
        )
    ).scalar_one()

    if (
        "pg_catalog.set_config(" not in function_body
        or "'app.current_org_id'" not in function_body
        or "app_secure.reserve_finance_idempotency(" not in function_body
        or "app_secure.complete_finance_idempotency(" not in function_body
    ):
        raise RuntimeError(
            "zd07 provider evidence tenant/idempotency "
            "boundary drift"
        )

    payment_event_tail = function_body.split(
        "FROM finance.payment_events pe",
        1,
    )[1]

    payment_event_lookup = payment_event_tail.split(
        "IF v_existing_event.id IS NOT NULL",
        1,
    )[0]

    if "FOR UPDATE" in payment_event_lookup:
        raise RuntimeError(
            "zd07 provider evidence replay lookup must "
            "remain read-only"
        )


def _p4d2_install_provider_evidence_authority(bind) -> None:
    _require_identity_contract(bind)

    _require_exact_function_count(
        bind,
        schema_name="app_secure",
        function_name="confirm_finance_provider_evidence",
        normalized_args=(
            "text,text,text,text,text,bigint,"
            "text,text,text,text"
        ),
        expected_count=0,
        error_label=(
            "zd07 provider evidence capability already exists"
        ),
    )

    op.execute("SET LOCAL ROLE app_security_owner")

    bind.execute(
        sa.text(
            r"""
            CREATE FUNCTION
                app_secure.confirm_finance_provider_evidence(
                    p_provider_code text,
                    p_provider_event_id text,
                    p_event_type text,
                    p_provider_order_ref text,
                    p_provider_payment_ref text,
                    p_provider_amount_subunits bigint,
                    p_provider_currency text,
                    p_provider_payment_status text,
                    p_idempotency_key text,
                    p_request_hash_sha256 text
                )
            RETURNS TABLE(
                payment_event_id uuid,
                payment_id uuid,
                organization_id uuid,
                provider_code text,
                provider_event_id text,
                event_type text,
                previous_payment_status text,
                payment_status text,
                event_recorded boolean,
                state_changed boolean,
                state_ignored boolean,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_existing_org_id uuid;
                v_payment finance.payments%ROWTYPE;
                v_payment_by_ref finance.payments%ROWTYPE;
                v_existing_event finance.payment_events%ROWTYPE;
                v_event finance.payment_events%ROWTYPE;
                v_idem record;
                v_target_status text;
                v_previous_status text;
                v_transition text;
                v_outbox_payload jsonb;
                v_outbox_canonical text;
                v_outbox_hash text;
                v_outbox_idempotency_key text;
                v_amount_subunits numeric;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'app_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'P4D provider evidence requires app_runtime'
                        USING ERRCODE='42501';
                END IF;

                IF p_provider_code IS DISTINCT FROM
                    'razorpay_sandbox'
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence provider unavailable'
                        USING ERRCODE='42501';
                END IF;

                IF p_provider_event_id IS NULL
                   OR p_provider_event_id !~
                      '^[A-Za-z0-9_-]{1,200}$'
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence event id invalid'
                        USING ERRCODE='22023';
                END IF;

                IF p_provider_order_ref IS NULL
                   OR p_provider_order_ref !~
                      '^[A-Za-z0-9_-]{1,200}$'
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence order invalid'
                        USING ERRCODE='22023';
                END IF;

                IF p_provider_payment_ref IS NULL
                   OR p_provider_payment_ref !~
                      '^[A-Za-z0-9_-]{1,200}$'
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence payment ref invalid'
                        USING ERRCODE='22023';
                END IF;

                IF p_provider_amount_subunits IS NULL
                   OR p_provider_amount_subunits < 0
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence amount invalid'
                        USING ERRCODE='22023';
                END IF;

                IF p_provider_currency IS NULL
                   OR p_provider_currency !~ '^[A-Z]{3}$'
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence currency invalid'
                        USING ERRCODE='22023';
                END IF;

                IF p_idempotency_key IS NULL
                   OR pg_catalog.btrim(
                       p_idempotency_key
                   ) = ''
                   OR pg_catalog.length(
                       p_idempotency_key
                   ) > 200
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence idempotency invalid'
                        USING ERRCODE='22023';
                END IF;

                IF p_request_hash_sha256 IS NULL
                   OR p_request_hash_sha256 !~
                      '^[0-9a-f]{64}$'
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence request hash invalid'
                        USING ERRCODE='22023';
                END IF;

                CASE p_event_type
                    WHEN 'payment.authorized'
                    THEN
                        v_target_status := 'authorized';

                    WHEN 'payment.captured'
                    THEN
                        v_target_status := 'captured';

                    WHEN 'order.paid'
                    THEN
                        v_target_status := 'captured';

                    WHEN 'payment.failed'
                    THEN
                        v_target_status := 'failed';

                    ELSE
                        RAISE EXCEPTION
                            'P4D provider evidence event unavailable'
                            USING ERRCODE='42501';
                END CASE;

                IF p_provider_payment_status
                   IS DISTINCT FROM v_target_status
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence status mismatch'
                        USING ERRCODE='23514';
                END IF;

                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        p_provider_code
                        || ':'
                        || p_provider_event_id,
                        0
                    )
                );

                SELECT p.*
                INTO v_payment
                FROM finance.payments p
                WHERE p.provider_code = p_provider_code
                  AND p.provider_order_ref =
                      p_provider_order_ref
                FOR UPDATE;

                IF NOT FOUND THEN
                    SELECT p.*
                    INTO v_payment_by_ref
                    FROM finance.payments p
                    WHERE p.provider_code =
                          p_provider_code
                      AND p.provider_payment_ref =
                          p_provider_payment_ref
                    FOR UPDATE;

                    IF FOUND THEN
                        RAISE EXCEPTION
                            'P4D provider evidence order mismatch'
                            USING ERRCODE='23514';
                    END IF;

                    RAISE EXCEPTION
                        'P4D provider evidence payment not found'
                        USING ERRCODE='P0002';
                END IF;

                SELECT p.*
                INTO v_payment_by_ref
                FROM finance.payments p
                WHERE p.provider_code =
                      p_provider_code
                  AND p.provider_payment_ref =
                      p_provider_payment_ref
                FOR UPDATE;

                IF FOUND
                   AND v_payment_by_ref.id
                       IS DISTINCT FROM v_payment.id
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence reference mismatch'
                        USING ERRCODE='23514';
                END IF;

                IF v_payment.provider_payment_ref IS NOT NULL
                   AND v_payment.provider_payment_ref
                       IS DISTINCT FROM
                       p_provider_payment_ref
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence payment mismatch'
                        USING ERRCODE='23514';
                END IF;

                IF v_payment.organization_id IS NULL THEN
                    RAISE EXCEPTION
                        'P4D provider evidence payment unavailable'
                        USING ERRCODE='P0002';
                END IF;

                BEGIN
                    v_existing_org_id :=
                        NULLIF(
                            pg_catalog.current_setting(
                                'app.current_org_id',
                                true
                            ),
                            ''
                        )::uuid;
                EXCEPTION
                    WHEN invalid_text_representation
                    THEN
                        RAISE EXCEPTION
                            'P4D provider evidence existing '
                            'tenant context invalid'
                            USING ERRCODE='22023';
                END;

                IF v_existing_org_id IS NOT NULL
                   AND v_existing_org_id IS DISTINCT FROM
                       v_payment.organization_id
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence tenant conflict'
                        USING ERRCODE='42501';
                END IF;

                PERFORM pg_catalog.set_config(
                    'app.current_org_id',
                    v_payment.organization_id::text,
                    true
                );

                v_amount_subunits :=
                    v_payment.amount * 100;

                IF v_payment.amount IS NULL
                   OR v_amount_subunits IS NULL
                   OR v_amount_subunits <>
                      pg_catalog.trunc(
                          v_amount_subunits
                      )
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence server amount invalid'
                        USING ERRCODE='23514';
                END IF;

                IF v_amount_subunits::bigint
                   IS DISTINCT FROM
                   p_provider_amount_subunits
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence amount mismatch'
                        USING ERRCODE='23514';
                END IF;

                IF pg_catalog.btrim(
                       v_payment.currency_code::text
                   )
                   IS DISTINCT FROM
                   p_provider_currency
                THEN
                    RAISE EXCEPTION
                        'P4D provider evidence currency mismatch'
                        USING ERRCODE='23514';
                END IF;

                SELECT pe.*
                INTO v_existing_event
                FROM finance.payment_events pe
                WHERE pe.provider_code =
                      p_provider_code
                  AND pe.provider_event_id =
                      p_provider_event_id;

                IF v_existing_event.id IS NOT NULL THEN
                    IF v_existing_event.payment_id
                           IS DISTINCT FROM v_payment.id
                       OR v_existing_event.event_type
                           IS DISTINCT FROM p_event_type
                       OR v_existing_event
                           .event_payload_sha256::text
                           IS DISTINCT FROM
                           p_request_hash_sha256
                    THEN
                        RAISE EXCEPTION
                            'P4D provider evidence event conflict'
                            USING ERRCODE='23505';
                    END IF;

                    SELECT *
                    INTO v_idem
                    FROM app_secure.reserve_finance_idempotency(
                        'finance.provider.capture.confirm',
                        p_idempotency_key,
                        p_request_hash_sha256,
                        v_payment.organization_id,
                        pg_catalog.clock_timestamp()
                            + interval '7 days'
                    );

                    IF v_idem.inserted THEN
                        PERFORM 1
                        FROM app_secure.complete_finance_idempotency(
                            v_idem.id,
                            v_existing_event.id::text
                        );

                    ELSIF v_idem.response_ref IS NULL THEN
                        RAISE EXCEPTION
                            'P4D provider evidence already processing'
                            USING ERRCODE='23505';
                    END IF;

                    RETURN QUERY
                    SELECT
                        v_existing_event.id,
                        v_payment.id,
                        v_payment.organization_id,
                        p_provider_code,
                        p_provider_event_id,
                        p_event_type,
                        v_payment.status::text,
                        v_payment.status::text,
                        false,
                        false,
                        (
                            v_payment.status::text
                            IS DISTINCT FROM
                            v_target_status
                        ),
                        true;

                    RETURN;
                END IF;

                SELECT *
                INTO v_idem
                FROM app_secure.reserve_finance_idempotency(
                    'finance.provider.capture.confirm',
                    p_idempotency_key,
                    p_request_hash_sha256,
                    v_payment.organization_id,
                    pg_catalog.clock_timestamp()
                        + interval '7 days'
                );

                IF NOT v_idem.inserted THEN
                    RAISE EXCEPTION
                        'P4D provider evidence idempotency conflict'
                        USING ERRCODE='23505';
                END IF;

                v_previous_status :=
                    v_payment.status::text;

                IF v_previous_status =
                   v_target_status
                THEN
                    v_transition := 'noop';

                ELSIF v_previous_status IN (
                    'failed',
                    'cancelled',
                    'refunded'
                ) THEN
                    RAISE EXCEPTION
                        'P4D provider evidence state transition invalid'
                        USING ERRCODE='23514';

                ELSIF v_target_status = 'authorized'
                   AND v_previous_status IN (
                       'created',
                       'pending'
                   )
                THEN
                    v_transition := 'apply';

                ELSIF v_target_status = 'captured'
                   AND v_previous_status IN (
                       'created',
                       'pending',
                       'authorized'
                   )
                THEN
                    v_transition := 'apply';

                ELSIF v_target_status = 'failed'
                   AND v_previous_status IN (
                       'created',
                       'pending',
                       'authorized'
                   )
                THEN
                    v_transition := 'apply';

                ELSIF v_target_status = 'authorized'
                   AND v_previous_status IN (
                       'captured',
                       'settled',
                       'partially_refunded'
                   )
                THEN
                    v_transition := 'ignore_stale';

                ELSIF v_target_status = 'captured'
                   AND v_previous_status IN (
                       'settled',
                       'partially_refunded'
                   )
                THEN
                    v_transition := 'ignore_stale';

                ELSE
                    RAISE EXCEPTION
                        'P4D provider evidence state transition invalid'
                        USING ERRCODE='23514';
                END IF;

                IF v_payment.provider_payment_ref IS NULL THEN
                    BEGIN
                        UPDATE finance.payments p
                        SET
                            provider_payment_ref =
                                p_provider_payment_ref,
                            updated_at =
                                pg_catalog.clock_timestamp()
                        WHERE p.id = v_payment.id;

                    EXCEPTION
                        WHEN unique_violation
                        THEN
                            RAISE EXCEPTION
                                'P4D provider evidence payment conflict'
                                USING ERRCODE='23505';
                    END;

                    v_payment.provider_payment_ref :=
                        p_provider_payment_ref;
                END IF;

                INSERT INTO finance.payment_events (
                    payment_id,
                    provider_code,
                    provider_event_id,
                    event_type,
                    event_payload_sha256
                )
                VALUES (
                    v_payment.id,
                    p_provider_code,
                    p_provider_event_id,
                    p_event_type,
                    p_request_hash_sha256::char(64)
                )
                RETURNING *
                INTO v_event;

                IF v_transition = 'apply' THEN
                    UPDATE finance.payments p
                    SET
                        status =
                            v_target_status,
                        raw_status =
                            p_provider_payment_status,
                        updated_at =
                            pg_catalog.clock_timestamp()
                    WHERE p.id = v_payment.id;

                    v_payment.status :=
                        v_target_status;
                    v_payment.raw_status :=
                        p_provider_payment_status;

                    v_outbox_payload :=
                        pg_catalog.jsonb_build_object(
                            'payment_id',
                            v_payment.id::text,
                            'previous_status',
                            v_previous_status,
                            'status',
                            v_target_status,
                            'provider_event_id',
                            p_provider_event_id
                        );

                    v_outbox_canonical :=
                        '{"payment_id":'
                        || pg_catalog.to_jsonb(
                            v_payment.id::text
                        )::text
                        || ',"previous_status":'
                        || pg_catalog.to_jsonb(
                            v_previous_status
                        )::text
                        || ',"provider_event_id":'
                        || pg_catalog.to_jsonb(
                            p_provider_event_id
                        )::text
                        || ',"status":'
                        || pg_catalog.to_jsonb(
                            v_target_status
                        )::text
                        || '}';

                    v_outbox_hash :=
                        pg_catalog.encode(
                            pg_catalog.sha256(
                                pg_catalog.convert_to(
                                    v_outbox_canonical,
                                    'UTF8'
                                )
                            ),
                            'hex'
                        );

                    v_outbox_idempotency_key :=
                        'provider-state:'
                        || pg_catalog.encode(
                            pg_catalog.sha256(
                                pg_catalog.convert_to(
                                    p_provider_code
                                    || '|'
                                    || p_provider_event_id,
                                    'UTF8'
                                )
                            ),
                            'hex'
                        );

                    INSERT INTO finance.outbox_events (
                        organization_id,
                        legal_entity_id,
                        division_id,
                        brand_id,
                        aggregate_type,
                        aggregate_id,
                        event_type,
                        idempotency_key,
                        payload_json,
                        payload_sha256,
                        status
                    )
                    VALUES (
                        v_payment.organization_id,
                        v_payment.legal_entity_id,
                        v_payment.division_id,
                        v_payment.brand_id,
                        'payment',
                        v_payment.id,
                        'finance.payment.state_changed',
                        v_outbox_idempotency_key,
                        v_outbox_payload,
                        v_outbox_hash,
                        'pending'
                    );
                END IF;

                PERFORM 1
                FROM app_secure.complete_finance_idempotency(
                    v_idem.id,
                    v_event.id::text
                );

                RETURN QUERY
                SELECT
                    v_event.id,
                    v_payment.id,
                    v_payment.organization_id,
                    p_provider_code,
                    p_provider_event_id,
                    p_event_type,
                    v_previous_status,
                    v_payment.status::text,
                    true,
                    v_transition = 'apply',
                    v_transition = 'ignore_stale',
                    false;
            END;
            $function$
            """
        )
    )

    bind.execute(
        sa.text(
            """
            REVOKE ALL ON FUNCTION
                app_secure.confirm_finance_provider_evidence(
                    text,text,text,text,text,bigint,
                    text,text,text,text
                )
            FROM PUBLIC
            """
        )
    )

    bind.execute(
        sa.text(
            """
            GRANT EXECUTE ON FUNCTION
                app_secure.confirm_finance_provider_evidence(
                    text,text,text,text,text,bigint,
                    text,text,text,text
                )
            TO app_runtime
            """
        )
    )

    bind.execute(
        sa.text(
            """
            ALTER FUNCTION
                app_secure.confirm_finance_provider_evidence(
                    text,text,text,text,text,bigint,
                    text,text,text,text
                )
            OWNER TO app_security_owner
            """
        )
    )

    op.execute("RESET ROLE")

    _p4d2_require_provider_evidence_authority(bind)


def _p4d2_downgrade_provider_evidence_authority(bind) -> None:
    _require_identity_contract(bind)

    op.execute("SET LOCAL ROLE app_security_owner")

    bind.execute(
        sa.text(
            """
            DROP FUNCTION IF EXISTS
                app_secure.confirm_finance_provider_evidence(
                    text,text,text,text,text,bigint,
                    text,text,text,text
                )
            """
        )
    )

    op.execute("RESET ROLE")

    _require_exact_function_count(
        bind,
        schema_name="app_secure",
        function_name="confirm_finance_provider_evidence",
        normalized_args=(
            "text,text,text,text,text,bigint,"
            "text,text,text,text"
        ),
        expected_count=0,
        error_label=(
            "zd07 downgrade failed to remove provider "
            "evidence capability"
        ),
    )


_P4D2_PAYMENT_APPLICATION_FUNCTION = (
    "app_secure.apply_finance_confirmed_payment("
    "uuid,uuid,numeric,text,text,text)"
)

_P4D2_PAYMENT_APPLICATION_ACL_STATE = (
    "app_private.p4d2_payment_application_acl_delta"
)

_P4D2_PAYMENT_APPLICATION_SCHEMA_ACL_STATE = (
    "app_private.p4d2_payment_application_schema_acl_delta"
)

_P4D2_PAYMENT_APPLICATION_EVIDENCE = (
    "app_private.p4d2_payment_application_evidence"
)

_P4D2_PAYMENT_APPLICATION_COLUMN_PRIVILEGES = (
    (
        "invoices",
        "UPDATE",
        (
            "status",
        ),
    ),
    (
        "payment_allocations",
        "INSERT",
        (
            "payment_id",
            "invoice_id",
            "allocated_amount",
        ),
    ),
    (
        "ledger_accounts",
        "SELECT",
        (
            "id",
            "legal_entity_id",
            "code",
        ),
    ),
    (
        "ledger_entries",
        "SELECT",
        (
            "id",
            "source_type",
            "source_id",
            "status",
        ),
    ),
    (
        "ledger_entries",
        "INSERT",
        (
            "legal_entity_id",
            "division_id",
            "brand_id",
            "entry_type",
            "source_type",
            "source_id",
            "status",
            "posted_at",
        ),
    ),
    (
        "ledger_entry_lines",
        "INSERT",
        (
            "ledger_entry_id",
            "ledger_account_id",
            "debit_amount",
            "credit_amount",
            "memo",
        ),
    ),
    (
        "outbox_events",
        "INSERT",
        (
            "organization_id",
            "legal_entity_id",
            "division_id",
            "brand_id",
            "aggregate_type",
            "aggregate_id",
            "event_type",
            "idempotency_key",
            "payload_json",
            "payload_sha256",
            "status",
        ),
    ),
)


def _p4d2_grant_payment_application_column_if_missing(
    bind,
    *,
    table_name: str,
    column_name: str,
    privilege_name: str,
) -> None:
    already_present = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_column_privilege(
                'app_security_owner',
                :relation_name,
                :column_name,
                :privilege_name
            )
            """
        ),
        {
            "relation_name": f"finance.{table_name}",
            "column_name": column_name,
            "privilege_name": privilege_name,
        },
    ).scalar_one()

    if already_present:
        return

    bind.execute(
        sa.text(
            f"GRANT {privilege_name} ({column_name}) "
            f"ON TABLE finance.{table_name} "
            "TO app_security_owner"
        )
    )

    bind.execute(
        sa.text(
            """
            INSERT INTO
                app_private.p4d2_payment_application_acl_delta(
                    table_name,
                    column_name,
                    privilege_name
                )
            VALUES (
                :table_name,
                :column_name,
                :privilege_name
            )
            """
        ),
        {
            "table_name": table_name,
            "column_name": column_name,
            "privilege_name": privilege_name,
        },
    )


def _p4d2_require_payment_application_authority(bind) -> None:
    _require_identity_contract(bind)

    _require_function_contract(
        bind,
        function_name="apply_finance_confirmed_payment",
        normalized_args="uuid,uuid,numeric,text,text,text",
        allowed_role="app_runtime",
        blocked_roles=(
            "auth_runtime",
            "worker_runtime",
            "lifecycle_maintenance_runtime",
            "finance_config_runtime",
        ),
        error_label="finance confirmed payment application capability",
    )

    for relation, privilege in (
        ("finance.payment_allocations", "INSERT"),
        ("finance.ledger_entries", "INSERT"),
        ("finance.ledger_entry_lines", "INSERT"),
        ("finance.outbox_events", "INSERT"),
        ("finance.invoices", "UPDATE"),
    ):
        if bind.execute(
            sa.text(
                """
                SELECT pg_catalog.has_table_privilege(
                    'app_runtime',
                    :relation_name,
                    :privilege_name
                )
                """
            ),
            {
                "relation_name": relation,
                "privilege_name": privilege,
            },
        ).scalar_one():
            raise RuntimeError(
                "zd07 payment application leaked direct Finance "
                f"{privilege} authority on {relation} to app_runtime"
            )

    for state_relation in (
        _P4D2_PAYMENT_APPLICATION_ACL_STATE,
        _P4D2_PAYMENT_APPLICATION_SCHEMA_ACL_STATE,
        _P4D2_PAYMENT_APPLICATION_EVIDENCE,
    ):
        if not bind.execute(
            sa.text(
                """
                SELECT pg_catalog.to_regclass(
                    :relation_name
                ) IS NOT NULL
                """
            ),
            {"relation_name": state_relation},
        ).scalar_one():
            raise RuntimeError(
                "zd07 payment application state relation missing: "
                f"{state_relation}"
            )


def _p4d2_install_payment_application_authority(bind) -> None:
    _require_identity_contract(bind)

    _require_exact_function_count(
        bind,
        schema_name="app_secure",
        function_name="apply_finance_confirmed_payment",
        normalized_args="uuid,uuid,numeric,text,text,text",
        expected_count=0,
        error_label=(
            "zd07 payment application capability already exists"
        ),
    )

    for relation_name in (
        _P4D2_PAYMENT_APPLICATION_ACL_STATE,
        _P4D2_PAYMENT_APPLICATION_SCHEMA_ACL_STATE,
        _P4D2_PAYMENT_APPLICATION_EVIDENCE,
    ):
        if bind.execute(
            sa.text(
                """
                SELECT pg_catalog.to_regclass(
                    :relation_name
                ) IS NOT NULL
                """
            ),
            {"relation_name": relation_name},
        ).scalar_one():
            raise RuntimeError(
                "zd07 payment application state already exists: "
                f"{relation_name}"
            )

    bind.execute(
        sa.text(
            """
            CREATE TABLE
                app_private.p4d2_payment_application_acl_delta (
                    table_name text NOT NULL,
                    column_name text NOT NULL,
                    privilege_name text NOT NULL,
                    CONSTRAINT
                        pk_p4d2_payment_application_acl_delta
                    PRIMARY KEY (
                        table_name,
                        column_name,
                        privilege_name
                    ),
                    CONSTRAINT
                        chk_p4d2_payment_application_acl_privilege
                    CHECK (
                        privilege_name IN (
                            'SELECT',
                            'INSERT',
                            'UPDATE'
                        )
                    )
                )
            """
        )
    )

    bind.execute(
        sa.text(
            """
            REVOKE ALL ON TABLE
                app_private.p4d2_payment_application_acl_delta
            FROM PUBLIC
            """
        )
    )

    bind.execute(
        sa.text(
            """
            CREATE TABLE
                app_private.p4d2_payment_application_schema_acl_delta (
                    privilege_name text PRIMARY KEY,
                    CONSTRAINT
                        chk_p4d2_payment_application_schema_acl_delta
                    CHECK (
                        privilege_name = 'USAGE'
                    )
                )
            """
        )
    )

    bind.execute(
        sa.text(
            """
            REVOKE ALL ON TABLE
                app_private.p4d2_payment_application_schema_acl_delta
            FROM PUBLIC
            """
        )
    )

    bind.execute(
        sa.text(
            """
            CREATE TABLE
                app_private.p4d2_payment_application_evidence (
                    allocation_id uuid PRIMARY KEY,
                    created_at timestamptz NOT NULL
                        DEFAULT pg_catalog.clock_timestamp()
                )
            """
        )
    )

    bind.execute(
        sa.text(
            """
            REVOKE ALL ON TABLE
                app_private.p4d2_payment_application_evidence
            FROM PUBLIC
            """
        )
    )

    has_app_private_usage = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.has_schema_privilege(
                'app_security_owner',
                'app_private',
                'USAGE'
            )
            """
        )
    ).scalar_one()

    if not has_app_private_usage:
        bind.execute(
            sa.text(
                """
                GRANT USAGE
                ON SCHEMA app_private
                TO app_security_owner
                """
            )
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO
                    app_private.p4d2_payment_application_schema_acl_delta(
                        privilege_name
                    )
                VALUES ('USAGE')
                """
            )
        )

    bind.execute(
        sa.text(
            """
            GRANT INSERT (allocation_id)
            ON TABLE
                app_private.p4d2_payment_application_evidence
            TO app_security_owner
            """
        )
    )

    for table_name, privilege_name, columns in (
        _P4D2_PAYMENT_APPLICATION_COLUMN_PRIVILEGES
    ):
        for column_name in columns:
            _p4d2_grant_payment_application_column_if_missing(
                bind,
                table_name=table_name,
                column_name=column_name,
                privilege_name=privilege_name,
            )

    op.execute("SET LOCAL ROLE app_security_owner")

    bind.execute(
        sa.text(
            r"""
            CREATE FUNCTION
                app_secure.apply_finance_confirmed_payment(
                    p_payment_id uuid,
                    p_invoice_id uuid,
                    p_amount numeric,
                    p_currency_code text,
                    p_idempotency_key text,
                    p_request_hash_sha256 text
                )
            RETURNS TABLE(
                allocation_id uuid,
                payment_id uuid,
                invoice_id uuid,
                invoice_status text,
                allocated_amount numeric,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_existing_org_id uuid;
                v_payment finance.payments%ROWTYPE;
                v_invoice finance.invoices%ROWTYPE;

                v_amount numeric(14,2);
                v_amount_text text;
                v_apply_canonical text;
                v_apply_hash text;

                v_apply_idem record;
                v_existing_allocation
                    finance.payment_allocations%ROWTYPE;
                v_allocation
                    finance.payment_allocations%ROWTYPE;

                v_payment_allocated numeric(14,2);
                v_invoice_allocated numeric(14,2);
                v_payment_available numeric(14,2);
                v_invoice_outstanding numeric(14,2);
                v_new_invoice_outstanding numeric(14,2);

                v_ledger_idem record;
                v_ledger_entry_id uuid;

                v_clearing_account_id uuid;
                v_ar_account_id uuid;

                v_ledger_division_id uuid;
                v_ledger_brand_id uuid;
                v_ledger_canonical text;
                v_ledger_hash text;

                v_allocation_payload jsonb;
                v_allocation_canonical text;
                v_allocation_hash text;

                v_invoice_payload jsonb;
                v_invoice_canonical text;
                v_invoice_hash text;

                v_ledger_outbox_payload jsonb;
                v_ledger_outbox_canonical text;
                v_ledger_outbox_hash text;
            BEGIN
                IF NOT pg_catalog.pg_has_role(
                    session_user,
                    'app_runtime',
                    'MEMBER'
                ) THEN
                    RAISE EXCEPTION
                        'P4D finance payment application requires app_runtime'
                        USING ERRCODE='42501';
                END IF;

                IF p_payment_id IS NULL
                   OR p_invoice_id IS NULL
                   OR p_amount IS NULL
                   OR p_currency_code IS NULL
                   OR pg_catalog.btrim(p_currency_code) = ''
                   OR p_idempotency_key IS NULL
                   OR pg_catalog.btrim(p_idempotency_key) = ''
                   OR pg_catalog.length(p_idempotency_key) > 200
                   OR p_request_hash_sha256 IS NULL
                   OR p_request_hash_sha256
                        !~ '^[0-9a-f]{64}$'
                THEN
                    RAISE EXCEPTION
                        'P4D finance payment application request invalid'
                        USING ERRCODE='22023';
                END IF;

                v_amount := pg_catalog.round(p_amount, 2);

                IF v_amount <= 0 THEN
                    RAISE EXCEPTION
                        'P4D finance payment application amount invalid'
                        USING ERRCODE='22023';
                END IF;

                BEGIN
                    v_existing_org_id :=
                        NULLIF(
                            pg_catalog.current_setting(
                                'app.current_org_id',
                                true
                            ),
                            ''
                        )::uuid;
                EXCEPTION
                    WHEN invalid_text_representation THEN
                        RAISE EXCEPTION
                            'P4D finance payment application tenant invalid'
                            USING ERRCODE='22023';
                END;

                SELECT p.*
                INTO v_payment
                FROM finance.payments p
                WHERE p.id = p_payment_id
                FOR UPDATE;

                IF NOT FOUND
                   OR v_payment.organization_id IS NULL
                THEN
                    RAISE EXCEPTION
                        'P4D finance payment application payment unavailable'
                        USING ERRCODE='42501';
                END IF;

                IF v_existing_org_id IS NOT NULL
                   AND v_existing_org_id
                        IS DISTINCT FROM
                        v_payment.organization_id
                THEN
                    RAISE EXCEPTION
                        'P4D finance payment application tenant unavailable'
                        USING ERRCODE='42501';
                END IF;

                PERFORM pg_catalog.set_config(
                    'app.current_org_id',
                    v_payment.organization_id::text,
                    true
                );

                SELECT i.*
                INTO v_invoice
                FROM finance.invoices i
                WHERE i.id = p_invoice_id
                FOR UPDATE;

                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'P4D finance payment application invoice unavailable'
                        USING ERRCODE='42501';
                END IF;

                IF v_payment.status
                    NOT IN ('captured', 'settled')
                THEN
                    RAISE EXCEPTION
                        'P4D finance payment application payment state invalid'
                        USING ERRCODE='23514';
                END IF;

                IF pg_catalog.upper(
                        pg_catalog.btrim(p_currency_code)
                   )
                   IS DISTINCT FROM
                   pg_catalog.btrim(v_payment.currency_code::text)
                   OR
                   pg_catalog.upper(
                        pg_catalog.btrim(p_currency_code)
                   )
                   IS DISTINCT FROM
                   pg_catalog.btrim(v_invoice.currency_code::text)
                THEN
                    RAISE EXCEPTION
                        'P4D finance payment application currency invalid'
                        USING ERRCODE='23514';
                END IF;

                IF v_amount > v_payment.amount
                   OR v_amount > v_invoice.grand_total_amount
                THEN
                    RAISE EXCEPTION
                        'P4D finance payment application amount exceeds authority'
                        USING ERRCODE='23514';
                END IF;

                IF v_payment.organization_id
                        IS DISTINCT FROM v_invoice.organization_id
                   OR v_payment.legal_entity_id
                        IS DISTINCT FROM v_invoice.legal_entity_id
                   OR v_payment.gst_registration_id
                        IS DISTINCT FROM v_invoice.gst_registration_id
                   OR v_payment.division_id
                        IS DISTINCT FROM v_invoice.division_id
                   OR v_payment.brand_id
                        IS DISTINCT FROM v_invoice.brand_id
                   OR v_payment.amount
                        > v_invoice.grand_total_amount
                THEN
                    RAISE EXCEPTION
                        'P4D finance payment application relationship invalid'
                        USING ERRCODE='23514';
                END IF;

                v_amount_text :=
                    pg_catalog.to_char(
                        v_amount,
                        'FM999999999999999999999999990.00'
                    );

                v_apply_canonical :=
                    '{"amount":"'
                    || v_amount_text
                    || '","invoice_id":"'
                    || p_invoice_id::text
                    || '","payment_id":"'
                    || p_payment_id::text
                    || '"}';

                v_apply_hash :=
                    pg_catalog.encode(
                        pg_catalog.sha256(
                            pg_catalog.convert_to(
                                v_apply_canonical,
                                'UTF8'
                            )),
                        'hex'
                    );

                IF v_apply_hash
                    IS DISTINCT FROM p_request_hash_sha256
                THEN
                    RAISE EXCEPTION
                        'P4D finance payment application request hash invalid'
                        USING ERRCODE='23514';
                END IF;

                SELECT *
                INTO v_apply_idem
                FROM app_secure.reserve_finance_idempotency(
                    'finance.payment.apply',
                    p_idempotency_key,
                    v_apply_hash,
                    v_payment.organization_id,
                    pg_catalog.clock_timestamp()
                        + interval '7 days'
                );

                IF NOT v_apply_idem.inserted THEN
                    IF v_apply_idem.response_ref IS NULL THEN
                        RAISE EXCEPTION
                            'P4D finance payment application already processing'
                            USING ERRCODE='40001';
                    END IF;

                    BEGIN
                        SELECT pa.*
                        INTO v_existing_allocation
                        FROM finance.payment_allocations pa
                        WHERE pa.id =
                            v_apply_idem.response_ref::uuid;
                    EXCEPTION
                        WHEN invalid_text_representation THEN
                            RAISE EXCEPTION
                                'P4D finance payment application replay unavailable'
                                USING ERRCODE='23514';
                    END;

                    IF NOT FOUND
                       OR v_existing_allocation.payment_id
                            IS DISTINCT FROM p_payment_id
                       OR v_existing_allocation.invoice_id
                            IS DISTINCT FROM p_invoice_id
                    THEN
                        RAISE EXCEPTION
                            'P4D finance payment application replay unavailable'
                            USING ERRCODE='23514';
                    END IF;

                    RETURN QUERY
                    SELECT
                        v_existing_allocation.id,
                        v_existing_allocation.payment_id,
                        v_existing_allocation.invoice_id,
                        v_invoice.status::text,
                        v_existing_allocation.allocated_amount,
                        true;

                    RETURN;
                END IF;

                IF v_invoice.status
                    NOT IN ('issued', 'partially_paid')
                THEN
                    RAISE EXCEPTION
                        'P4D finance payment application invoice state invalid'
                        USING ERRCODE='23514';
                END IF;

                SELECT pa.*
                INTO v_existing_allocation
                FROM finance.payment_allocations pa
                WHERE pa.payment_id = p_payment_id
                  AND pa.invoice_id = p_invoice_id;

                IF FOUND THEN
                    RAISE EXCEPTION
                        'P4D finance payment application already allocated'
                        USING ERRCODE='23505';
                END IF;

                SELECT
                    COALESCE(
                        pg_catalog.sum(pa.allocated_amount),
                        0
                    )
                INTO v_payment_allocated
                FROM finance.payment_allocations pa
                WHERE pa.payment_id = p_payment_id;

                SELECT
                    COALESCE(
                        pg_catalog.sum(pa.allocated_amount),
                        0
                    )
                INTO v_invoice_allocated
                FROM finance.payment_allocations pa
                WHERE pa.invoice_id = p_invoice_id;

                v_payment_available :=
                    v_payment.amount - v_payment_allocated;

                v_invoice_outstanding :=
                    v_invoice.grand_total_amount
                    - v_invoice_allocated;

                IF v_amount > v_payment_available THEN
                    RAISE EXCEPTION
                        'P4D finance payment application exceeds payment balance'
                        USING ERRCODE='23514';
                END IF;

                IF v_amount > v_invoice_outstanding THEN
                    RAISE EXCEPTION
                        'P4D finance payment application exceeds invoice balance'
                        USING ERRCODE='23514';
                END IF;

                INSERT INTO finance.payment_allocations(
                    payment_id,
                    invoice_id,
                    allocated_amount
                )
                VALUES (
                    p_payment_id,
                    p_invoice_id,
                    v_amount
                )
                RETURNING *
                INTO v_allocation;

                v_new_invoice_outstanding :=
                    v_invoice_outstanding - v_amount;

                UPDATE finance.invoices
                SET status = CASE
                    WHEN v_new_invoice_outstanding = 0
                    THEN 'paid'
                    ELSE 'partially_paid'
                END
                WHERE id = p_invoice_id
                RETURNING status
                INTO v_invoice.status;

                v_ledger_division_id :=
                    COALESCE(
                        v_payment.division_id,
                        v_invoice.division_id
                    );

                v_ledger_brand_id :=
                    COALESCE(
                        v_payment.brand_id,
                        v_invoice.brand_id
                    );

                v_ledger_canonical :=
                    '{"brand_id":'
                    || CASE
                        WHEN v_ledger_brand_id IS NULL
                        THEN 'null'
                        ELSE
                            '"'
                            || v_ledger_brand_id::text
                            || '"'
                       END
                    || ',"division_id":'
                    || CASE
                        WHEN v_ledger_division_id IS NULL
                        THEN 'null'
                        ELSE
                            '"'
                            || v_ledger_division_id::text
                            || '"'
                       END
                    || ',"entry_type":"payment"'
                    || ',"legal_entity_id":"'
                    || v_payment.legal_entity_id::text
                    || '"'
                    || ',"lines":['
                    || '{"account_code":"PAYMENT_CLEARING",'
                    || '"credit_amount":"0.00",'
                    || '"debit_amount":"'
                    || v_amount_text
                    || '","memo":"Payment clearing"},'
                    || '{"account_code":"AR",'
                    || '"credit_amount":"'
                    || v_amount_text
                    || '","debit_amount":"0.00",'
                    || '"memo":"Receivable settled"}]'
                    || ',"source_id":"'
                    || v_allocation.id::text
                    || '"'
                    || ',"source_type":"payment_allocation"}';

                v_ledger_hash :=
                    pg_catalog.encode(
                        pg_catalog.sha256(
                            pg_catalog.convert_to(
                                v_ledger_canonical,
                                'UTF8'
                            )),
                        'hex'
                    );

                SELECT *
                INTO v_ledger_idem
                FROM app_secure.reserve_finance_idempotency(
                    'finance.ledger.post',
                    p_idempotency_key || pg_catalog.chr(58) || 'ledger',
                    v_ledger_hash,
                    v_payment.organization_id,
                    pg_catalog.clock_timestamp()
                        + interval '7 days'
                );

                IF NOT v_ledger_idem.inserted THEN
                    IF v_ledger_idem.response_ref IS NULL THEN
                        RAISE EXCEPTION
                            'P4D finance payment application ledger already processing'
                            USING ERRCODE='40001';
                    END IF;

                    BEGIN
                        v_ledger_entry_id :=
                            v_ledger_idem.response_ref::uuid;
                    EXCEPTION
                        WHEN invalid_text_representation THEN
                            RAISE EXCEPTION
                                'P4D finance payment application ledger replay unavailable'
                                USING ERRCODE='23514';
                    END;

                    PERFORM 1
                    FROM finance.ledger_entries le
                    WHERE le.id = v_ledger_entry_id
                      AND le.source_type = 'payment_allocation'
                      AND le.source_id = v_allocation.id
                      AND le.status = 'posted';

                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'P4D finance payment application ledger replay unavailable'
                            USING ERRCODE='23514';
                    END IF;
                ELSE
                    SELECT la.id
                    INTO v_clearing_account_id
                    FROM finance.ledger_accounts la
                    WHERE la.legal_entity_id =
                            v_payment.legal_entity_id
                      AND la.code = 'PAYMENT_CLEARING';

                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'P4D finance payment application clearing account unavailable'
                            USING ERRCODE='23514';
                    END IF;

                    SELECT la.id
                    INTO v_ar_account_id
                    FROM finance.ledger_accounts la
                    WHERE la.legal_entity_id =
                            v_payment.legal_entity_id
                      AND la.code = 'AR';

                    IF NOT FOUND THEN
                        RAISE EXCEPTION
                            'P4D finance payment application AR account unavailable'
                            USING ERRCODE='23514';
                    END IF;

                    INSERT INTO finance.ledger_entries(
                        legal_entity_id,
                        division_id,
                        brand_id,
                        entry_type,
                        source_type,
                        source_id,
                        status,
                        posted_at
                    )
                    VALUES (
                        v_payment.legal_entity_id,
                        v_ledger_division_id,
                        v_ledger_brand_id,
                        'payment',
                        'payment_allocation',
                        v_allocation.id,
                        'posted',
                        pg_catalog.clock_timestamp()
                    )
                    RETURNING id
                    INTO v_ledger_entry_id;

                    INSERT INTO finance.ledger_entry_lines(
                        ledger_entry_id,
                        ledger_account_id,
                        debit_amount,
                        credit_amount,
                        memo
                    )
                    VALUES
                    (
                        v_ledger_entry_id,
                        v_clearing_account_id,
                        v_amount,
                        0,
                        'Payment clearing'
                    ),
                    (
                        v_ledger_entry_id,
                        v_ar_account_id,
                        0,
                        v_amount,
                        'Receivable settled'
                    );

                    PERFORM 1
                    FROM app_secure.complete_finance_idempotency(
                        v_ledger_idem.id,
                        v_ledger_entry_id::text
                    );
                END IF;

                v_allocation_payload :=
                    pg_catalog.jsonb_build_object(
                        'allocation_id',
                        v_allocation.id::text,
                        'payment_id',
                        p_payment_id::text,
                        'invoice_id',
                        p_invoice_id::text,
                        'allocated_amount',
                        v_amount_text
                    );

                v_allocation_canonical :=
                    '{"allocated_amount":"'
                    || v_amount_text
                    || '","allocation_id":"'
                    || v_allocation.id::text
                    || '","invoice_id":"'
                    || p_invoice_id::text
                    || '","payment_id":"'
                    || p_payment_id::text
                    || '"}';

                v_allocation_hash :=
                    pg_catalog.encode(
                        pg_catalog.sha256(
                            pg_catalog.convert_to(
                                v_allocation_canonical,
                                'UTF8'
                            )),
                        'hex'
                    );

                INSERT INTO finance.outbox_events(
                    organization_id,
                    legal_entity_id,
                    division_id,
                    brand_id,
                    aggregate_type,
                    aggregate_id,
                    event_type,
                    idempotency_key,
                    payload_json,
                    payload_sha256,
                    status
                )
                VALUES (
                    v_payment.organization_id,
                    v_payment.legal_entity_id,
                    v_payment.division_id,
                    v_payment.brand_id,
                    'payment_allocation',
                    v_allocation.id,
                    'finance.payment.applied',
                    p_idempotency_key,
                    v_allocation_payload,
                    v_allocation_hash,
                    'pending'
                );

                v_invoice_payload :=
                    pg_catalog.jsonb_build_object(
                        'invoice_id',
                        p_invoice_id::text,
                        'status',
                        v_invoice.status::text
                    );

                v_invoice_canonical :=
                    '{"invoice_id":"'
                    || p_invoice_id::text
                    || '","status":"'
                    || v_invoice.status::text
                    || '"}';

                v_invoice_hash :=
                    pg_catalog.encode(
                        pg_catalog.sha256(
                            pg_catalog.convert_to(
                                v_invoice_canonical,
                                'UTF8'
                            )),
                        'hex'
                    );

                INSERT INTO finance.outbox_events(
                    organization_id,
                    legal_entity_id,
                    division_id,
                    brand_id,
                    aggregate_type,
                    aggregate_id,
                    event_type,
                    idempotency_key,
                    payload_json,
                    payload_sha256,
                    status
                )
                VALUES (
                    v_invoice.organization_id,
                    v_invoice.legal_entity_id,
                    v_invoice.division_id,
                    v_invoice.brand_id,
                    'invoice',
                    p_invoice_id,
                    CASE
                        WHEN v_invoice.status = 'paid'
                        THEN 'finance.invoice.paid'
                        ELSE 'finance.invoice.partially_paid'
                    END,
                    p_idempotency_key,
                    v_invoice_payload,
                    v_invoice_hash,
                    'pending'
                );

                v_ledger_outbox_payload :=
                    pg_catalog.jsonb_build_object(
                        'ledger_entry_id',
                        v_ledger_entry_id::text,
                        'source_type',
                        'payment_allocation'
                    );

                v_ledger_outbox_canonical :=
                    '{"ledger_entry_id":"'
                    || v_ledger_entry_id::text
                    || '","source_type":"payment_allocation"}';

                v_ledger_outbox_hash :=
                    pg_catalog.encode(
                        pg_catalog.sha256(
                            pg_catalog.convert_to(
                                v_ledger_outbox_canonical,
                                'UTF8'
                            )),
                        'hex'
                    );

                INSERT INTO finance.outbox_events(
                    organization_id,
                    legal_entity_id,
                    division_id,
                    brand_id,
                    aggregate_type,
                    aggregate_id,
                    event_type,
                    idempotency_key,
                    payload_json,
                    payload_sha256,
                    status
                )
                VALUES (
                    v_payment.organization_id,
                    v_payment.legal_entity_id,
                    v_payment.division_id,
                    v_payment.brand_id,
                    'ledger_entry',
                    v_ledger_entry_id,
                    'finance.ledger.entry.posted',
                    p_idempotency_key || pg_catalog.chr(58) || 'ledger',
                    v_ledger_outbox_payload,
                    v_ledger_outbox_hash,
                    'pending'
                );

                INSERT INTO
                    app_private.p4d2_payment_application_evidence(
                        allocation_id
                    )
                VALUES (
                    v_allocation.id
                );

                PERFORM 1
                FROM app_secure.complete_finance_idempotency(
                    v_apply_idem.id,
                    v_allocation.id::text
                );

                RETURN QUERY
                SELECT
                    v_allocation.id,
                    p_payment_id,
                    p_invoice_id,
                    v_invoice.status::text,
                    v_allocation.allocated_amount,
                    false;
            END;
            $function$
            """
        )
    )

    bind.execute(
        sa.text(
            """
            REVOKE ALL ON FUNCTION
                app_secure.apply_finance_confirmed_payment(
                    uuid,uuid,numeric,text,text,text
                )
            FROM PUBLIC
            """
        )
    )

    bind.execute(
        sa.text(
            """
            GRANT EXECUTE ON FUNCTION
                app_secure.apply_finance_confirmed_payment(
                    uuid,uuid,numeric,text,text,text
                )
            TO app_runtime
            """
        )
    )

    bind.execute(
        sa.text(
            """
            ALTER FUNCTION
                app_secure.apply_finance_confirmed_payment(
                    uuid,uuid,numeric,text,text,text
                )
            OWNER TO app_security_owner
            """
        )
    )

    op.execute("RESET ROLE")

    _p4d2_require_payment_application_authority(bind)


def _p4d2_remove_payment_application_authority(bind) -> None:
    _require_identity_contract(bind)

    evidence_exists = bind.execute(
        sa.text(
            """
            SELECT pg_catalog.to_regclass(
                'app_private.p4d2_payment_application_evidence'
            ) IS NOT NULL
            """
        )
    ).scalar_one()

    if evidence_exists:
        has_evidence = bind.execute(
            sa.text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM
                        app_private.p4d2_payment_application_evidence
                    LIMIT 1
                )
                """
            )
        ).scalar_one()

        if has_evidence:
            raise RuntimeError(
                "zd07 downgrade blocked: payment application "
                "authority/evidence exists"
            )

    op.execute("SET LOCAL ROLE app_security_owner")

    bind.execute(
        sa.text(
            """
            DROP FUNCTION IF EXISTS
                app_secure.apply_finance_confirmed_payment(
                    uuid,uuid,numeric,text,text,text
                )
            """
        )
    )

    op.execute("RESET ROLE")

    _require_exact_function_count(
        bind,
        schema_name="app_secure",
        function_name="apply_finance_confirmed_payment",
        normalized_args="uuid,uuid,numeric,text,text,text",
        expected_count=0,
        error_label=(
            "zd07 downgrade failed to remove payment "
            "application capability"
        ),
    )

    if bind.execute(
        sa.text(
            """
            SELECT pg_catalog.to_regclass(
                'app_private.p4d2_payment_application_evidence'
            ) IS NOT NULL
            """
        )
    ).scalar_one():
        bind.execute(
            sa.text(
                """
                REVOKE INSERT (allocation_id)
                ON TABLE
                    app_private.p4d2_payment_application_evidence
                FROM app_security_owner
                """
            )
        )

        bind.execute(
            sa.text(
                """
                DROP TABLE
                    app_private.p4d2_payment_application_evidence
                """
            )
        )

    if bind.execute(
        sa.text(
            """
            SELECT pg_catalog.to_regclass(
                'app_private.p4d2_payment_application_acl_delta'
            ) IS NOT NULL
            """
        )
    ).scalar_one():
        rows = bind.execute(
            sa.text(
                """
                SELECT
                    table_name,
                    column_name,
                    privilege_name
                FROM
                    app_private.p4d2_payment_application_acl_delta
                ORDER BY
                    table_name DESC,
                    privilege_name DESC,
                    column_name DESC
                """
            )
        ).mappings().all()

        for row in rows:
            bind.execute(
                sa.text(
                    f"REVOKE {row['privilege_name']} "
                    f"({row['column_name']}) "
                    f"ON TABLE finance.{row['table_name']} "
                    "FROM app_security_owner"
                )
            )

        bind.execute(
            sa.text(
                """
                DROP TABLE
                    app_private.p4d2_payment_application_acl_delta
                """
            )
        )

    if bind.execute(
        sa.text(
            """
            SELECT pg_catalog.to_regclass(
                'app_private.'
                'p4d2_payment_application_schema_acl_delta'
            ) IS NOT NULL
            """
        )
    ).scalar_one():
        schema_usage_was_added = bind.execute(
            sa.text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM app_private.
                        p4d2_payment_application_schema_acl_delta
                    WHERE privilege_name = 'USAGE'
                )
                """
            )
        ).scalar_one()

        bind.execute(
            sa.text(
                """
                DROP TABLE
                    app_private.
                    p4d2_payment_application_schema_acl_delta
                """
            )
        )

        if schema_usage_was_added:
            bind.execute(
                sa.text(
                    """
                    REVOKE USAGE
                    ON SCHEMA app_private
                    FROM app_security_owner
                    """
                )
            )

def upgrade() -> None:
    bind = op.get_bind()
    _require_identity_contract(bind)
    _require_predecessor(bind)
    _install_function(bind)
    _post_install_proof(bind)

    _p4d2_install_invoice_issue_authority(op.get_bind())
    _p4d2_install_checkout_callback_authority(op.get_bind())
    _p4d2_install_provider_evidence_authority(bind)
    _p4d2_install_payment_application_authority(bind)


def downgrade() -> None:
    bind = op.get_bind()

    _p4d2_remove_payment_application_authority(bind)
    _p4d2_remove_checkout_callback_authority(bind)
    _p4d2_downgrade_provider_evidence_authority(bind)
    _p4d2_remove_invoice_issue_authority(bind)

    _require_identity_contract(bind)
    has_binding_evidence = False
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass('finance.refund_obligation_bindings') IS NOT NULL")
    ).scalar_one():
        op.execute("SET LOCAL ROLE app_security_owner")
        try:
            has_binding_evidence = bind.execute(
                sa.text("SELECT EXISTS(SELECT 1 FROM finance.refund_obligation_bindings LIMIT 1)")
            ).scalar_one()
        finally:
            op.execute("RESET ROLE")
    if has_binding_evidence:
        raise RuntimeError(
            "zd07 downgrade blocked: refund obligation binding authority/evidence exists"
        )
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass('finance.refund_execution_commands') IS NOT NULL")
    ).scalar_one():
        op.execute("SET LOCAL ROLE app_security_owner")
        try:
            has_refund_execution_evidence = bind.execute(
                sa.text("SELECT EXISTS(SELECT 1 FROM finance.refund_execution_commands LIMIT 1)")
            ).scalar_one()
        finally:
            op.execute("RESET ROLE")
        if has_refund_execution_evidence:
            raise RuntimeError(
                "zd07 downgrade blocked: refund execution authority/evidence exists"
            )
    if bind.execute(
        sa.text("SELECT pg_catalog.to_regclass('finance.refunds') IS NOT NULL")
    ).scalar_one():
        op.execute("SET LOCAL ROLE app_security_owner")
        try:
            has_refund_intent_evidence = bind.execute(
                sa.text(
                    """
                    SELECT EXISTS(
                        SELECT 1
                        FROM finance.refunds
                        WHERE reason_code ~ '^branch-refund:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        LIMIT 1
                    )
                    """
                )
            ).scalar_one()
        finally:
            op.execute("RESET ROLE")
        if has_refund_intent_evidence:
            raise RuntimeError(
                "zd07 downgrade blocked: P4D-2 Finance refund intent evidence exists"
            )
    if bind.execute(sa.text("SELECT pg_catalog.to_regclass('finance.member_subscription_checkout_bindings') IS NOT NULL")).scalar_one():
        op.execute("SET LOCAL ROLE app_security_owner")
        try:
            has_checkout_binding_evidence = bind.execute(
                sa.text("SELECT EXISTS(SELECT 1 FROM finance.member_subscription_checkout_bindings LIMIT 1)")
            ).scalar_one()
        finally:
            op.execute("RESET ROLE")
        if has_checkout_binding_evidence:
            raise RuntimeError("zd07 downgrade blocked: member subscription checkout binding authority/evidence exists")
    if bind.execute(sa.text("SELECT pg_catalog.to_regclass('finance.branch_accounting_profiles') IS NOT NULL")).scalar_one():
        op.execute("SET LOCAL ROLE app_security_owner")
        try:
            has_branch_profile_evidence = bind.execute(
                sa.text("SELECT EXISTS(SELECT 1 FROM finance.branch_accounting_profiles LIMIT 1)")
            ).scalar_one()
        finally:
            op.execute("RESET ROLE")
        if has_branch_profile_evidence:
            raise RuntimeError("zd07 downgrade blocked: branch accounting profile authority/evidence exists")
    if bind.execute(sa.text("SELECT pg_catalog.to_regclass('finance.membership_plan_tax_profiles') IS NOT NULL")).scalar_one():
        op.execute("SET LOCAL ROLE app_security_owner")
        try:
            has_tax_profile_evidence = bind.execute(
                sa.text("SELECT EXISTS(SELECT 1 FROM finance.membership_plan_tax_profiles LIMIT 1)")
            ).scalar_one()
        finally:
            op.execute("RESET ROLE")
        if has_tax_profile_evidence:
            raise RuntimeError("zd07 downgrade blocked: membership plan tax profile authority/evidence exists")
    if bind.execute(sa.text("SELECT pg_catalog.to_regclass('finance.billing_parties') IS NOT NULL")).scalar_one():
        op.execute("SET LOCAL ROLE app_security_owner")
        try:
            has_member_buyer_evidence = bind.execute(
                sa.text("SELECT EXISTS(SELECT 1 FROM finance.billing_parties WHERE buyer_kind = 'member' LIMIT 1)")
            ).scalar_one()
        finally:
            op.execute("RESET ROLE")
        if has_member_buyer_evidence:
            raise RuntimeError("zd07 downgrade blocked: member billing-party authority/evidence exists")
    op.execute("SET LOCAL ROLE app_security_owner")
    op.execute("DROP FUNCTION IF EXISTS app_secure.resolve_finance_invoice_result(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.persist_finance_draft_invoice(uuid,uuid,uuid,uuid,uuid,uuid,uuid,date,text,text,text,text,text,text,text,text,text,text,text,text,text,text,numeric,numeric,numeric,numeric,numeric,jsonb)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.complete_finance_idempotency(uuid,text)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.reserve_finance_idempotency(text,text,text,uuid,timestamp with time zone)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.resolve_finance_invoice_accounting_master_data(uuid,uuid,uuid,uuid,uuid,uuid,date)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.resolve_finance_billing_party_organization(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.resolve_finance_invoice_organization(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.attach_member_subscription_checkout_provider_order(uuid,uuid,text)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.record_member_subscription_checkout_binding(uuid,uuid,uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.establish_membership_plan_tax_profile(uuid,uuid,uuid,text,date,date)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.establish_branch_accounting_profile(uuid,uuid,uuid,uuid,uuid,uuid,date,date)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.establish_finance_tax_code(uuid,text,text,text,text,integer)")
    op.execute("REVOKE USAGE ON SCHEMA app_secure FROM finance_config_runtime")
    op.execute("DROP FUNCTION IF EXISTS app_secure.resolve_member_subscription_checkout_inputs(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.upsert_member_billing_party(uuid,text,text)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.record_refund_obligation_binding(uuid,uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_secure.resolve_branch_refund_required(uuid,uuid)")
    op.execute("RESET ROLE")
    op.execute("REVOKE SELECT, INSERT ON TABLE finance.member_subscription_checkout_bindings FROM app_security_owner")
    op.execute("DROP POLICY IF EXISTS p4d_member_subscription_checkout_bindings_security_owner_insert ON finance.member_subscription_checkout_bindings")
    op.execute("DROP POLICY IF EXISTS p4d_member_subscription_checkout_bindings_security_owner_select ON finance.member_subscription_checkout_bindings")
    op.execute("DROP TABLE IF EXISTS finance.member_subscription_checkout_bindings")
    op.execute("REVOKE SELECT ON TABLE finance.membership_plan_tax_profiles FROM app_security_owner")
    op.execute("DROP POLICY IF EXISTS p4d_membership_plan_tax_profiles_security_owner_insert ON finance.membership_plan_tax_profiles")
    op.execute("DROP POLICY IF EXISTS p4d_membership_plan_tax_profiles_security_owner_select ON finance.membership_plan_tax_profiles")
    op.execute("ALTER TABLE finance.membership_plan_tax_profiles DROP CONSTRAINT IF EXISTS ex_membership_plan_tax_profiles_active_window")
    op.execute("DROP TABLE IF EXISTS finance.membership_plan_tax_profiles")
    op.execute("DROP FUNCTION IF EXISTS finance.prevent_membership_plan_tax_profile_overlap()")
    op.execute("REVOKE SELECT ON TABLE finance.branch_accounting_profiles FROM app_security_owner")
    op.execute("DROP POLICY IF EXISTS p4d_branch_accounting_profiles_security_owner_insert ON finance.branch_accounting_profiles")
    op.execute("DROP POLICY IF EXISTS p4d_branch_accounting_profiles_security_owner_select ON finance.branch_accounting_profiles")
    op.execute("ALTER TABLE finance.branch_accounting_profiles DROP CONSTRAINT IF EXISTS ex_branch_accounting_profiles_active_window")
    op.execute("DROP TABLE IF EXISTS finance.branch_accounting_profiles")
    op.execute("DROP FUNCTION IF EXISTS finance.prevent_branch_accounting_profile_invalidity()")
    op.execute("DROP INDEX IF EXISTS finance.uq_finance_billing_parties_member_buyer")
    op.execute("DROP INDEX IF EXISTS finance.uq_finance_billing_parties_org_buyer")
    op.execute("ALTER TABLE finance.billing_parties DROP CONSTRAINT IF EXISTS fk_finance_billing_parties_member_org RESTRICT")
    op.execute("ALTER TABLE finance.billing_parties DROP CONSTRAINT IF EXISTS chk_finance_billing_parties_buyer_shape")
    op.execute("ALTER TABLE finance.billing_parties DROP CONSTRAINT IF EXISTS chk_finance_billing_parties_buyer_kind")
    op.execute("ALTER TABLE finance.billing_parties DROP COLUMN IF EXISTS member_id")
    op.execute("ALTER TABLE finance.billing_parties DROP COLUMN IF EXISTS buyer_kind")
    op.execute("ALTER TABLE finance.billing_parties ADD CONSTRAINT uq_finance_billing_parties_organization UNIQUE (organization_id) DEFERRABLE INITIALLY DEFERRED")
    op.execute("REVOKE INSERT ON TABLE finance.invoice_lines FROM app_security_owner")
    op.execute("REVOKE INSERT ON TABLE finance.invoices FROM app_security_owner")
    op.execute("REVOKE SELECT (id, organization_id, status, official_invoice_number, brand_reference) ON TABLE finance.invoices FROM app_security_owner")
    op.execute("REVOKE SELECT, INSERT, UPDATE ON TABLE finance.billing_parties FROM app_security_owner")
    op.execute("REVOKE SELECT ON TABLE finance.legal_entities FROM app_security_owner")
    op.execute("REVOKE SELECT ON TABLE finance.gst_registrations FROM app_security_owner")
    op.execute("REVOKE SELECT ON TABLE finance.divisions FROM app_security_owner")
    op.execute("REVOKE SELECT ON TABLE finance.brands FROM app_security_owner")
    op.execute("DROP POLICY IF EXISTS p4d_tax_codes_security_owner_insert ON finance.tax_codes")
    op.execute("DROP POLICY IF EXISTS p4d_tax_codes_security_owner_select ON finance.tax_codes")
    op.execute("ALTER TABLE finance.tax_codes NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE finance.tax_codes DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE SELECT, INSERT ON TABLE finance.tax_codes FROM app_security_owner")
    op.execute("REVOKE SELECT (is_active) ON TABLE public.organizations FROM app_security_owner")
    op.execute("REVOKE SELECT ON TABLE public.members FROM app_security_owner")
    op.execute("REVOKE SELECT ON TABLE public.membership_plans FROM app_security_owner")
    op.execute("REVOKE SELECT, INSERT ON TABLE finance.refund_obligation_bindings FROM app_security_owner")
    op.execute("DROP POLICY IF EXISTS p4d_refund_obligation_security_owner_insert ON finance.refund_obligation_bindings")
    op.execute("DROP POLICY IF EXISTS p4d_refund_obligation_security_owner_select ON finance.refund_obligation_bindings")
    op.execute("DROP TABLE IF EXISTS finance.refund_obligation_bindings")
    op.execute("DROP POLICY IF EXISTS p4d_member_subscriptions_v2_security_owner_select ON public.member_subscriptions_v2")
    op.execute("REVOKE SELECT ON TABLE public.member_subscriptions_v2 FROM app_security_owner")
    op.execute("DROP POLICY IF EXISTS p4d_membership_plans_security_owner_select ON public.membership_plans")
    if bind.execute(sa.text("SELECT pg_catalog.to_regclass('finance.idempotency_keys') IS NOT NULL")).scalar_one():
        duplicate_idempotency_keys = bind.execute(sa.text("""
            SELECT EXISTS (
                SELECT 1
                FROM finance.idempotency_keys
                GROUP BY scope, idempotency_key
                HAVING count(*) > 1
            )
        """)).scalar_one()
        if duplicate_idempotency_keys:
            raise RuntimeError("zd07 downgrade blocked: tenant-scoped finance idempotency evidence cannot restore global uniqueness")
        op.execute("ALTER TABLE finance.idempotency_keys DROP CONSTRAINT IF EXISTS uq_finance_idempotency_keys_scope_key")
        op.execute("ALTER TABLE finance.idempotency_keys ADD CONSTRAINT uq_finance_idempotency_keys_scope_key UNIQUE (scope, idempotency_key)")
    op.execute("ALTER TABLE finance.payments DROP CONSTRAINT IF EXISTS uq_finance_payments_id_org RESTRICT")
    op.execute("ALTER TABLE finance.invoices DROP CONSTRAINT IF EXISTS uq_finance_invoices_id_org RESTRICT")
    op.execute("ALTER TABLE public.member_subscriptions_v2 DROP CONSTRAINT IF EXISTS uq_member_subscriptions_v2_id_org RESTRICT")
    op.execute("ALTER TABLE public.member_subscriptions_v2 DROP CONSTRAINT IF EXISTS fk_member_subscriptions_v2_branch_org RESTRICT")
    op.execute("ALTER TABLE public.membership_plans DROP CONSTRAINT IF EXISTS uq_membership_plans_id_org RESTRICT")
    op.execute("ALTER TABLE public.members DROP CONSTRAINT IF EXISTS uq_members_id_org RESTRICT")
    op.execute("REVOKE INSERT ON TABLE finance.refunds FROM app_security_owner")
    op.execute("REVOKE SELECT ON TABLE finance.invoices FROM app_security_owner")
    op.execute("REVOKE SELECT ON TABLE finance.payment_allocations FROM app_security_owner")
    for relation, privilege in (
        ("finance.payment_allocations", "SELECT"),
        ("finance.invoices", "SELECT"),
        ("finance.refunds", "INSERT"),
        ("public.member_subscriptions_v2", "SELECT"),
        ("finance.billing_parties", "SELECT"),
        ("finance.billing_parties", "INSERT"),
        ("finance.billing_parties", "UPDATE"),
        ("finance.legal_entities", "SELECT"),
        ("finance.gst_registrations", "SELECT"),
        ("finance.divisions", "SELECT"),
        ("finance.brands", "SELECT"),
        ("finance.tax_codes", "SELECT"),
        ("finance.invoices", "INSERT"),
        ("finance.invoice_lines", "INSERT"),
        ("public.members", "SELECT"),
        ("public.membership_plans", "SELECT"),
    ):
        if _has_table_privilege(bind, "app_security_owner", relation, privilege):
            raise RuntimeError(f"zd07 downgrade failed to remove zd07-owned ACL {privilege} on {relation}")
    for relation, privilege in (
        ("finance.payments", "SELECT"),
        ("finance.payments", "UPDATE"),
        ("finance.refunds", "SELECT"),
        ("finance.refunds", "UPDATE"),
        ("public.branch_outbox_events", "SELECT"),
    ):
        _require_predecessor_present_acl(bind, "app_security_owner", relation, privilege)
    if _has_table_privilege(bind, "app_security_owner", "finance.refunds", "INSERT"):
        raise RuntimeError("zd07 downgrade failed to remove zd07-owned INSERT on finance.refunds")
    if bind.execute(sa.text("SELECT pg_catalog.has_schema_privilege('migration_owner', 'app_secure', 'USAGE')")).scalar_one():
        raise RuntimeError("zd07 downgrade leaked migration_owner app_secure USAGE")
    _require_exact_function_count(
        bind,
        schema_name="app_secure",
        function_name="record_refund_obligation_binding",
        normalized_args="uuid,uuid",
        expected_count=0,
        error_label="zd07 downgrade failed to remove producer",
    )
    _require_exact_function_count(
        bind,
        schema_name="app_secure",
        function_name="resolve_branch_refund_required",
        normalized_args="uuid,uuid",
        expected_count=0,
        error_label="zd07 downgrade failed to remove resolver",
    )
    for function_name, normalized_args in (
        ("upsert_member_billing_party", "uuid,text,text"),
        ("resolve_member_subscription_checkout_inputs", "uuid"),
        ("record_member_subscription_checkout_binding", "uuid,uuid,uuid"),
        ("attach_member_subscription_checkout_provider_order", "uuid,uuid,text"),
        ("resolve_finance_invoice_organization", "uuid"),
        ("resolve_finance_billing_party_organization", "uuid"),
        ("resolve_finance_invoice_accounting_master_data", "uuid,uuid,uuid,uuid,uuid,uuid,date"),
        ("establish_finance_tax_code", "uuid,text,text,text,text,integer"),
        ("establish_branch_accounting_profile", "uuid,uuid,uuid,uuid,uuid,uuid,date,date"),
        ("establish_membership_plan_tax_profile", "uuid,uuid,uuid,text,date,date"),
    ):
        _require_exact_function_count(
            bind,
            schema_name="app_secure",
            function_name=function_name,
            normalized_args=normalized_args,
            expected_count=0,
            error_label=f"zd07 downgrade failed to remove {function_name}",
        )

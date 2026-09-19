"""PAY-4 member subscription term to Finance Core authority.

Revision ID: zo07d8e9f0a49
Revises: zn07d8e9f0a48
Create Date: 2026-09-19

Introduces the canonical immutable member-subscription-term to Finance binding,
a durable payment-context identity, pending-term materialization for the current
V2 compatibility API, and a Finance-event-only activation capability.

No provider call, payment capture, refund execution, live-money enablement,
release, deployment or production activation is introduced.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "zo07d8e9f0a49"
down_revision = "zn07d8e9f0a48"
branch_labels = None
depends_on = None

_MIGRATION_OWNER = "migration_owner"
_SECURITY_OWNER = "app_security_owner"
_API = "app_runtime"
_WORKER = "worker_runtime"

_CREATE_PENDING = "app_secure.create_member_subscription_pending_term(uuid)"
_RECORD_BINDING = "app_secure.record_member_subscription_finance_binding(uuid,uuid)"
_APPLY_EVENT = "app_secure.apply_member_subscription_finance_event(uuid,text)"

_TENANT = "NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid"


def _require_role(bind, role: str, *, login: bool = False) -> None:
    row = bind.execute(sa.text(
        """
        SELECT rolcanlogin,rolsuper,rolinherit,rolcreatedb,rolcreaterole,
               rolreplication,rolbypassrls
        FROM pg_catalog.pg_roles WHERE rolname=:role
        """
    ), {"role": role}).mappings().one_or_none()
    if row is None:
        raise RuntimeError(f"PAY-4 missing externally managed role: {role}")
    if bool(row["rolcanlogin"]) is not login:
        raise RuntimeError(f"PAY-4 role login posture drift: {role}")
    for key in ("rolsuper","rolinherit","rolcreatedb","rolcreaterole","rolreplication","rolbypassrls"):
        if bool(row[key]):
            raise RuntimeError(f"PAY-4 reduced-role drift: {role}.{key}")


def _require_identity(bind) -> None:
    _require_role(bind, _MIGRATION_OWNER, login=True)
    _require_role(bind, _SECURITY_OWNER)
    _require_role(bind, _API)
    _require_role(bind, _WORKER)
    identity = bind.execute(sa.text("SELECT session_user::text,current_user::text")).one()
    if tuple(identity) != (_MIGRATION_OWNER, _MIGRATION_OWNER):
        raise RuntimeError("PAY-4 migration requires migration_owner")
    if not bind.execute(sa.text(
        "SELECT pg_catalog.pg_has_role(:member,:target,'SET')"
    ), {"member": _MIGRATION_OWNER, "target": _SECURITY_OWNER}).scalar_one():
        raise RuntimeError("PAY-4 requires migration_owner SET edge to app_security_owner")
    for role in (_API, _WORKER):
        if bind.execute(sa.text(
            "SELECT pg_catalog.pg_has_role(:member,:target,'MEMBER') "
            "OR pg_catalog.pg_has_role(:member,:target,'SET')"
        ), {"member": role, "target": _SECURITY_OWNER}).scalar_one():
            raise RuntimeError(f"PAY-4 runtime capability can reach app_security_owner: {role}")


def _install_schema() -> None:
    op.execute(
        """
        CREATE TABLE finance.payment_contexts (
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            context_type VARCHAR(80) NOT NULL,
            business_reference VARCHAR(200) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT uq_finance_payment_contexts_id_org
                UNIQUE (id, organization_id),
            CONSTRAINT uq_finance_payment_contexts_business
                UNIQUE (organization_id, context_type, business_reference),
            CONSTRAINT chk_finance_payment_contexts_type
                CHECK (context_type = 'member_subscription_term'),
            CONSTRAINT chk_finance_payment_contexts_business
                CHECK (business_reference ~ '^subscription_term:[0-9a-f-]{36}$')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE finance.member_subscription_finance_bindings (
            id UUID PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
            organization_id UUID NOT NULL
                REFERENCES public.organizations(id) ON DELETE RESTRICT,
            subscription_term_id UUID NOT NULL,
            finance_invoice_id UUID NOT NULL,
            finance_payment_context_id UUID NOT NULL,
            member_id UUID NOT NULL,
            plan_snapshot JSONB NOT NULL,
            amount NUMERIC(14,2) NOT NULL,
            currency_code CHAR(3) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT fk_pay4_binding_term_org
                FOREIGN KEY (subscription_term_id, organization_id)
                REFERENCES public.subscription_terms(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT fk_pay4_binding_invoice_org
                FOREIGN KEY (finance_invoice_id, organization_id)
                REFERENCES finance.invoices(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_pay4_binding_context_org
                FOREIGN KEY (finance_payment_context_id, organization_id)
                REFERENCES finance.payment_contexts(id, organization_id) ON DELETE RESTRICT,
            CONSTRAINT fk_pay4_binding_member_org
                FOREIGN KEY (member_id, organization_id)
                REFERENCES public.members(id, org_id) ON DELETE RESTRICT,
            CONSTRAINT uq_pay4_binding_term UNIQUE (subscription_term_id),
            CONSTRAINT uq_pay4_binding_invoice UNIQUE (finance_invoice_id),
            CONSTRAINT uq_pay4_binding_context UNIQUE (finance_payment_context_id),
            CONSTRAINT chk_pay4_binding_plan_snapshot
                CHECK (pg_catalog.jsonb_typeof(plan_snapshot)='object'),
            CONSTRAINT chk_pay4_binding_amount CHECK (amount >= 0),
            CONSTRAINT chk_pay4_binding_currency CHECK (currency_code ~ '^[A-Z]{3}$')
        )
        """
    )
    for table in ("payment_contexts","member_subscription_finance_bindings"):
        op.execute(f"ALTER TABLE finance.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE finance.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE finance.{table} FROM PUBLIC")
        op.execute(f"GRANT SELECT,INSERT ON TABLE finance.{table} TO app_security_owner")
        op.execute(
            f"""
            CREATE POLICY pay4_{table}_security_owner_select
            ON finance.{table}
            FOR SELECT TO app_security_owner
            USING (organization_id = {_TENANT})
            """
        )
        op.execute(
            f"""
            CREATE POLICY pay4_{table}_security_owner_insert
            ON finance.{table}
            FOR INSERT TO app_security_owner
            WITH CHECK (organization_id = {_TENANT})
            """
        )

    # Existing Finance evidence is read by the reduced owner only inside the
    # SECURITY DEFINER activation capability. Runtime roles remain table-blind.
    for table in ("invoices","invoice_lines","payments","payment_allocations","outbox_events","billing_parties"):
        op.execute(f"GRANT SELECT ON TABLE finance.{table} TO app_security_owner")

    # Product writes remain capability-only. Ordinary app_runtime retains its
    # predecessor read/create contract and receives no lifecycle table DML.
    for table in (
        "member_subscriptions_v2","membership_plans","members","org_branches",
        "subscription_series","subscription_terms","subscription_term_slots",
        "subscription_slot_assignments","subscription_events",
    ):
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO app_security_owner")

    for table in ("subscription_series","subscription_terms","subscription_term_slots",
                  "subscription_slot_assignments","subscription_events"):
        op.execute(
            f"""
            CREATE POLICY pay4_{table}_security_owner_select
            ON public.{table}
            FOR SELECT TO app_security_owner
            USING (org_id = {_TENANT})
            """
        )
        op.execute(
            f"""
            CREATE POLICY pay4_{table}_security_owner_insert
            ON public.{table}
            FOR INSERT TO app_security_owner
            WITH CHECK (org_id = {_TENANT})
            """
        )

    for table in ("member_subscriptions_v2","membership_plans","members","org_branches"):
        op.execute(
            f"""
            CREATE POLICY pay4_{table}_security_owner_select
            ON public.{table}
            FOR SELECT TO app_security_owner
            USING (org_id = {_TENANT})
            """
        )

    op.execute(
        "GRANT UPDATE (status,activated_at,updated_at,version) "
        "ON TABLE public.subscription_terms TO app_security_owner"
    )
    op.execute(
        f"""
        CREATE POLICY pay4_subscription_terms_security_owner_update
        ON public.subscription_terms
        FOR UPDATE TO app_security_owner
        USING (org_id = {_TENANT})
        WITH CHECK (org_id = {_TENANT})
        """
    )
    op.execute(
        "GRANT UPDATE (status,updated_at,updated_by) "
        "ON TABLE public.member_subscriptions_v2 TO app_security_owner"
    )
    op.execute(
        f"""
        CREATE POLICY pay4_member_subscriptions_v2_security_owner_update
        ON public.member_subscriptions_v2
        FOR UPDATE TO app_security_owner
        USING (org_id = {_TENANT})
        WITH CHECK (org_id = {_TENANT})
        """
    )


def _install_immutability_and_activation_guards() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay4_reject_binding_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            AS $function$
            BEGIN
                IF session_user = 'migration_owner' THEN
                    RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
                END IF;
                RAISE EXCEPTION 'PAY-4 member finance binding history is immutable'
                    USING ERRCODE='42501';
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay4_guard_subscription_activation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            AS $function$
            DECLARE
                v_crossing boolean;
            BEGIN
                v_crossing := NEW.status::text IN ('scheduled','active')
                    AND (
                        TG_OP='INSERT'
                        OR OLD.status::text IS DISTINCT FROM NEW.status::text
                    );
                IF v_crossing AND NOT (
                    current_user='app_security_owner'
                    AND pg_catalog.pg_has_role(session_user,'worker_runtime','MEMBER')
                ) THEN
                    RAISE EXCEPTION 'PAY-4 subscription activation requires Finance worker authority'
                        USING ERRCODE='42501';
                END IF;
                RETURN NEW;
            END
            $function$
            """
        )
        op.execute(
            r"""
            CREATE FUNCTION app_secure.pay4_guard_v2_activation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            AS $function$
            DECLARE
                v_crossing boolean;
            BEGIN
                v_crossing := NEW.status::text='active'
                    AND (
                        TG_OP='INSERT'
                        OR OLD.status::text IS DISTINCT FROM NEW.status::text
                    );
                IF v_crossing AND NOT (
                    current_user='app_security_owner'
                    AND pg_catalog.pg_has_role(session_user,'worker_runtime','MEMBER')
                ) THEN
                    RAISE EXCEPTION 'PAY-4 V2 activation requires Finance worker authority'
                        USING ERRCODE='42501';
                END IF;
                RETURN NEW;
            END
            $function$
            """
        )
    finally:
        op.execute("RESET ROLE")

    op.execute(
        """
        CREATE TRIGGER trg_pay4_payment_contexts_immutable
        BEFORE UPDATE OR DELETE ON finance.payment_contexts
        FOR EACH ROW EXECUTE FUNCTION app_secure.pay4_reject_binding_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay4_member_finance_bindings_immutable
        BEFORE UPDATE OR DELETE ON finance.member_subscription_finance_bindings
        FOR EACH ROW EXECUTE FUNCTION app_secure.pay4_reject_binding_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay4_subscription_term_activation_guard
        BEFORE INSERT OR UPDATE OF status ON public.subscription_terms
        FOR EACH ROW EXECUTE FUNCTION app_secure.pay4_guard_subscription_activation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pay4_v2_activation_guard
        BEFORE INSERT OR UPDATE OF status ON public.member_subscriptions_v2
        FOR EACH ROW EXECUTE FUNCTION app_secure.pay4_guard_v2_activation()
        """
    )


def _install_capabilities() -> None:
    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        op.execute(
            r"""
            CREATE FUNCTION app_secure.create_member_subscription_pending_term(
                p_subscription_id uuid
            )
            RETURNS TABLE(
                subscription_term_id uuid,
                subscription_series_id uuid,
                inserted boolean,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_source public.member_subscriptions_v2%ROWTYPE;
                v_plan public.membership_plans%ROWTYPE;
                v_series_id uuid;
                v_term_id uuid;
                v_existing public.subscription_terms%ROWTYPE;
                v_slot_id uuid;
                v_idx integer;
            BEGIN
                IF NOT pg_catalog.pg_has_role(session_user,'app_runtime','MEMBER') THEN
                    RAISE EXCEPTION 'PAY-4 pending admission requires app_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org := NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid;
                IF v_org IS NULL OR p_subscription_id IS NULL THEN
                    RAISE EXCEPTION 'PAY-4 pending admission requires tenant and subscription'
                        USING ERRCODE='22023';
                END IF;

                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended('pay4:pending-term:'||p_subscription_id::text,0)
                );

                SELECT * INTO v_existing
                FROM public.subscription_terms t
                WHERE t.legacy_member_subscription_v2_id=p_subscription_id
                  AND t.org_id=v_org
                FOR UPDATE;
                IF FOUND THEN
                    IF v_existing.status::text <> 'pending_payment' THEN
                        RAISE EXCEPTION 'PAY-4 pending admission replay conflicts with existing lifecycle state'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT v_existing.id,v_existing.series_id,false,true;
                    RETURN;
                END IF;

                SELECT * INTO v_source
                FROM public.member_subscriptions_v2 s
                WHERE s.id=p_subscription_id AND s.org_id=v_org
                FOR SHARE;
                IF NOT FOUND OR v_source.status::text <> 'pending' THEN
                    RAISE EXCEPTION 'PAY-4 source subscription must exist as pending'
                        USING ERRCODE='23514';
                END IF;

                SELECT * INTO v_plan
                FROM public.membership_plans p
                WHERE p.id=v_source.membership_plan_id AND p.org_id=v_org;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'PAY-4 source membership plan unavailable'
                        USING ERRCODE='23514';
                END IF;

                v_series_id := pg_catalog.gen_random_uuid();
                v_term_id := pg_catalog.gen_random_uuid();

                INSERT INTO public.subscription_series(
                    id,org_id,originating_branch_id,series_code,primary_member_id,
                    lifecycle_status,opened_by,metadata,version
                ) VALUES (
                    v_series_id,v_org,v_source.branch_id,'SER-'||v_source.subscription_code,
                    v_source.primary_member_id,'open',v_source.created_by,
                    pg_catalog.jsonb_build_object(
                        'source_table','member_subscriptions_v2',
                        'source_id',v_source.id::text,
                        'pay4_authority','pending_payment'
                    ),1
                );

                INSERT INTO public.subscription_terms(
                    id,org_id,branch_id,series_id,sequence_number,term_code,
                    renewed_from_term_id,source_type,plan_id,
                    legacy_member_subscription_v2_id,legacy_subscription_code,
                    plan_code_snapshot,plan_name_snapshot,duration_unit_snapshot,
                    duration_value_snapshot,capacity_snapshot,currency_code,
                    list_price_amount,discount_amount,tax_amount,final_amount,
                    starts_on,base_ends_on,effective_ends_on,status,
                    source_metadata,created_by,version
                ) VALUES (
                    v_term_id,v_org,v_source.branch_id,v_series_id,1,
                    v_source.subscription_code,NULL,'admission',v_source.membership_plan_id,
                    v_source.id,v_source.subscription_code,v_plan.plan_code,v_plan.name,
                    v_source.duration_unit_snapshot,v_source.duration_value_snapshot,
                    v_source.max_members_snapshot,upper(v_source.currency_code),
                    v_source.price_snapshot,0,0,v_source.price_snapshot,
                    v_source.start_date,v_source.end_date,v_source.end_date,'pending_payment',
                    pg_catalog.jsonb_build_object(
                        'source_table','member_subscriptions_v2',
                        'source_id',v_source.id::text,
                        'pay4_authority','finance_required'
                    ),
                    v_source.created_by,1
                );

                FOR v_idx IN 1..v_source.max_members_snapshot LOOP
                    v_slot_id := pg_catalog.gen_random_uuid();
                    INSERT INTO public.subscription_term_slots(
                        id,org_id,term_id,slot_index,slot_role,created_by
                    ) VALUES (
                        v_slot_id,v_org,v_term_id,v_idx,
                        CASE WHEN v_idx=1 THEN 'primary'::subscription_slot_role
                             ELSE 'standard'::subscription_slot_role END,
                        v_source.created_by
                    );
                    IF v_idx=1 THEN
                        INSERT INTO public.subscription_slot_assignments(
                            id,org_id,term_id,term_slot_id,member_id,effective_from,
                            effective_until,assignment_state,assigned_by
                        ) VALUES (
                            pg_catalog.gen_random_uuid(),v_org,v_term_id,v_slot_id,
                            v_source.primary_member_id,v_source.start_date,v_source.end_date,
                            'active',v_source.created_by
                        );
                    END IF;
                END LOOP;

                INSERT INTO public.subscription_events(
                    id,org_id,branch_id,series_id,term_id,event_type,actor_user_id,
                    event_source,correlation_id,idempotency_key,metadata
                ) VALUES (
                    pg_catalog.gen_random_uuid(),v_org,v_source.branch_id,v_series_id,v_term_id,
                    'admission_created',v_source.created_by,'pay4_pending_admission',
                    p_subscription_id::text,'pay4:admission:'||p_subscription_id::text,
                    pg_catalog.jsonb_build_object(
                        'subscription_id',p_subscription_id::text,
                        'status','pending_payment'
                    )
                );

                RETURN QUERY SELECT v_term_id,v_series_id,true,false;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.record_member_subscription_finance_binding(
                p_subscription_id uuid,
                p_invoice_id uuid
            )
            RETURNS TABLE(
                binding_id uuid,
                subscription_term_id uuid,
                finance_invoice_id uuid,
                finance_payment_context_id uuid,
                amount numeric,
                currency_code text,
                inserted boolean,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_term public.subscription_terms%ROWTYPE;
                v_series public.subscription_series%ROWTYPE;
                v_invoice finance.invoices%ROWTYPE;
                v_party finance.billing_parties%ROWTYPE;
                v_context finance.payment_contexts%ROWTYPE;
                v_binding finance.member_subscription_finance_bindings%ROWTYPE;
                v_line_count bigint;
                v_line_unit numeric;
                v_line_description text;
                v_plan_snapshot jsonb;
            BEGIN
                IF NOT pg_catalog.pg_has_role(session_user,'app_runtime','MEMBER') THEN
                    RAISE EXCEPTION 'PAY-4 finance binding requires app_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org := NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid;
                IF v_org IS NULL OR p_subscription_id IS NULL OR p_invoice_id IS NULL THEN
                    RAISE EXCEPTION 'PAY-4 finance binding identity invalid'
                        USING ERRCODE='22023';
                END IF;

                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended('pay4:binding:'||p_subscription_id::text,0)
                );

                SELECT * INTO v_term
                FROM public.subscription_terms t
                WHERE t.legacy_member_subscription_v2_id=p_subscription_id
                  AND t.org_id=v_org
                FOR SHARE;
                IF NOT FOUND OR v_term.status::text <> 'pending_payment' THEN
                    RAISE EXCEPTION 'PAY-4 binding requires pending_payment subscription term'
                        USING ERRCODE='23514';
                END IF;
                SELECT * INTO v_series
                FROM public.subscription_series s
                WHERE s.id=v_term.series_id AND s.org_id=v_org;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'PAY-4 subscription series unavailable'
                        USING ERRCODE='23514';
                END IF;

                SELECT * INTO v_invoice
                FROM finance.invoices i
                WHERE i.id=p_invoice_id AND i.organization_id=v_org
                FOR SHARE;
                IF NOT FOUND OR v_invoice.status NOT IN ('issued','partially_paid','paid') THEN
                    RAISE EXCEPTION 'PAY-4 binding requires issued Finance invoice'
                        USING ERRCODE='23514';
                END IF;
                IF upper(v_invoice.currency_code) <> upper(v_term.currency_code) THEN
                    RAISE EXCEPTION 'PAY-4 binding invoice currency mismatch'
                        USING ERRCODE='23514';
                END IF;

                SELECT * INTO v_party
                FROM finance.billing_parties bp
                WHERE bp.id=v_invoice.billing_party_id
                  AND bp.organization_id=v_org;
                IF NOT FOUND OR v_party.member_id IS DISTINCT FROM v_series.primary_member_id THEN
                    RAISE EXCEPTION 'PAY-4 binding invoice member mismatch'
                        USING ERRCODE='23514';
                END IF;

                SELECT count(*),min(il.unit_amount),min(il.description)
                INTO v_line_count,v_line_unit,v_line_description
                FROM finance.invoice_lines il
                WHERE il.invoice_id=v_invoice.id;
                IF v_line_count <> 1
                   OR v_line_unit IS DISTINCT FROM v_term.list_price_amount
                   OR pg_catalog.btrim(v_line_description) IS DISTINCT FROM pg_catalog.btrim(v_term.plan_name_snapshot) THEN
                    RAISE EXCEPTION 'PAY-4 binding invoice plan snapshot mismatch'
                        USING ERRCODE='23514';
                END IF;

                SELECT * INTO v_binding
                FROM finance.member_subscription_finance_bindings b
                WHERE b.subscription_term_id=v_term.id
                FOR SHARE;
                IF FOUND THEN
                    IF v_binding.finance_invoice_id IS DISTINCT FROM p_invoice_id THEN
                        RAISE EXCEPTION 'PAY-4 subscription term already bound to different Finance invoice'
                            USING ERRCODE='23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_binding.id,v_binding.subscription_term_id,
                        v_binding.finance_invoice_id,v_binding.finance_payment_context_id,
                        v_binding.amount,v_binding.currency_code::text,false,true;
                    RETURN;
                END IF;

                INSERT INTO finance.payment_contexts(
                    organization_id,context_type,business_reference
                ) VALUES (
                    v_org,'member_subscription_term','subscription_term:'||v_term.id::text
                )
                ON CONFLICT ON CONSTRAINT uq_finance_payment_contexts_business
                DO UPDATE SET business_reference=EXCLUDED.business_reference
                RETURNING * INTO v_context;

                v_plan_snapshot := pg_catalog.jsonb_build_object(
                    'plan_id',v_term.plan_id::text,
                    'plan_code',v_term.plan_code_snapshot,
                    'plan_name',v_term.plan_name_snapshot,
                    'duration_unit',v_term.duration_unit_snapshot::text,
                    'duration_value',v_term.duration_value_snapshot,
                    'capacity',v_term.capacity_snapshot,
                    'list_price_amount',v_term.list_price_amount::text
                );

                INSERT INTO finance.member_subscription_finance_bindings(
                    organization_id,subscription_term_id,finance_invoice_id,
                    finance_payment_context_id,member_id,plan_snapshot,amount,currency_code
                ) VALUES (
                    v_org,v_term.id,v_invoice.id,v_context.id,v_series.primary_member_id,
                    v_plan_snapshot,v_invoice.grand_total_amount,upper(v_invoice.currency_code)
                )
                RETURNING * INTO v_binding;

                RETURN QUERY SELECT
                    v_binding.id,v_binding.subscription_term_id,
                    v_binding.finance_invoice_id,v_binding.finance_payment_context_id,
                    v_binding.amount,v_binding.currency_code::text,true,false;
            END
            $function$
            """
        )

        op.execute(
            r"""
            CREATE FUNCTION app_secure.apply_member_subscription_finance_event(
                p_finance_event_id uuid,
                p_idempotency_key text
            )
            RETURNS TABLE(
                subscription_term_id uuid,
                subscription_status text,
                activated boolean,
                replayed boolean
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path=pg_catalog,public,finance
            SET row_security=on
            AS $function$
            DECLARE
                v_org uuid;
                v_event finance.outbox_events%ROWTYPE;
                v_binding finance.member_subscription_finance_bindings%ROWTYPE;
                v_invoice finance.invoices%ROWTYPE;
                v_term public.subscription_terms%ROWTYPE;
                v_series public.subscription_series%ROWTYPE;
                v_allocated numeric;
                v_payment_count bigint;
                v_bad_payment_count bigint;
                v_existing_event uuid;
                v_target text;
                v_business_date date;
                v_legacy uuid;
            BEGIN
                IF NOT pg_catalog.pg_has_role(session_user,'worker_runtime','MEMBER') THEN
                    RAISE EXCEPTION 'PAY-4 activation requires worker_runtime'
                        USING ERRCODE='42501';
                END IF;
                v_org := NULLIF(pg_catalog.current_setting('app.current_org_id',true),'')::uuid;
                IF v_org IS NULL OR p_finance_event_id IS NULL
                   OR p_idempotency_key IS NULL
                   OR p_idempotency_key !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$' THEN
                    RAISE EXCEPTION 'PAY-4 activation identity invalid'
                        USING ERRCODE='22023';
                END IF;

                SELECT * INTO v_event
                FROM finance.outbox_events e
                WHERE e.id=p_finance_event_id
                  AND e.organization_id=v_org
                  AND e.aggregate_type='invoice'
                  AND e.event_type='finance.invoice.paid'
                FOR SHARE;
                IF NOT FOUND
                   OR v_event.payload_json->>'invoice_id' IS DISTINCT FROM v_event.aggregate_id::text
                   OR v_event.payload_json->>'status' IS DISTINCT FROM 'paid' THEN
                    RAISE EXCEPTION 'PAY-4 activation requires authoritative Finance invoice paid event'
                        USING ERRCODE='23514';
                END IF;

                SELECT * INTO v_binding
                FROM finance.member_subscription_finance_bindings b
                WHERE b.organization_id=v_org
                  AND b.finance_invoice_id=v_event.aggregate_id
                FOR SHARE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'PAY-4 activation Finance binding unavailable'
                        USING ERRCODE='23514';
                END IF;

                SELECT * INTO v_term
                FROM public.subscription_terms t
                WHERE t.id=v_binding.subscription_term_id AND t.org_id=v_org
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'PAY-4 activation subscription term unavailable'
                        USING ERRCODE='23514';
                END IF;

                SELECT e.id INTO v_existing_event
                FROM public.subscription_events e
                WHERE e.org_id=v_org
                  AND e.term_id=v_term.id
                  AND e.event_source='finance'
                  AND e.metadata->>'finance_event_id'=p_finance_event_id::text
                  AND e.idempotency_key=p_idempotency_key
                LIMIT 1;
                IF v_existing_event IS NOT NULL THEN
                    RETURN QUERY SELECT v_term.id,v_term.status::text,false,true;
                    RETURN;
                END IF;
                IF v_term.status::text <> 'pending_payment' THEN
                    RAISE EXCEPTION 'PAY-4 activation term is not pending_payment'
                        USING ERRCODE='23505';
                END IF;

                SELECT * INTO v_invoice
                FROM finance.invoices i
                WHERE i.id=v_binding.finance_invoice_id
                  AND i.organization_id=v_org
                FOR SHARE;
                IF NOT FOUND OR v_invoice.status <> 'paid'
                   OR v_invoice.grand_total_amount IS DISTINCT FROM v_binding.amount
                   OR upper(v_invoice.currency_code) IS DISTINCT FROM upper(v_binding.currency_code) THEN
                    RAISE EXCEPTION 'PAY-4 activation invoice amount/currency/state mismatch'
                        USING ERRCODE='23514';
                END IF;

                SELECT
                    COALESCE(sum(a.allocated_amount),0),
                    count(*),
                    count(*) FILTER (
                        WHERE p.id IS NULL
                           OR p.organization_id IS DISTINCT FROM v_org
                           OR upper(p.currency_code) IS DISTINCT FROM upper(v_binding.currency_code)
                           OR p.status NOT IN ('captured','settled')
                    )
                INTO v_allocated,v_payment_count,v_bad_payment_count
                FROM finance.payment_allocations a
                LEFT JOIN finance.payments p ON p.id=a.payment_id
                WHERE a.invoice_id=v_invoice.id;

                IF v_payment_count=0
                   OR v_bad_payment_count<>0
                   OR v_allocated IS DISTINCT FROM v_binding.amount THEN
                    RAISE EXCEPTION 'PAY-4 activation requires fully applied matching Finance payments'
                        USING ERRCODE='23514';
                END IF;

                SELECT * INTO v_series
                FROM public.subscription_series s
                WHERE s.id=v_term.series_id AND s.org_id=v_org;
                IF NOT FOUND
                   OR v_series.primary_member_id IS DISTINCT FROM v_binding.member_id
                   OR upper(v_term.currency_code) IS DISTINCT FROM upper(v_binding.currency_code)
                   OR v_binding.plan_snapshot->>'plan_id' IS DISTINCT FROM v_term.plan_id::text
                   OR v_binding.plan_snapshot->>'plan_code' IS DISTINCT FROM v_term.plan_code_snapshot
                   OR v_binding.plan_snapshot->>'plan_name' IS DISTINCT FROM v_term.plan_name_snapshot THEN
                    RAISE EXCEPTION 'PAY-4 activation binding snapshot drift'
                        USING ERRCODE='23514';
                END IF;

                SELECT COALESCE(
                    pg_catalog.timezone(NULLIF(b.timezone,''),pg_catalog.clock_timestamp())::date,
                    pg_catalog.current_date
                )
                INTO v_business_date
                FROM public.org_branches b
                WHERE b.id=v_term.branch_id AND b.org_id=v_org;
                IF v_business_date IS NULL THEN
                    RAISE EXCEPTION 'PAY-4 activation branch timezone unavailable'
                        USING ERRCODE='23514';
                END IF;
                IF v_business_date > v_term.effective_ends_on THEN
                    RAISE EXCEPTION 'PAY-4 paid subscription term is already outside entitlement window'
                        USING ERRCODE='23514';
                END IF;

                v_target := CASE
                    WHEN v_business_date < v_term.starts_on THEN 'scheduled'
                    ELSE 'active'
                END;

                UPDATE public.subscription_terms
                SET status=v_target::subscription_term_status,
                    activated_at=CASE WHEN v_target='active' THEN pg_catalog.clock_timestamp() ELSE NULL END,
                    updated_at=pg_catalog.clock_timestamp(),
                    version=version+1
                WHERE id=v_term.id AND org_id=v_org;

                v_legacy := v_term.legacy_member_subscription_v2_id;
                IF v_target='active' AND v_legacy IS NOT NULL THEN
                    UPDATE public.member_subscriptions_v2
                    SET status='active',
                        updated_at=pg_catalog.clock_timestamp()
                    WHERE id=v_legacy AND org_id=v_org AND status='pending';
                END IF;

                INSERT INTO public.subscription_events(
                    id,org_id,branch_id,series_id,term_id,event_type,event_at,
                    actor_user_id,event_source,correlation_id,idempotency_key,metadata,
                    before_snapshot,after_snapshot
                ) VALUES (
                    pg_catalog.gen_random_uuid(),v_org,v_term.branch_id,v_term.series_id,v_term.id,
                    CASE WHEN v_target='active' THEN 'term_activated'::subscription_event_type
                         ELSE 'term_scheduled'::subscription_event_type END,
                    pg_catalog.clock_timestamp(),NULL,'finance',p_finance_event_id::text,
                    p_idempotency_key,
                    pg_catalog.jsonb_build_object(
                        'finance_event_id',p_finance_event_id::text,
                        'finance_binding_id',v_binding.id::text,
                        'finance_invoice_id',v_binding.finance_invoice_id::text,
                        'finance_payment_context_id',v_binding.finance_payment_context_id::text,
                        'amount',v_binding.amount::text,
                        'currency',v_binding.currency_code::text
                    ),
                    pg_catalog.jsonb_build_object('status','pending_payment'),
                    pg_catalog.jsonb_build_object('status',v_target)
                );

                RETURN QUERY SELECT v_term.id,v_target,true,false;
            END
            $function$
            """
        )

        for signature in (_CREATE_PENDING,_RECORD_BINDING,_APPLY_EVENT):
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    finally:
        op.execute("RESET ROLE")

    # Admission and invoice-binding are existing API-side preparation only.
    # The money-derived activation capability remains unbound until PAY-5.
    op.execute("GRANT USAGE ON SCHEMA app_secure TO app_runtime")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_CREATE_PENDING} TO app_runtime")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_RECORD_BINDING} TO app_runtime")


def upgrade() -> None:
    bind=op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)
    for relation in ("finance.payment_contexts","finance.member_subscription_finance_bindings"):
        if bind.execute(sa.text(
            "SELECT pg_catalog.to_regclass(:relation) IS NOT NULL"
        ), {"relation":relation}).scalar_one():
            raise RuntimeError(f"PAY-4 relation already exists: {relation}")
    _install_schema()
    _install_immutability_and_activation_guards()
    _install_capabilities()

    for role in (_API,_WORKER):
        for table in ("finance.payment_contexts","finance.member_subscription_finance_bindings"):
            for privilege in ("SELECT","INSERT","UPDATE","DELETE","TRUNCATE"):
                if bind.execute(sa.text(
                    "SELECT pg_catalog.has_table_privilege(:role,:relation,:privilege)"
                ), {"role":role,"relation":table,"privilege":privilege}).scalar_one():
                    raise RuntimeError(
                        f"PAY-4 runtime role received direct binding authority: {role} {table} {privilege}"
                    )
    if bind.execute(sa.text(
        "SELECT pg_catalog.has_function_privilege('worker_runtime',"
        "'app_secure.apply_member_subscription_finance_event(uuid,text)','EXECUTE')"
    )).scalar_one():
        raise RuntimeError("PAY-4 activation capability must remain unbound until PAY-5")


def downgrade() -> None:
    bind=op.get_bind()
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("SET LOCAL statement_timeout='30s'")
    _require_identity(bind)

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        binding_rows=bind.execute(sa.text(
            "SELECT EXISTS(SELECT 1 FROM finance.member_subscription_finance_bindings LIMIT 1)"
        )).scalar_one()
        activation_rows=bind.execute(sa.text(
            """
            SELECT EXISTS(
              SELECT 1 FROM public.subscription_events
              WHERE event_source='finance'
                AND metadata ? 'finance_binding_id'
              LIMIT 1
            )
            """
        )).scalar_one()
    finally:
        op.execute("RESET ROLE")
    if binding_rows or activation_rows:
        raise RuntimeError(
            "PAY-4 downgrade blocked: member-Finance binding/activation evidence exists"
        )

    op.execute(f"REVOKE EXECUTE ON FUNCTION {_RECORD_BINDING} FROM app_runtime")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_CREATE_PENDING} FROM app_runtime")

    for trigger,table in (
        ("trg_pay4_v2_activation_guard","public.member_subscriptions_v2"),
        ("trg_pay4_subscription_term_activation_guard","public.subscription_terms"),
        ("trg_pay4_member_finance_bindings_immutable","finance.member_subscription_finance_bindings"),
        ("trg_pay4_payment_contexts_immutable","finance.payment_contexts"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")

    op.execute("SET LOCAL ROLE app_security_owner")
    try:
        for signature in (_APPLY_EVENT,_RECORD_BINDING,_CREATE_PENDING):
            op.execute(f"DROP FUNCTION IF EXISTS {signature}")
        op.execute("DROP FUNCTION IF EXISTS app_secure.pay4_guard_v2_activation()")
        op.execute("DROP FUNCTION IF EXISTS app_secure.pay4_guard_subscription_activation()")
        op.execute("DROP FUNCTION IF EXISTS app_secure.pay4_reject_binding_mutation()")
    finally:
        op.execute("RESET ROLE")

    op.execute(
        "DROP POLICY IF EXISTS pay4_member_subscriptions_v2_security_owner_update "
        "ON public.member_subscriptions_v2"
    )
    op.execute(
        "REVOKE UPDATE (status,updated_at,updated_by) "
        "ON TABLE public.member_subscriptions_v2 FROM app_security_owner"
    )
    op.execute(
        "DROP POLICY IF EXISTS pay4_subscription_terms_security_owner_update "
        "ON public.subscription_terms"
    )
    op.execute(
        "REVOKE UPDATE (status,activated_at,updated_at,version) "
        "ON TABLE public.subscription_terms FROM app_security_owner"
    )

    for table in ("member_subscriptions_v2","membership_plans","members","org_branches"):
        op.execute(
            f"DROP POLICY IF EXISTS pay4_{table}_security_owner_select ON public.{table}"
        )
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM app_security_owner")

    for table in (
        "subscription_events","subscription_slot_assignments","subscription_term_slots",
        "subscription_terms","subscription_series",
    ):
        op.execute(f"DROP POLICY IF EXISTS pay4_{table}_security_owner_insert ON public.{table}")
        op.execute(f"DROP POLICY IF EXISTS pay4_{table}_security_owner_select ON public.{table}")
        op.execute(f"REVOKE SELECT,INSERT ON TABLE public.{table} FROM app_security_owner")

    for table in ("invoices","invoice_lines","payments","payment_allocations","outbox_events","billing_parties"):
        op.execute(f"REVOKE SELECT ON TABLE finance.{table} FROM app_security_owner")

    for table in ("member_subscription_finance_bindings","payment_contexts"):
        op.execute(f"DROP POLICY IF EXISTS pay4_{table}_security_owner_insert ON finance.{table}")
        op.execute(f"DROP POLICY IF EXISTS pay4_{table}_security_owner_select ON finance.{table}")

    op.execute("DROP TABLE finance.member_subscription_finance_bindings")
    op.execute("DROP TABLE finance.payment_contexts")

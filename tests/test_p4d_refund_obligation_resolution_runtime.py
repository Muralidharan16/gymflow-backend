from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from psycopg import sql

import psycopg
import pytest
from psycopg.errors import CheckViolation, ExclusionViolation, InsufficientPrivilege, UniqueViolation
from psycopg.types.json import Jsonb

from tests.test_p4d_refund_authority_runtime import (
    _ADMIN_LOGIN,
    _APP_LOGIN,
    _BRANCH_A,
    _BRANCH_B,
    _CONFIG_LOGIN,
    _ENTITY_A,
    _ENTITY_B,
    _MAINTENANCE_LOGIN,
    _ORG_A,
    _ORG_B,
    _PAYMENT_A,
    _PAYMENT_B,
    _SOURCE_A,
    _WORKER_LOGIN,
    _connect,
    _security_owner_fetchone,
    _validate_safe_p4d_database,
)


_RESOLVE = "app_secure.resolve_branch_refund_required(uuid,uuid)"
_PAYMENT_C = uuid.UUID("d4200000-0000-4000-8000-000000000021")
_SOURCE_C = uuid.UUID("d4200000-0000-4000-8000-000000000051")
_SOURCE_D = uuid.UUID("d4200000-0000-4000-8000-000000000052")
_BRANCH_C = uuid.UUID("d4200000-0000-4000-8000-000000000081")
_INVOICE_A = uuid.UUID("d4200000-0000-4000-8000-000000000101")
_INVOICE_C = uuid.UUID("d4200000-0000-4000-8000-000000000102")
_MEMBER_A = uuid.UUID("d4200000-0000-4000-8000-000000000103")
_MEMBER_B = uuid.UUID("d4200000-0000-4000-8000-00000000010c")
_MEMBER_C_A = uuid.UUID("d4200000-0000-4000-8000-000000000104")
_MEMBER_C_C = uuid.UUID("d4200000-0000-4000-8000-000000000109")
_PLAN_A = uuid.UUID("d4200000-0000-4000-8000-000000000105")
_PLAN_B = uuid.UUID("d4200000-0000-4000-8000-00000000010d")
_PLAN_C_A = uuid.UUID("d4200000-0000-4000-8000-000000000108")
_PLAN_C_C = uuid.UUID("d4200000-0000-4000-8000-00000000010a")
_SUBSCRIPTION_A = uuid.UUID("d4200000-0000-4000-8000-000000000106")
_SUBSCRIPTION_B = uuid.UUID("d4200000-0000-4000-8000-00000000010e")
_SUBSCRIPTION_C_A = uuid.UUID("d4200000-0000-4000-8000-000000000107")
_SUBSCRIPTION_C_C = uuid.UUID("d4200000-0000-4000-8000-00000000010b")
_GST_A = uuid.UUID("d4200000-0000-4000-8000-000000000111")
_DIVISION_A = uuid.UUID("d4200000-0000-4000-8000-000000000121")
_BRAND_A = uuid.UUID("d4200000-0000-4000-8000-000000000131")
_BILLING_A = uuid.UUID("d4200000-0000-4000-8000-000000000141")
_TAX_CODE_A = uuid.UUID("d4200000-0000-4000-8000-000000000151")
_BRANCH_PROFILE_A = uuid.UUID("d4200000-0000-4000-8000-000000000161")
_BRANCH_PROFILE_CONFLICT = uuid.UUID("d4200000-0000-4000-8000-000000000162")
_PLAN_TAX_PROFILE_A = uuid.UUID("d4200000-0000-4000-8000-000000000171")
_PLAN_TAX_PROFILE_CONFLICT = uuid.UUID("d4200000-0000-4000-8000-000000000172")
_CONFIG_TEST1_TAX_CODE = uuid.UUID("d4200000-0000-4000-8000-000000101151")
_CONFIG_TEST1_BRANCH_PROFILE = uuid.UUID("d4200000-0000-4000-8000-000000101161")
_CONFIG_TEST1_PLAN_TAX_PROFILE = uuid.UUID("d4200000-0000-4000-8000-000000101171")
_CONFIG_TEST3_TAX_CODE = uuid.UUID("d4200000-0000-4000-8000-000000103151")
_CONFIG_TEST3_BRANCH_PROFILE = uuid.UUID("d4200000-0000-4000-8000-000000103161")
_CONFIG_TEST3_BRANCH_PROFILE_CONFLICT = uuid.UUID("d4200000-0000-4000-8000-000000103162")
_CONFIG_TEST3_PLAN_TAX_PROFILE = uuid.UUID("d4200000-0000-4000-8000-000000103171")
_CONFIG_TEST4_TAX_CODE = uuid.UUID("d4200000-0000-4000-8000-000000104151")
_CONFIG_TEST4_BRANCH_PROFILE = uuid.UUID("d4200000-0000-4000-8000-000000104161")
_CONFIG_TEST4_PLAN_TAX_PROFILE = uuid.UUID("d4200000-0000-4000-8000-000000104171")
_CONFIG_TEST5_TAX_CODE = uuid.UUID("d4200000-0000-4000-8000-000000105151")
_CONFIG_TEST5_BRANCH_PROFILE = uuid.UUID("d4200000-0000-4000-8000-000000105161")
_CONFIG_TEST5_PLAN_TAX_PROFILE = uuid.UUID("d4200000-0000-4000-8000-000000105171")
_CORRELATION_C = uuid.UUID("d4200000-0000-4000-8000-000000000061")
_CORRELATION_D = uuid.UUID("d4200000-0000-4000-8000-000000000062")
_WORKER_A = uuid.UUID("d4200000-0000-4000-8000-000000000071")
_WORKER_B = uuid.UUID("d4200000-0000-4000-8000-000000000072")


def _cleanup_p4d2_rows() -> None:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE finance.refund_execution_commands")
            cur.execute(
                "TRUNCATE TABLE public.notification_delivery_attempts, public.notification_operator_actions, "
                "public.notification_provider_events, public.branch_search_effect_attempts, "
                "public.notification_commands, public.branch_outbox_events"
            )
            cur.execute("TRUNCATE TABLE finance.member_subscription_checkout_bindings")
            cur.execute("TRUNCATE TABLE finance.refund_obligation_bindings")
            cur.execute("DELETE FROM finance.credit_notes WHERE invoice_id = ANY(%s)", ([_INVOICE_A, _INVOICE_C],))
            cur.execute("DELETE FROM finance.tax_records WHERE invoice_id = ANY(%s)", ([_INVOICE_A, _INVOICE_C],))
            cur.execute("DELETE FROM finance.invoice_lines WHERE invoice_id = ANY(%s)", ([_INVOICE_A, _INVOICE_C],))
            cur.execute("DELETE FROM finance.payment_allocations WHERE invoice_id = ANY(%s)", ([_INVOICE_A, _INVOICE_C],))
            cur.execute(
                "DELETE FROM finance.refunds WHERE payment_id = ANY(%s) OR reason_code LIKE 'branch-refund:%%'",
                ([_PAYMENT_A, _PAYMENT_C],),
            )
            cur.execute("DELETE FROM finance.invoices WHERE id = ANY(%s)", ([_INVOICE_A, _INVOICE_C],))
            cur.execute("DELETE FROM finance.billing_parties WHERE id = %s", (_BILLING_A,))
            cur.execute("DELETE FROM finance.payment_events WHERE payment_id = %s", (_PAYMENT_C,))
            cur.execute("DELETE FROM finance.payments WHERE id = %s", (_PAYMENT_C,))
        conn.commit()


def _ensure_p4d2_base_state() -> None:
    _validate_safe_p4d_database()
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code
                ) VALUES
                    (%s,'P4D Runtime Org A','p4d-runtime-org-a','basic',true,10,'INR'),
                    (%s,'P4D Runtime Org B','p4d-runtime-org-b','basic',true,10,'INR')
                ON CONFLICT (id) DO NOTHING
                """,
                (_ORG_A, _ORG_B),
            )
            cur.execute(
                """
                SELECT id,name,slug,tier,is_active,max_branches,default_currency_code
                FROM public.organizations
                WHERE id = ANY(%s)
                ORDER BY id
                """,
                ([_ORG_A, _ORG_B],),
            )
            assert cur.fetchall() == [
                (_ORG_A, "P4D Runtime Org A", "p4d-runtime-org-a", "basic", True, 10, "INR"),
                (_ORG_B, "P4D Runtime Org B", "p4d-runtime-org-b", "basic", True, 10, "INR"),
            ]
            cur.execute(
                """
                INSERT INTO finance.legal_entities(id,code,legal_name,status)
                VALUES
                    (%s,'P4D_RUNTIME_A','P4D Runtime Entity A','active'),
                    (%s,'P4D_RUNTIME_B','P4D Runtime Entity B','active')
                ON CONFLICT (id) DO NOTHING
                """,
                (_ENTITY_A, _ENTITY_B),
            )
            cur.execute(
                """
                SELECT id,code,legal_name,status
                FROM finance.legal_entities
                WHERE id = ANY(%s)
                ORDER BY id
                """,
                ([_ENTITY_A, _ENTITY_B],),
            )
            assert cur.fetchall() == [
                (_ENTITY_A, "P4D_RUNTIME_A", "P4D Runtime Entity A", "active"),
                (_ENTITY_B, "P4D_RUNTIME_B", "P4D Runtime Entity B", "active"),
            ]
            cur.execute(
                """
                INSERT INTO finance.payments(
                    id,organization_id,legal_entity_id,provider_code,
                    provider_payment_ref,amount,currency_code,status
                ) VALUES
                    (%s,%s,%s,'runtime_provider','runtime_payment_a',100,'INR','captured'),
                    (%s,%s,%s,'runtime_provider','runtime_payment_b',100,'INR','captured')
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    _PAYMENT_A,
                    _ORG_A,
                    _ENTITY_A,
                    _PAYMENT_B,
                    _ORG_B,
                    _ENTITY_B,
                ),
            )
            cur.execute(
                """
                SELECT id,organization_id,legal_entity_id,provider_code,provider_payment_ref,
                       amount::text,currency_code,status
                FROM finance.payments
                WHERE id = ANY(%s)
                ORDER BY id
                """,
                ([_PAYMENT_A, _PAYMENT_B],),
            )
            assert cur.fetchall() == [
                (_PAYMENT_A, _ORG_A, _ENTITY_A, "runtime_provider", "runtime_payment_a", "100.00", "INR", "captured"),
                (_PAYMENT_B, _ORG_B, _ENTITY_B, "runtime_provider", "runtime_payment_b", "100.00", "INR", "captured"),
            ]
            cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_A),))
            cur.execute(
                """
                INSERT INTO public.org_branches(
                    id,org_id,branch_name,branch_code,internal_slug,country_code,currency_code
                ) VALUES (%s,%s,'P4D Runtime Branch A','P4D-A','p4d-runtime-a','IN','INR')
                ON CONFLICT (id) DO NOTHING
                """,
                (_BRANCH_A, _ORG_A),
            )
            cur.execute(
                """
                SELECT id,org_id,branch_name,branch_code,internal_slug,country_code,currency_code
                FROM public.org_branches
                WHERE id = %s
                """,
                (_BRANCH_A,),
            )
            assert cur.fetchone() == (_BRANCH_A, _ORG_A, "P4D Runtime Branch A", "P4D-A", "p4d-runtime-a", "IN", "INR")
            cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_B),))
            cur.execute(
                """
                INSERT INTO public.org_branches(
                    id,org_id,branch_name,branch_code,internal_slug,country_code,currency_code
                ) VALUES (%s,%s,'P4D Runtime Branch B','P4D-B','p4d-runtime-b','IN','INR')
                ON CONFLICT (id) DO NOTHING
                """,
                (_BRANCH_B, _ORG_B),
            )
            cur.execute(
                """
                SELECT id,org_id,branch_name,branch_code,internal_slug,country_code,currency_code
                FROM public.org_branches
                WHERE id = %s
                """,
                (_BRANCH_B,),
            )
            assert cur.fetchone() == (_BRANCH_B, _ORG_B, "P4D Runtime Branch B", "P4D-B", "p4d-runtime-b", "IN", "INR")
        conn.commit()

@pytest.fixture(autouse=True)
def _fresh_obligation_state() -> None:
    _cleanup_p4d2_rows()
    _ensure_p4d2_base_state()


def _subscription_fixture(invoice_id: uuid.UUID, branch_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str, str]:
    if invoice_id == _INVOICE_C and branch_id == _BRANCH_C:
        return _SUBSCRIPTION_C_C, _MEMBER_C_C, _PLAN_C_C, "P4D2-SUB-C-C", "P4D2-PLAN-C-C"
    if invoice_id == _INVOICE_C:
        return _SUBSCRIPTION_C_A, _MEMBER_C_A, _PLAN_C_A, "P4D2-SUB-C-A", "P4D2-PLAN-C-A"
    return _SUBSCRIPTION_A, _MEMBER_A, _PLAN_A, "P4D2-SUB-A", "P4D2-PLAN-A"


def _semantic_row(cur) -> dict[str, object]:
    row = cur.fetchone()
    assert row is not None
    return {column.name: value for column, value in zip(cur.description, row)}


def _record_refund_obligation_binding(invoice_id: uuid.UUID, subscription_id: uuid.UUID) -> dict[str, object]:
    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_A),))
            cur.execute("SELECT * FROM app_secure.record_refund_obligation_binding(%s,%s)", (invoice_id, subscription_id))
            binding = _semantic_row(cur)
        conn.commit()
        return binding


def _record_member_subscription_checkout_binding(
    subscription_id: uuid.UUID,
    invoice_id: uuid.UUID,
    checkout_intent_id: uuid.UUID,
) -> dict[str, object]:
    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_A),))
            cur.execute(
                "SELECT * FROM app_secure.record_member_subscription_checkout_binding(%s,%s,%s)",
                (subscription_id, invoice_id, checkout_intent_id),
            )
            binding = _semantic_row(cur)
        conn.commit()
        return binding


def _persisted_refund_obligation_binding(invoice_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str, uuid.UUID] | None:
    return _security_owner_fetchone(
        """
        SELECT invoice_id, organization_id, branch_id, source_table, source_id
        FROM finance.refund_obligation_bindings
        WHERE invoice_id = %s
        """,
        (invoice_id,),
    )


def _persisted_checkout_binding(subscription_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID] | None:
    return _security_owner_fetchone(
        """
        SELECT subscription_id, invoice_id, checkout_intent_id, organization_id
        FROM finance.member_subscription_checkout_bindings
        WHERE subscription_id = %s
        """,
        (subscription_id,),
    )


def _tenant_fetchone(org_id: uuid.UUID, sql: str, params: tuple[object, ...] = ()):
    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(org_id),))
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
        return row


def _seed_org_b_subscription(
    *,
    branch_profile_id: uuid.UUID = _BRANCH_PROFILE_A,
    plan_tax_profile_id: uuid.UUID = _PLAN_TAX_PROFILE_A,
    tax_code_id: uuid.UUID = _TAX_CODE_A,
) -> None:
    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_catalog.set_config('app.current_role', 'owner', true)")
            cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_B),))
            cur.execute(
                """
                INSERT INTO public.members(
                    id,org_id,home_branch_id,member_uid,member_number,name,status,is_active,is_migrated
                ) VALUES (%s,%s,%s,'P4D2B001',430001,'P4D2 Runtime Member B','active',true,false)
                ON CONFLICT (id) DO NOTHING
                """,
                (_MEMBER_B, _ORG_B, _BRANCH_B),
            )
            cur.execute(
                """
                INSERT INTO public.membership_plans(
                    id,org_id,branch_id,plan_code,name,price,currency,
                    duration_value,duration_unit,max_members,status
                ) VALUES (%s,%s,%s,'P4D2-PLAN-B','P4D2 Runtime Plan B',100,'INR',1,'months',1,'active')
                ON CONFLICT (id) DO NOTHING
                """,
                (_PLAN_B, _ORG_B, _BRANCH_B),
            )
            cur.execute(
                """
                INSERT INTO public.member_subscriptions_v2(
                    id,org_id,branch_id,membership_plan_id,primary_member_id,subscription_code,
                    start_date,end_date,status,price_snapshot,currency_code,
                    duration_value_snapshot,duration_unit_snapshot,max_members_snapshot
                ) VALUES (%s,%s,%s,%s,%s,'P4D2-SUB-B',current_date,current_date + interval '1 month','pending',100,'INR',1,'months',1)
                ON CONFLICT (id) DO NOTHING
                """,
                (_SUBSCRIPTION_B, _ORG_B, _BRANCH_B, _PLAN_B, _MEMBER_B),
            )
        conn.commit()


def _fixture_invoice_business_key(invoice_id: uuid.UUID, branch_id: uuid.UUID = _BRANCH_A) -> str:
    if invoice_id == _INVOICE_A:
        return "A-PRIMARY"
    if invoice_id == _INVOICE_C and branch_id == _BRANCH_C:
        return "C-BRANCH-C"
    if invoice_id == _INVOICE_C:
        return "C-SECONDARY"
    return f"UUID-{invoice_id.hex}"


def _seed_invoice(
    invoice_id: uuid.UUID,
    *,
    branch_id: uuid.UUID = _BRANCH_A,
    bind_refund_obligation: bool = True,
    create_invoice: bool = True,
) -> None:
    subscription_id, member_id, plan_id, subscription_code, plan_code = _subscription_fixture(invoice_id, branch_id)
    invoice_key = _fixture_invoice_business_key(invoice_id, branch_id)
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            if branch_id == _BRANCH_C:
                cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_A),))
                cur.execute(
                    """
                    INSERT INTO public.org_branches(
                        id,org_id,branch_name,branch_code,internal_slug,country_code,currency_code
                    ) VALUES (%s,%s,'P4D2 Runtime Branch C','P4D2-C','p4d2-runtime-c','IN','INR')
                    ON CONFLICT (id) DO UPDATE SET
                        org_id=EXCLUDED.org_id,
                        branch_name=EXCLUDED.branch_name,
                        branch_code=EXCLUDED.branch_code,
                        internal_slug=EXCLUDED.internal_slug,
                        country_code=EXCLUDED.country_code,
                        currency_code=EXCLUDED.currency_code
                    """,
                    (_BRANCH_C, _ORG_A),
                )
            cur.execute(
                """
                INSERT INTO finance.gst_registrations(
                    id,legal_entity_id,gstin,state_code,state_name,registered_address,status
                ) VALUES (%s,%s,'29ABCDE1234F1Z5','29','Karnataka','Bengaluru','active')
                ON CONFLICT (id) DO NOTHING
                """,
                (_GST_A, _ENTITY_A),
            )
            cur.execute(
                """
                INSERT INTO finance.divisions(id,legal_entity_id,code,name,status)
                VALUES (%s,%s,'VS','P4D2 Division','active')
                ON CONFLICT (id) DO NOTHING
                """,
                (_DIVISION_A, _ENTITY_A),
            )
            cur.execute(
                """
                INSERT INTO finance.brands(id,legal_entity_id,division_id,code,name,status)
                VALUES (%s,%s,%s,'DS','P4D2 Brand','active')
                ON CONFLICT (id) DO NOTHING
                """,
                (_BRAND_A, _ENTITY_A, _DIVISION_A),
            )
    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_catalog.set_config('app.current_role', 'owner', true)")
            cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_A),))
            cur.execute(
                """
                INSERT INTO public.members(
                    id,org_id,home_branch_id,member_uid,member_number,name,status,is_active,is_migrated
                ) VALUES (%s,%s,%s,%s,%s,'P4D2 Runtime Member','active',true,false)
                ON CONFLICT (id) DO NOTHING
                """,
                (member_id, _ORG_A, branch_id, f"P4D2{member_id.hex[-6:]}", 420000 + int(member_id.hex[-2:], 16)),
            )
            cur.execute(
                """
                INSERT INTO public.membership_plans(
                    id,org_id,branch_id,plan_code,name,price,currency,
                    duration_value,duration_unit,max_members,status
                ) VALUES (%s,%s,%s,%s,'P4D2 Runtime Plan',100,'INR',1,'months',1,'active')
                ON CONFLICT (id) DO NOTHING
                """,
                (plan_id, _ORG_A, branch_id, plan_code),
            )
            cur.execute(
                """
                INSERT INTO public.member_subscriptions_v2(
                    id,org_id,branch_id,membership_plan_id,primary_member_id,subscription_code,
                    start_date,end_date,status,price_snapshot,currency_code,
                    duration_value_snapshot,duration_unit_snapshot,max_members_snapshot
                ) VALUES (%s,%s,%s,%s,%s,%s,current_date,current_date + interval '1 month','pending',100,'INR',1,'months',1)
                ON CONFLICT (id) DO NOTHING
                """,
                (subscription_id, _ORG_A, branch_id, plan_id, member_id, subscription_code),
            )
        conn.commit()

    if create_invoice:
        with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO finance.billing_parties(
                        id,organization_id,buyer_kind,member_id,billing_name,party_type,gst_treatment,
                        billing_address,place_of_supply_state_code,status
                    ) VALUES (%s,%s,'organization',NULL,'P4D2 Buyer','individual','b2c','Bengaluru','29','active')
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (_BILLING_A, _ORG_A),
                )
                cur.execute(
                    """
                    INSERT INTO finance.invoices(
                        id,organization_id,billing_party_id,legal_entity_id,gst_registration_id,
                        division_id,brand_id,financial_year,official_invoice_number,
                        brand_reference,status,currency_code,seller_legal_name,seller_gstin,
                        seller_registered_address,seller_state_code,buyer_billing_name,
                        buyer_address,buyer_place_of_supply_state_code,buyer_gst_treatment,
                        gst_supply_type,subtotal_amount,taxable_amount,total_tax_amount,
                        grand_total_amount,issued_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,'2026',%s,
                        %s,'issued','INR','P4D2 Seller',
                        '29ABCDE1234F1Z5','Bengaluru','29','P4D2 Buyer','Bengaluru',
                        '29','b2c','intra_state',100,100,0,100,clock_timestamp()
                    )
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        invoice_id,
                        _ORG_A,
                        _BILLING_A,
                        _ENTITY_A,
                        _GST_A,
                        _DIVISION_A,
                        _BRAND_A,
                        f"P4D2-INV-{invoice_key}",
                        f"P4D2-BR-{invoice_key}",
                    ),
                )
            conn.commit()
    if bind_refund_obligation:
        binding = _record_refund_obligation_binding(invoice_id, subscription_id)
        assert binding == {
            "invoice_id": invoice_id,
            "organization_id": _ORG_A,
            "branch_id": branch_id,
            "source_table": "member_subscriptions_v2",
            "source_id": subscription_id,
            "inserted": True,
            "replayed": False,
        }




def _establish_checkout_configuration(
    *,
    branch_profile_id: uuid.UUID = _BRANCH_PROFILE_A,
    plan_tax_profile_id: uuid.UUID = _PLAN_TAX_PROFILE_A,
    branch_id: uuid.UUID = _BRANCH_A,
    plan_id: uuid.UUID = _PLAN_A,
    tax_code_id: uuid.UUID = _TAX_CODE_A,
    tax_code_code: str = "P4D2_STANDARD_GST",
    pricing_mode: str = "tax_exclusive",
    effective_from: str = "2026-01-01",
    effective_until: str | None = None,
) -> list[dict[str, object]]:
    with _connect(_CONFIG_LOGIN, "FINANCE_CONFIG_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM app_secure.establish_finance_tax_code(%s,%s,%s,%s,%s,%s)",
                (tax_code_id, tax_code_code, "P4D2 standard GST", "9999", "gst", 1800),
            )
            tax_code = _semantic_row(cur)
            cur.execute(
                "SELECT * FROM app_secure.establish_branch_accounting_profile(%s,%s,%s,%s,%s,%s,%s,%s)",
                (branch_profile_id, branch_id, _ENTITY_A, _GST_A, _DIVISION_A, _BRAND_A, effective_from, effective_until),
            )
            branch_profile = _semantic_row(cur)
            cur.execute(
                "SELECT * FROM app_secure.establish_membership_plan_tax_profile(%s,%s,%s,%s,%s,%s)",
                (plan_tax_profile_id, plan_id, tax_code_id, pricing_mode, effective_from, effective_until),
            )
            plan_profile = _semantic_row(cur)
        conn.commit()
    return [tax_code, branch_profile, plan_profile]


def _direct_config_table_insert(login: str, password_env: str, table_name: str) -> None:
    with _connect(login, password_env) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("INSERT INTO finance.{} DEFAULT VALUES").format(sql.Identifier(table_name))
            )
        conn.commit()


def _direct_valid_tax_code_insert(login: str, password_env: str, tax_code_id: uuid.UUID) -> None:
    with _connect(login, password_env) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.tax_codes(id, code, description, hsn_sac, tax_type, gst_rate_basis_points, status)
                VALUES (%s,%s,'P4D2 direct tax-code bypass attempt','9999','gst',1800,'active')
                """,
                (tax_code_id, f"P4D2_DIRECT_{tax_code_id.hex[-12:].upper()}"),
            )
        conn.commit()


def _assert_tax_code_row_is_canonical(
    row: dict[str, object],
    tax_code_id: uuid.UUID = _TAX_CODE_A,
    tax_code_code: str = "P4D2_STANDARD_GST",
) -> None:
    assert row["tax_code_id"] == tax_code_id
    assert row["code"] == tax_code_code
    assert row["inserted"] is not row["replayed"]


def _allocate(
    payment_id: uuid.UUID = _PAYMENT_A,
    *,
    invoice_id: uuid.UUID = _INVOICE_A,
    amount: str = "80.00",
    branch_id: uuid.UUID = _BRANCH_A,
) -> None:
    _seed_invoice(invoice_id, branch_id=branch_id)
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.payment_allocations(payment_id,invoice_id,allocated_amount)
                VALUES (%s,%s,%s)
                ON CONFLICT (payment_id, invoice_id) DO UPDATE
                SET allocated_amount=EXCLUDED.allocated_amount
                """,
                (payment_id, invoice_id, Decimal(amount)),
            )
        conn.commit()


def _seed_second_org_a_payment(amount: str = "10.00", *, branch_id: uuid.UUID = _BRANCH_A) -> None:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.payments(
                    id,organization_id,legal_entity_id,provider_code,
                    provider_payment_ref,amount,currency_code,status
                ) VALUES (%s,%s,%s,'runtime_provider','runtime_payment_c',100,'INR','captured')
                """,
                (_PAYMENT_C, _ORG_A, _ENTITY_A),
            )
        conn.commit()
    _allocate(_PAYMENT_C, invoice_id=_INVOICE_C, amount=amount, branch_id=branch_id)


def _insert_source(source_id: uuid.UUID, *, branch_id: uuid.UUID = _BRANCH_A, tenant_id: uuid.UUID = _ORG_A, payload: dict[str, object] | None = None) -> None:
    payload = payload or {}
    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_catalog.set_config('app.current_role', 'owner', true)")
            cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(tenant_id),))
            cur.execute(
                """
                INSERT INTO public.branch_outbox_events(
                    outbox_id,branch_id,tenant_id,event_type,payload,correlation_id,
                    status,attempt_count,max_attempts,leased_by,leased_until
                ) VALUES (%s,%s,%s,'branch.refund_required',%s,%s,'pending',0,5,NULL,NULL)
                """,
                (source_id, branch_id, tenant_id, Jsonb(payload), _CORRELATION_C),
            )
        conn.commit()


def _claim_source(source_id: uuid.UUID, worker_id: uuid.UUID = _WORKER_A) -> None:
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT outbox_id
                    FROM public.branch_outbox_events
                    WHERE outbox_id = %s
                      AND attempt_count < max_attempts
                      AND process_after <= pg_catalog.clock_timestamp()
                      AND (
                          status = 'pending'
                          OR (
                              status = 'processing'
                              AND leased_until <= pg_catalog.clock_timestamp()
                          )
                      )
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE public.branch_outbox_events AS o
                SET status = 'processing',
                    attempt_count = o.attempt_count + 1,
                    last_attempted_at = pg_catalog.clock_timestamp(),
                    last_error = NULL,
                    leased_by = %s,
                    leased_until = pg_catalog.clock_timestamp() + interval '10 minutes'
                FROM candidates
                WHERE o.outbox_id = candidates.outbox_id
                RETURNING o.outbox_id
                """,
                (source_id, worker_id),
            )
            assert cur.fetchall() == [(source_id,)]
        conn.commit()


def _resolve(source_id: uuid.UUID = _SOURCE_C, worker_id: uuid.UUID = _WORKER_A):
    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM app_secure.resolve_branch_refund_required(%s,%s)", (source_id, worker_id))
            row = cur.fetchone()
        conn.commit()
        return row


def test_fixture_invoice_business_keys_are_semantic_and_collision_free() -> None:
    assert _fixture_invoice_business_key(_INVOICE_A) == "A-PRIMARY"
    assert _fixture_invoice_business_key(_INVOICE_C) == "C-SECONDARY"
    assert _fixture_invoice_business_key(_INVOICE_C, _BRANCH_C) == "C-BRANCH-C"
    assert len({_fixture_invoice_business_key(_INVOICE_A), _fixture_invoice_business_key(_INVOICE_C), _fixture_invoice_business_key(_INVOICE_C, _BRANCH_C)}) == 3


def test_distinct_fixture_invoices_can_coexist_under_official_number_constraint() -> None:
    _seed_invoice(_INVOICE_A, bind_refund_obligation=False)
    _seed_invoice(_INVOICE_C, bind_refund_obligation=False)

    assert _security_owner_fetchone(
        """
        SELECT array_agg(official_invoice_number ORDER BY official_invoice_number)
        FROM finance.invoices
        WHERE id = ANY(%s)
        """,
        ([_INVOICE_A, _INVOICE_C],),
    )[0] == ["P4D2-INV-A-PRIMARY", "P4D2-INV-C-SECONDARY"]


def test_same_invoice_different_source_fixture_does_not_create_second_invoice() -> None:
    _seed_invoice(_INVOICE_A, bind_refund_obligation=False)
    _seed_invoice(_INVOICE_C, bind_refund_obligation=False, create_invoice=False)

    assert _security_owner_fetchone("SELECT count(*) FROM finance.invoices WHERE id = ANY(%s)", ([_INVOICE_A, _INVOICE_C],))[0] == 1
    assert _tenant_fetchone(_ORG_A, "SELECT count(*) FROM public.member_subscriptions_v2 WHERE id = %s", (_SUBSCRIPTION_C_A,))[0] == 1


def test_fixture_symbol_topology_constants_are_defined_and_unique() -> None:
    critical_symbols = {
        "org_a": _ORG_A,
        "org_b": _ORG_B,
        "branch_a": _BRANCH_A,
        "branch_b": _BRANCH_B,
        "branch_c": _BRANCH_C,
        "member_a": _MEMBER_A,
        "member_b": _MEMBER_B,
        "member_c_a": _MEMBER_C_A,
        "member_c_c": _MEMBER_C_C,
        "plan_a": _PLAN_A,
        "plan_b": _PLAN_B,
        "plan_c_a": _PLAN_C_A,
        "plan_c_c": _PLAN_C_C,
        "subscription_a": _SUBSCRIPTION_A,
        "subscription_b": _SUBSCRIPTION_B,
        "subscription_c_a": _SUBSCRIPTION_C_A,
        "subscription_c_c": _SUBSCRIPTION_C_C,
        "invoice_a": _INVOICE_A,
        "invoice_c": _INVOICE_C,
        "source_c": _SOURCE_C,
        "source_d": _SOURCE_D,
        "payment_c": _PAYMENT_C,
    }
    assert len(set(critical_symbols.values())) == len(critical_symbols)
    assert _ORG_A != _ORG_B
    assert _BRANCH_A != _BRANCH_B
    assert _BRANCH_C not in {_BRANCH_A, _BRANCH_B}


def test_org_b_source_graph_uses_org_b_branch_and_parents() -> None:
    _seed_org_b_subscription()

    assert _tenant_fetchone(_ORG_B, "SELECT org_id FROM public.org_branches WHERE id = %s", (_BRANCH_B,)) == (_ORG_B,)
    assert _tenant_fetchone(_ORG_B, "SELECT org_id, home_branch_id FROM public.members WHERE id = %s", (_MEMBER_B,)) == (
        _ORG_B,
        _BRANCH_B,
    )
    assert _tenant_fetchone(_ORG_B, "SELECT org_id, branch_id FROM public.membership_plans WHERE id = %s", (_PLAN_B,)) == (
        _ORG_B,
        _BRANCH_B,
    )
    assert _tenant_fetchone(
        _ORG_B,
        """
        SELECT org_id, branch_id, membership_plan_id, primary_member_id
        FROM public.member_subscriptions_v2
        WHERE id = %s
        """,
        (_SUBSCRIPTION_B,),
    ) == (_ORG_B, _BRANCH_B, _PLAN_B, _MEMBER_B)


def test_cross_tenant_fixture_uses_org_a_invoice_and_org_b_source_with_tenant_isolation() -> None:
    _seed_invoice(_INVOICE_A, bind_refund_obligation=False)
    _seed_org_b_subscription()

    assert _security_owner_fetchone("SELECT organization_id FROM finance.invoices WHERE id = %s", (_INVOICE_A,)) == (_ORG_A,)
    assert _tenant_fetchone(_ORG_B, "SELECT org_id, branch_id FROM public.member_subscriptions_v2 WHERE id = %s", (_SUBSCRIPTION_B,)) == (
        _ORG_B,
        _BRANCH_B,
    )
    assert _tenant_fetchone(_ORG_A, "SELECT org_id, branch_id FROM public.member_subscriptions_v2 WHERE id = %s", (_SUBSCRIPTION_B,)) is None
    assert _tenant_fetchone(_ORG_A, "SELECT org_id, home_branch_id FROM public.members WHERE id = %s", (_MEMBER_B,)) is None
    assert _ORG_A != _ORG_B
    assert _BRANCH_A != _BRANCH_B


def test_branch_mismatch_fixture_uses_same_org_distinct_branches() -> None:
    _seed_invoice(_INVOICE_A, bind_refund_obligation=False)
    _seed_invoice(_INVOICE_C, branch_id=_BRANCH_C, bind_refund_obligation=False, create_invoice=False)

    assert _security_owner_fetchone("SELECT organization_id FROM finance.invoices WHERE id = %s", (_INVOICE_A,)) == (_ORG_A,)
    assert _tenant_fetchone(_ORG_A, "SELECT org_id FROM public.org_branches WHERE id = %s", (_BRANCH_C,)) == (_ORG_A,)
    assert _tenant_fetchone(_ORG_A, "SELECT org_id, branch_id FROM public.member_subscriptions_v2 WHERE id = %s", (_SUBSCRIPTION_C_C,)) == (
        _ORG_A,
        _BRANCH_C,
    )
    assert _BRANCH_A != _BRANCH_C


def test_refund_obligation_binding_return_contract_first_replay_and_conflict() -> None:
    _seed_invoice(_INVOICE_A, bind_refund_obligation=False)

    first = _record_refund_obligation_binding(_INVOICE_A, _SUBSCRIPTION_A)
    assert first == {
        "invoice_id": _INVOICE_A,
        "organization_id": _ORG_A,
        "branch_id": _BRANCH_A,
        "source_table": "member_subscriptions_v2",
        "source_id": _SUBSCRIPTION_A,
        "inserted": True,
        "replayed": False,
    }
    assert _persisted_refund_obligation_binding(_INVOICE_A) == (
        _INVOICE_A,
        _ORG_A,
        _BRANCH_A,
        "member_subscriptions_v2",
        _SUBSCRIPTION_A,
    )

    replay = _record_refund_obligation_binding(_INVOICE_A, _SUBSCRIPTION_A)
    assert replay == first | {"inserted": False, "replayed": True}
    assert _persisted_refund_obligation_binding(_INVOICE_A) == (
        _INVOICE_A,
        _ORG_A,
        _BRANCH_A,
        "member_subscriptions_v2",
        _SUBSCRIPTION_A,
    )

    _seed_invoice(_INVOICE_C, bind_refund_obligation=False, create_invoice=False)
    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as conn:
        with pytest.raises(psycopg.Error) as exc_info:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_A),))
                cur.execute("SELECT * FROM app_secure.record_refund_obligation_binding(%s,%s)", (_INVOICE_A, _SUBSCRIPTION_C_A))
        conn.rollback()
    assert exc_info.value.sqlstate == "23505"
    assert _persisted_refund_obligation_binding(_INVOICE_A) == (
        _INVOICE_A,
        _ORG_A,
        _BRANCH_A,
        "member_subscriptions_v2",
        _SUBSCRIPTION_A,
    )


def test_refund_obligation_binding_cross_tenant_source_fails_closed() -> None:
    _seed_invoice(_INVOICE_A, bind_refund_obligation=False)
    _seed_org_b_subscription()

    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as conn:
        with pytest.raises(psycopg.Error) as exc_info:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_B),))
                cur.execute("SELECT * FROM app_secure.record_refund_obligation_binding(%s,%s)", (_INVOICE_A, _SUBSCRIPTION_B))
        conn.rollback()
    assert exc_info.value.sqlstate == "23514"
    assert _persisted_refund_obligation_binding(_INVOICE_A) is None


def test_finance_config_runtime_establishes_checkout_configuration_without_direct_dml() -> None:
    _seed_invoice(_INVOICE_A, bind_refund_obligation=False)

    first = _establish_checkout_configuration(
        branch_profile_id=_CONFIG_TEST1_BRANCH_PROFILE,
        plan_tax_profile_id=_CONFIG_TEST1_PLAN_TAX_PROFILE,
        tax_code_id=_CONFIG_TEST1_TAX_CODE,
        tax_code_code="P4D2_TEST101_GST",
        effective_from="2023-01-01",
        effective_until="2023-12-31",
    )
    _assert_tax_code_row_is_canonical(first[0], _CONFIG_TEST1_TAX_CODE, "P4D2_TEST101_GST")
    assert first[1:] == [
        {
            "profile_id": _CONFIG_TEST1_BRANCH_PROFILE,
            "organization_id": _ORG_A,
            "branch_id": _BRANCH_A,
            "inserted": True,
            "replayed": False,
        },
        {
            "profile_id": _CONFIG_TEST1_PLAN_TAX_PROFILE,
            "organization_id": _ORG_A,
            "membership_plan_id": _PLAN_A,
            "inserted": True,
            "replayed": False,
        },
    ]
    replay = _establish_checkout_configuration(
        branch_profile_id=_CONFIG_TEST1_BRANCH_PROFILE,
        plan_tax_profile_id=_CONFIG_TEST1_PLAN_TAX_PROFILE,
        tax_code_id=_CONFIG_TEST1_TAX_CODE,
        tax_code_code="P4D2_TEST101_GST",
        effective_from="2023-01-01",
        effective_until="2023-12-31",
    )
    assert [row["inserted"] for row in replay] == [False, False, False]
    assert [row["replayed"] for row in replay] == [True, True, True]
    assert _security_owner_fetchone(
        "SELECT organization_id, branch_id, configured_by FROM finance.branch_accounting_profiles WHERE id = %s",
        (_CONFIG_TEST1_BRANCH_PROFILE,),
    ) == (_ORG_A, _BRANCH_A, _CONFIG_LOGIN)
    assert _security_owner_fetchone(
        "SELECT organization_id, membership_plan_id, configured_by FROM finance.membership_plan_tax_profiles WHERE id = %s",
        (_CONFIG_TEST1_PLAN_TAX_PROFILE,),
    ) == (_ORG_A, _PLAN_A, _CONFIG_LOGIN)


def test_finance_config_runtime_has_only_bounded_function_authority() -> None:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD", autocommit=True) as conn:
        with conn.cursor() as cur:
            for login in (
                _APP_LOGIN,
                _WORKER_LOGIN,
                _MAINTENANCE_LOGIN,
                _CONFIG_LOGIN,
            ):
                for table_name, privilege in (
                    ("finance.branch_accounting_profiles", "INSERT"),
                    ("finance.branch_accounting_profiles", "UPDATE"),
                    ("finance.membership_plan_tax_profiles", "INSERT"),
                    ("finance.membership_plan_tax_profiles", "UPDATE"),
                    ("finance.tax_codes", "SELECT"),
                    ("finance.tax_codes", "INSERT"),
                    ("finance.tax_codes", "UPDATE"),
                    ("finance.tax_codes", "DELETE"),
                ):
                    cur.execute("SELECT pg_catalog.has_table_privilege(%s,%s,%s)", (login, table_name, privilege))
                    assert cur.fetchone()[0] is False
                cur.execute("SELECT pg_catalog.has_schema_privilege(%s,'finance','CREATE')", (login,))
                assert cur.fetchone()[0] is False
                cur.execute("SELECT rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = %s", (login,))
                assert cur.fetchone()[0] is False

    with pytest.raises(InsufficientPrivilege):
        _direct_config_table_insert(_CONFIG_LOGIN, "FINANCE_CONFIG_RUNTIME_PASSWORD", "branch_accounting_profiles")

    for login, password_env in (
        (_ADMIN_LOGIN, "MIGRATION_PASSWORD"),
        (_APP_LOGIN, "APP_RUNTIME_PASSWORD"),
        (_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD"),
        (_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD"),
        (_CONFIG_LOGIN, "FINANCE_CONFIG_RUNTIME_PASSWORD"),
    ):
        with pytest.raises(psycopg.Error):
            _direct_valid_tax_code_insert(login, password_env, uuid.uuid4())


def test_finance_config_conflicting_replay_and_overlap_fail_closed() -> None:
    _seed_invoice(_INVOICE_A, bind_refund_obligation=False)
    _establish_checkout_configuration(
        branch_profile_id=_CONFIG_TEST3_BRANCH_PROFILE,
        plan_tax_profile_id=_CONFIG_TEST3_PLAN_TAX_PROFILE,
        tax_code_id=_CONFIG_TEST3_TAX_CODE,
        tax_code_code="P4D2_TEST103_GST",
        effective_from="2022-01-01",
        effective_until="2022-12-31",
    )

    with _connect(_CONFIG_LOGIN, "FINANCE_CONFIG_RUNTIME_PASSWORD") as conn:
        with pytest.raises(psycopg.Error) as exc_info:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM app_secure.establish_membership_plan_tax_profile(%s,%s,%s,%s,%s,%s)",
                    (_CONFIG_TEST3_PLAN_TAX_PROFILE, _PLAN_A, _CONFIG_TEST3_TAX_CODE, "tax_inclusive", "2022-01-01", "2022-12-31"),
                )
        conn.rollback()
    assert exc_info.value.sqlstate == "23505"

    with _connect(_CONFIG_LOGIN, "FINANCE_CONFIG_RUNTIME_PASSWORD") as conn:
        with pytest.raises((ExclusionViolation, UniqueViolation)):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM app_secure.establish_branch_accounting_profile(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (_CONFIG_TEST3_BRANCH_PROFILE_CONFLICT, _BRANCH_A, _ENTITY_A, _GST_A, _DIVISION_A, _BRAND_A, "2022-06-01", "2022-12-31"),
                )
        conn.rollback()
    assert _security_owner_fetchone(
        "SELECT count(*) FROM finance.branch_accounting_profiles WHERE id = %s",
        (_CONFIG_TEST3_BRANCH_PROFILE,),
    )[0] == 1


def test_finance_config_cross_tenant_master_data_is_rejected() -> None:
    _seed_invoice(_INVOICE_A, bind_refund_obligation=False)
    _seed_org_b_subscription(
        branch_profile_id=_CONFIG_TEST4_BRANCH_PROFILE,
        plan_tax_profile_id=_CONFIG_TEST4_PLAN_TAX_PROFILE,
        tax_code_id=_CONFIG_TEST4_TAX_CODE,
    )

    with _connect(_CONFIG_LOGIN, "FINANCE_CONFIG_RUNTIME_PASSWORD") as conn:
        with pytest.raises(psycopg.Error) as exc_info:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM app_secure.establish_branch_accounting_profile(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (_CONFIG_TEST4_BRANCH_PROFILE, _BRANCH_B, _ENTITY_A, _GST_A, _DIVISION_A, _BRAND_A, "2026-01-01", None),
                )
        conn.rollback()
    assert exc_info.value.sqlstate == "23514"
    assert _security_owner_fetchone("SELECT count(*) FROM finance.branch_accounting_profiles WHERE id = %s", (_CONFIG_TEST4_BRANCH_PROFILE,))[0] == 0



def test_member_subscription_checkout_binding_return_contract_first_replay_and_conflict() -> None:
    _seed_invoice(_INVOICE_A, bind_refund_obligation=False)
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.payments(
                    id,organization_id,legal_entity_id,provider_code,
                    provider_payment_ref,amount,currency_code,status
                ) VALUES (%s,%s,%s,'runtime_provider','runtime_payment_c',100,'INR','captured')
                """,
                (_PAYMENT_C, _ORG_A, _ENTITY_A),
            )
        conn.commit()
    _seed_invoice(_INVOICE_C, bind_refund_obligation=False)

    first = _record_member_subscription_checkout_binding(_SUBSCRIPTION_A, _INVOICE_A, _PAYMENT_A)
    assert first == {
        "subscription_id": _SUBSCRIPTION_A,
        "invoice_id": _INVOICE_A,
        "checkout_intent_id": _PAYMENT_A,
        "organization_id": _ORG_A,
        "inserted": True,
        "replayed": False,
    }
    assert _persisted_checkout_binding(_SUBSCRIPTION_A) == (_SUBSCRIPTION_A, _INVOICE_A, _PAYMENT_A, _ORG_A)

    replay = _record_member_subscription_checkout_binding(_SUBSCRIPTION_A, _INVOICE_A, _PAYMENT_A)
    assert replay == first | {"inserted": False, "replayed": True}
    assert _persisted_checkout_binding(_SUBSCRIPTION_A) == (_SUBSCRIPTION_A, _INVOICE_A, _PAYMENT_A, _ORG_A)

    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as conn:
        with pytest.raises(psycopg.Error) as exc_info:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_catalog.set_config('app.current_org_id', %s, true)", (str(_ORG_A),))
                cur.execute(
                    "SELECT * FROM app_secure.record_member_subscription_checkout_binding(%s,%s,%s)",
                    (_SUBSCRIPTION_A, _INVOICE_C, _PAYMENT_C),
                )
        conn.rollback()
    assert exc_info.value.sqlstate == "23505"
    assert _persisted_checkout_binding(_SUBSCRIPTION_A) == (_SUBSCRIPTION_A, _INVOICE_A, _PAYMENT_A, _ORG_A)


def test_payload_refund_required_false_cannot_suppress_valid_event() -> None:
    _allocate(amount="80.00")
    _insert_source(_SOURCE_C, payload={"refund_required": False, "amount": "9999", "currency": "USD"})
    _claim_source(_SOURCE_C)

    row = _resolve()

    assert row[0] == "command_materialized"
    assert row[3] == _PAYMENT_A
    assert row[5] == Decimal("80.00")
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refunds WHERE reason_code LIKE 'branch-refund:%'")[0] == 1
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refund_execution_commands")[0] == 1


def test_actual_producer_payload_context_still_evaluates() -> None:
    _allocate(amount="80.00")
    _insert_source(_SOURCE_C, payload={"refund_policy": "prorated", "from_status": "operational", "to_status": "closed"})
    _claim_source(_SOURCE_C)

    row = _resolve()

    assert row[0] == "command_materialized"
    assert row[3] == _PAYMENT_A
    assert row[5] == Decimal("80.00")


def test_zero_refundable_payment_creates_no_fabricated_obligation() -> None:
    _insert_source(_SOURCE_C)
    _claim_source(_SOURCE_C)

    row = _resolve()

    assert row[0] == "no_refundable_payment"
    assert row[2] is None
    assert row[5] == 0
    assert row[6] is None
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refunds WHERE reason_code LIKE 'branch-refund:%'")[0] == 0


def test_cross_branch_same_org_payment_is_not_refunded_for_branch_a_event() -> None:
    _seed_second_org_a_payment(amount="80.00", branch_id=_BRANCH_C)
    _insert_source(_SOURCE_C)
    _claim_source(_SOURCE_C)

    row = _resolve()

    assert row[0] == "no_refundable_payment"
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refunds WHERE reason_code LIKE 'branch-refund:%'")[0] == 0
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refund_execution_commands")[0] == 0


def test_exact_branch_bound_payment_is_selected_despite_unrelated_org_payment() -> None:
    _allocate(amount="80.00", branch_id=_BRANCH_A)
    _seed_second_org_a_payment(amount="40.00", branch_id=_BRANCH_C)
    _insert_source(_SOURCE_C)
    _claim_source(_SOURCE_C)

    row = _resolve()

    assert row[0] == "command_materialized"
    assert row[3] == _PAYMENT_A
    assert row[5] == Decimal("80.00")


def test_mixed_branch_allocations_for_same_payment_fail_closed() -> None:
    _allocate(amount="60.00", branch_id=_BRANCH_A)
    _allocate(payment_id=_PAYMENT_A, invoice_id=_INVOICE_C, amount="20.00", branch_id=_BRANCH_C)
    _insert_source(_SOURCE_C)
    _claim_source(_SOURCE_C)

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM app_secure.resolve_branch_refund_required(%s,%s)", (_SOURCE_C, _WORKER_A))
        conn.rollback()

    assert _security_owner_fetchone("SELECT count(*) FROM finance.refunds WHERE reason_code LIKE 'branch-refund:%'")[0] == 0
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refund_execution_commands")[0] == 0


def test_exactly_one_refundable_payment_creates_authoritative_refund_and_command() -> None:
    _allocate(amount="80.00")
    _insert_source(_SOURCE_C, payload={"amount": "9999.99", "currency": "USD", "provider": "spoof", "payment_id": str(_PAYMENT_B)})
    _claim_source(_SOURCE_C)

    row = _resolve()

    assert row[0] == "command_materialized"
    assert row[3] == _PAYMENT_A
    assert row[4] == _ORG_A
    assert row[5] == Decimal("80.00")
    assert row[6] == "INR"
    assert row[7] == "runtime_provider"
    assert row[8] == "runtime_payment_a"
    assert row[9] == 1
    assert row[10:] == (False, False)
    assert _security_owner_fetchone(
        "SELECT payment_id,amount,currency_code,status,reason_code FROM finance.refunds WHERE id=%s",
        (row[1],),
    ) == (_PAYMENT_A, Decimal("80.00"), "INR", "requested", f"branch-refund:{_SOURCE_C}")
    assert _security_owner_fetchone(
        "SELECT refund_id,payment_id,amount,currency_code,source_id,status FROM finance.refund_execution_commands WHERE command_id=%s",
        (row[2],),
    ) == (row[1], _PAYMENT_A, Decimal("80.00"), "INR", _SOURCE_C, "pending")


def test_multiple_refundable_payments_fail_closed() -> None:
    _allocate(amount="80.00")
    _seed_second_org_a_payment(amount="10.00")
    _insert_source(_SOURCE_C)
    _claim_source(_SOURCE_C)

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM app_secure.resolve_branch_refund_required(%s,%s)", (_SOURCE_C, _WORKER_A))
        conn.rollback()

    assert _security_owner_fetchone("SELECT count(*) FROM finance.refunds WHERE reason_code LIKE 'branch-refund:%'")[0] == 0
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refund_execution_commands")[0] == 0


def test_partially_and_fully_refunded_payments_use_remaining_balance_only() -> None:
    _allocate(amount="80.00")
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.refunds(organization_id,payment_id,legal_entity_id,amount,currency_code,status,reason_code)
                VALUES (%s,%s,%s,30,'INR','succeeded','prior-partial')
                """,
                (_ORG_A, _PAYMENT_A, _ENTITY_A),
            )
        conn.commit()
    _insert_source(_SOURCE_C)
    _claim_source(_SOURCE_C)
    assert _resolve()[5] == Decimal("50.00")

    _cleanup_p4d2_rows()
    _ensure_p4d2_base_state()
    _allocate(amount="80.00")
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.refunds(organization_id,payment_id,legal_entity_id,amount,currency_code,status,reason_code)
                VALUES (%s,%s,%s,80,'INR','succeeded','prior-full')
                """,
                (_ORG_A, _PAYMENT_A, _ENTITY_A),
            )
        conn.commit()
    _insert_source(_SOURCE_C)
    _claim_source(_SOURCE_C)
    assert _resolve()[0] == "no_refundable_payment"


def test_cancelled_deterministic_refund_suppresses_command_generation() -> None:
    _allocate(amount="80.00")
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.refunds(organization_id,payment_id,legal_entity_id,amount,currency_code,status,reason_code)
                VALUES (%s,%s,%s,80,'INR','cancelled',%s)
                """,
                (_ORG_A, _PAYMENT_A, _ENTITY_A, f"branch-refund:{_SOURCE_C}"),
            )
        conn.commit()
    _insert_source(_SOURCE_C)
    _claim_source(_SOURCE_C)

    row = _resolve()

    assert row[0] == "cancelled_refund_suppressed"
    assert row[2] is None
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refund_execution_commands")[0] == 0


def test_duplicate_source_reason_on_two_payments_fails_closed_without_first_row_selection() -> None:
    _allocate(amount="80.00")
    _seed_second_org_a_payment(amount="10.00", branch_id=_BRANCH_A)
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.refunds(organization_id,payment_id,legal_entity_id,amount,currency_code,status,reason_code)
                VALUES
                    (%s,%s,%s,80,'INR','requested',%s),
                    (%s,%s,%s,10,'INR','requested',%s)
                """,
                (_ORG_A, _PAYMENT_A, _ENTITY_A, f"branch-refund:{_SOURCE_C}", _ORG_A, _PAYMENT_C, _ENTITY_A, f"branch-refund:{_SOURCE_C}"),
            )
        conn.commit()
    _insert_source(_SOURCE_C)
    _claim_source(_SOURCE_C)

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM app_secure.resolve_branch_refund_required(%s,%s)", (_SOURCE_C, _WORKER_A))
        conn.rollback()

    assert _security_owner_fetchone("SELECT count(*) FROM finance.refunds WHERE reason_code=%s", (f"branch-refund:{_SOURCE_C}",))[0] == 2
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refund_execution_commands WHERE source_id=%s", (_SOURCE_C,))[0] == 0


def test_duplicate_source_event_reuses_refund_and_command() -> None:
    _allocate(amount="80.00")
    _insert_source(_SOURCE_C)
    _claim_source(_SOURCE_C)

    first = _resolve()
    second = _resolve()

    assert second[0] == "command_materialized"
    assert second[1] == first[1]
    assert second[2] == first[2]
    assert second[10:] == (True, True)
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refunds WHERE reason_code=%s", (f"branch-refund:{_SOURCE_C}",))[0] == 1
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refund_execution_commands WHERE source_id=%s", (_SOURCE_C,))[0] == 1


def test_concurrent_duplicate_source_event_converges_to_one_command() -> None:
    _allocate(amount="80.00")
    _insert_source(_SOURCE_C)
    _claim_source(_SOURCE_C)
    barrier = threading.Barrier(2)

    def invoke(index: int):
        barrier.wait(timeout=5)
        return _resolve(_SOURCE_C, _WORKER_A)

    with ThreadPoolExecutor(max_workers=2) as executor:
        rows = list(executor.map(invoke, range(2)))

    assert rows[0][1] == rows[1][1]
    assert rows[0][2] == rows[1][2]
    assert sorted(row[10:] for row in rows) == [(False, False), (True, True)]
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refunds WHERE reason_code=%s", (f"branch-refund:{_SOURCE_C}",))[0] == 1


def test_concurrent_different_events_do_not_over_refund_same_payment() -> None:
    _allocate(amount="80.00")
    _insert_source(_SOURCE_C)
    _insert_source(_SOURCE_D)
    _claim_source(_SOURCE_C, _WORKER_A)
    _claim_source(_SOURCE_D, _WORKER_B)
    barrier = threading.Barrier(2)

    def invoke(source_id: uuid.UUID, worker_id: uuid.UUID):
        barrier.wait(timeout=5)
        return _resolve(source_id, worker_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        rows = list(executor.map(lambda args: invoke(*args), [(_SOURCE_C, _WORKER_A), (_SOURCE_D, _WORKER_B)]))

    outcomes = sorted(row[0] for row in rows)
    assert outcomes == ["command_materialized", "no_refundable_payment"]
    assert _security_owner_fetchone(
        "SELECT coalesce(sum(amount),0) FROM finance.refunds WHERE payment_id=%s AND status <> 'cancelled'",
        (_PAYMENT_A,),
    )[0] == Decimal("80.00")
    assert _security_owner_fetchone("SELECT count(*) FROM finance.refund_execution_commands")[0] == 1


def test_source_branch_tenant_mismatch_fails_closed() -> None:
    _allocate(amount="80.00")
    try:
        _insert_source(_SOURCE_C, branch_id=_BRANCH_A, tenant_id=_ORG_B)
    except psycopg.Error as exc:
        assert exc.sqlstate == "42501"
        return
    _claim_source(_SOURCE_C)

    with _connect(_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD") as conn:
        with pytest.raises(CheckViolation):
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM app_secure.resolve_branch_refund_required(%s,%s)", (_SOURCE_C, _WORKER_A))
        conn.rollback()


def test_payload_spoofing_financial_fields_is_ignored() -> None:
    _allocate(amount="80.00")
    _insert_source(
        _SOURCE_C,
        payload={
            "amount": "99999.99",
            "currency_code": "USD",
            "provider_code": "evil_provider",
            "provider_payment_ref": "evil_payment",
            "payment_id": str(_PAYMENT_B),
        },
    )
    _claim_source(_SOURCE_C)

    row = _resolve()

    assert row[3] == _PAYMENT_A
    assert row[5] == Decimal("80.00")
    assert row[6] == "INR"
    assert row[7] == "runtime_provider"
    assert row[8] == "runtime_payment_a"


def test_refund_obligation_binding_table_keeps_migration_owner_with_force_rls_zero_visibility() -> None:
    _allocate(amount="80.00")

    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pg_get_userbyid(c.relowner), c.relrowsecurity, c.relforcerowsecurity
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname='finance' AND c.relname='refund_obligation_bindings'
                """
            )
            assert cur.fetchone() == ("migration_owner", True, True)
            cur.execute(
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
                       array_agg(r.rolname ORDER BY r.rolname)
                FROM pg_catalog.pg_policy pol
                JOIN pg_catalog.pg_class c ON c.oid = pol.polrelid
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN LATERAL unnest(pol.polroles) AS role_oid(oid) ON true
                LEFT JOIN pg_catalog.pg_roles r ON r.oid = role_oid.oid
                WHERE n.nspname='finance' AND c.relname='refund_obligation_bindings'
                GROUP BY pol.polname, command
                """
            )
            assert cur.fetchall() == [
                ("p4d_refund_obligation_security_owner_insert", "INSERT", ["app_security_owner"]),
                ("p4d_refund_obligation_security_owner_select", "SELECT", ["app_security_owner"]),
            ]
            cur.execute("SELECT count(*) FROM finance.refund_obligation_bindings")
            assert cur.fetchone()[0] == 0
            cur.execute(
                """
                SELECT pg_catalog.has_table_privilege('app_security_owner','finance.refund_obligation_bindings','SELECT'),
                       pg_catalog.has_table_privilege('app_security_owner','finance.refund_obligation_bindings','INSERT'),
                       pg_catalog.has_table_privilege('app_security_owner','finance.refund_obligation_bindings','UPDATE'),
                       pg_catalog.has_table_privilege('app_security_owner','finance.refund_obligation_bindings','DELETE')
                """
            )
            assert cur.fetchone() == (True, True, False, False)
            cur.execute("SET LOCAL ROLE app_security_owner")
            cur.execute("SELECT count(*) FROM finance.refund_obligation_bindings")
            assert cur.fetchone()[0] == 1


def test_worker_runtime_gets_resolver_only_without_direct_finance_privileges() -> None:
    with _connect(_ADMIN_LOGIN, "MIGRATION_PASSWORD") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 'finance.refunds'::regclass::oid")
            refunds_oid = cur.fetchone()[0]
            cur.execute("SELECT 'finance.payment_allocations'::regclass::oid")
            allocations_oid = cur.fetchone()[0]
            cur.execute("SELECT 'finance.invoices'::regclass::oid")
            invoices_oid = cur.fetchone()[0]
            cur.execute("SELECT 'finance.refund_obligation_bindings'::regclass::oid")
            bindings_oid = cur.fetchone()[0]

    for login, password_env, can_execute in (
        (_WORKER_LOGIN, "WORKER_RUNTIME_PASSWORD", True),
        (_APP_LOGIN, "APP_RUNTIME_PASSWORD", False),
        (_MAINTENANCE_LOGIN, "MAINTENANCE_RUNTIME_PASSWORD", False),
    ):
        with _connect(login, password_env, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_catalog.has_function_privilege(current_user,%s,'EXECUTE')", (_RESOLVE,))
                assert cur.fetchone()[0] is can_execute
                for oid, privilege in ((refunds_oid, "INSERT"), (allocations_oid, "SELECT"), (invoices_oid, "SELECT"), (bindings_oid, "SELECT")):
                    cur.execute("SELECT pg_catalog.has_table_privilege(current_user,%s::oid,%s)", (oid, privilege))
                    assert cur.fetchone()[0] is False
                if login == _APP_LOGIN:
                    cur.execute("SELECT pg_catalog.has_table_privilege(current_user,'public.branch_outbox_events','UPDATE')")
                    assert cur.fetchone()[0] is False

    with _connect(_APP_LOGIN, "APP_RUNTIME_PASSWORD") as conn:
        with pytest.raises(InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM app_secure.resolve_branch_refund_required(%s,%s)", (_SOURCE_C, _WORKER_A))
        conn.rollback()

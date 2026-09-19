from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
import os
import uuid

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

APP_URL=os.environ.get("PAY4_APP_DATABASE_URL")
WORKER_URL=os.environ.get("PAY4_WORKER_DATABASE_URL")
ADMIN_URL=os.environ.get("PAY4_MIGRATION_DATABASE_URL")

pytestmark=pytest.mark.skipif(
    not (APP_URL and WORKER_URL and ADMIN_URL),
    reason="PAY-4 isolated PG16 harness is not configured",
)

ORG=uuid.UUID("44000000-0000-4000-8000-000000000001")
OTHER_ORG=uuid.UUID("44000000-0000-4000-8000-000000000002")
BRANCH=uuid.UUID("44000000-0000-4000-8000-000000000011")
MEMBER=uuid.UUID("44000000-0000-4000-8000-000000000021")
PLAN=uuid.UUID("44000000-0000-4000-8000-000000000031")
SUB=uuid.UUID("44000000-0000-4000-8000-000000000041")
ENTITY=uuid.UUID("44000000-0000-4000-8000-000000000051")
GST=uuid.UUID("44000000-0000-4000-8000-000000000052")
DIVISION=uuid.UUID("44000000-0000-4000-8000-000000000053")
BRAND=uuid.UUID("44000000-0000-4000-8000-000000000054")
PARTY=uuid.UUID("44000000-0000-4000-8000-000000000055")
INVOICE=uuid.UUID("44000000-0000-4000-8000-000000000061")
PAYMENT=uuid.UUID("44000000-0000-4000-8000-000000000071")
EVENT=uuid.UUID("44000000-0000-4000-8000-000000000081")


def _set_org(cur, org=ORG):
    cur.execute("SELECT pg_catalog.set_config('app.current_org_id',%s,true)",(str(org),))
    cur.execute("SELECT pg_catalog.set_config('app.current_role','owner',true)")


def _cleanup():
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.subscription_events WHERE org_id=%s",(ORG,))
            # PAY-4 binding/context rows are immutable under row mutation.  The
            # isolated test administrator resets both FK-related tables as one
            # explicit disposable-test TRUNCATE; no production code imports it.
            cur.execute(
                "TRUNCATE TABLE finance.member_subscription_finance_bindings, "
                "finance.payment_contexts"
            )
            cur.execute("DELETE FROM public.subscription_slot_assignments WHERE org_id=%s",(ORG,))
            cur.execute("DELETE FROM public.subscription_term_slots WHERE org_id=%s",(ORG,))
            cur.execute("DELETE FROM public.subscription_terms WHERE org_id=%s",(ORG,))
            cur.execute("DELETE FROM public.subscription_series WHERE org_id=%s",(ORG,))
            cur.execute("DELETE FROM finance.outbox_events WHERE organization_id=%s",(ORG,))
            cur.execute("DELETE FROM finance.payment_allocations WHERE invoice_id=%s",(INVOICE,))
            cur.execute("DELETE FROM finance.payment_events WHERE payment_id=%s",(PAYMENT,))
            cur.execute("DELETE FROM finance.payments WHERE id=%s",(PAYMENT,))
            cur.execute("DELETE FROM finance.invoice_lines WHERE invoice_id=%s",(INVOICE,))
            cur.execute("DELETE FROM finance.invoices WHERE id=%s",(INVOICE,))
            cur.execute("DELETE FROM finance.billing_parties WHERE id=%s",(PARTY,))
            cur.execute("DELETE FROM public.subscription_members WHERE subscription_id=%s",(SUB,))
            cur.execute("DELETE FROM public.member_subscriptions_v2 WHERE id=%s",(SUB,))
            cur.execute("DELETE FROM public.membership_plans WHERE id=%s",(PLAN,))
            cur.execute("DELETE FROM public.members WHERE id=%s",(MEMBER,))
            cur.execute("DELETE FROM public.org_branches WHERE id=%s",(BRANCH,))
            cur.execute("DELETE FROM finance.brands WHERE id=%s",(BRAND,))
            cur.execute("DELETE FROM finance.divisions WHERE id=%s",(DIVISION,))
            cur.execute("DELETE FROM finance.gst_registrations WHERE id=%s",(GST,))
            cur.execute("DELETE FROM finance.legal_entities WHERE id=%s",(ENTITY,))
            cur.execute("DELETE FROM public.organizations WHERE id IN (%s,%s)",(ORG,OTHER_ORG))
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh():
    _cleanup()
    yield
    _cleanup()


def _seed_pending(*, start_offset_days=0):
    start=date.today()+timedelta(days=start_offset_days)
    end=start+timedelta(days=30)
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.organizations(id,name,slug,tier,is_active,default_currency_code)
                VALUES
                  (%s,'PAY4 Org','pay4-org','basic',true,'INR'),
                  (%s,'PAY4 Other','pay4-other','basic',true,'INR')
                """,(ORG,OTHER_ORG)
            )
            cur.execute(
                "INSERT INTO finance.legal_entities(id,code,legal_name,status) "
                "VALUES (%s,'PAY4_ENTITY','PAY4 Entity','active')",(ENTITY,)
            )
            cur.execute(
                """
                INSERT INTO finance.gst_registrations(
                    id,legal_entity_id,gstin,state_code,state_name,registered_address,status
                ) VALUES (%s,%s,'33ABCDE1234F1Z5','33','Tamil Nadu','Chennai','active')
                """,(GST,ENTITY)
            )
            cur.execute(
                "INSERT INTO finance.divisions(id,legal_entity_id,code,name,status) "
                "VALUES (%s,%s,'VS','PAY4 Division','active')",(DIVISION,ENTITY)
            )
            cur.execute(
                "INSERT INTO finance.brands(id,legal_entity_id,division_id,code,name,status) "
                "VALUES (%s,%s,%s,'DS','PAY4 Brand','active')",(BRAND,ENTITY,DIVISION)
            )
            # Branch administration is not an ordinary app_runtime capability.
            # Seed the already-existing branch through the migration/admin test
            # identity while preserving the tenant GUC required by its guards.
            cur.execute(
                "SELECT pg_catalog.set_config('app.current_org_id',%s,true)",
                (str(ORG),),
            )
            cur.execute(
                """
                INSERT INTO public.org_branches(
                    id,org_id,branch_name,branch_code,internal_slug,country_code,
                    currency_code,timezone
                ) VALUES (%s,%s,'PAY4 Branch','PAY4','pay4-branch','IN','INR','Asia/Kolkata')
                """,(BRANCH,ORG)
            )
        conn.commit()

    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            cur.execute(
                """
                INSERT INTO public.members(
                    id,org_id,home_branch_id,member_uid,member_number,name,status,is_active,is_migrated
                ) VALUES (%s,%s,%s,'PAY4001',440001,'PAY4 Member','active',true,false)
                """,(MEMBER,ORG,BRANCH)
            )
            cur.execute(
                """
                INSERT INTO public.membership_plans(
                    id,org_id,branch_id,plan_code,name,price,currency,
                    duration_value,duration_unit,max_members,status
                ) VALUES (%s,%s,%s,'PAY4-PLAN','PAY4 Plan',100,'INR',1,'months',1,'active')
                """,(PLAN,ORG,BRANCH)
            )
            cur.execute(
                """
                INSERT INTO public.member_subscriptions_v2(
                    id,org_id,branch_id,membership_plan_id,primary_member_id,subscription_code,
                    start_date,end_date,status,price_snapshot,currency_code,
                    duration_value_snapshot,duration_unit_snapshot,max_members_snapshot
                ) VALUES (%s,%s,%s,%s,%s,'PAY4-SUB-A',%s,%s,'pending',100,'INR',1,'months',1)
                """,(SUB,ORG,BRANCH,PLAN,MEMBER,start,end)
            )
            cur.execute(
                "INSERT INTO public.subscription_members("
                "id,org_id,subscription_id,member_id,slot_number,role,is_active"
                ") VALUES (gen_random_uuid(),%s,%s,%s,1,'primary',true)",
                (ORG,SUB,MEMBER),
            )
            cur.execute(
                "SELECT * FROM app_secure.create_member_subscription_pending_term(%s)",
                (SUB,),
            )
            first=cur.fetchone()
            cur.execute(
                "SELECT * FROM app_secure.create_member_subscription_pending_term(%s)",
                (SUB,),
            )
            replay=cur.fetchone()
        conn.commit()
    assert first[0]==replay[0]
    assert first[2:] == (True,False)
    assert replay[2:] == (False,True)
    return first[0]


def _seed_invoice_and_binding(term_id):
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance.billing_parties(
                    id,organization_id,buyer_kind,member_id,billing_name,party_type,
                    gst_treatment,billing_address,place_of_supply_state_code,status
                ) VALUES (%s,%s,'member',%s,'PAY4 Member','individual','b2c','Chennai','33','active')
                """,(PARTY,ORG,MEMBER)
            )
            cur.execute(
                """
                INSERT INTO finance.invoices(
                    id,organization_id,billing_party_id,legal_entity_id,gst_registration_id,
                    division_id,brand_id,financial_year,official_invoice_number,brand_reference,
                    status,currency_code,seller_legal_name,seller_gstin,seller_registered_address,
                    seller_state_code,buyer_billing_name,buyer_address,
                    buyer_place_of_supply_state_code,buyer_gst_treatment,gst_supply_type,
                    subtotal_amount,discount_amount,taxable_amount,total_tax_amount,
                    grand_total_amount,issued_at
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,'2627','PAY4-INV-1','PAY4-BR-1',
                    'issued','INR','PAY4 Entity','33ABCDE1234F1Z5','Chennai','33',
                    'PAY4 Member','Chennai','33','b2c','intra_state',
                    100,0,100,0,100,clock_timestamp()
                )
                """,(INVOICE,ORG,PARTY,ENTITY,GST,DIVISION,BRAND)
            )
            cur.execute(
                """
                INSERT INTO finance.invoice_lines(
                    id,invoice_id,line_number,description,hsn_sac,quantity,unit_amount,
                    discount_amount,taxable_amount,gst_rate_basis_points,cgst_amount,
                    sgst_amount,igst_amount,total_tax_amount,line_total_amount,pricing_mode
                ) VALUES (
                    gen_random_uuid(),%s,1,'Membership subscription PAY4-SUB-A','9999',
                    1,100,0,100,0,0,0,0,0,100,'tax_inclusive'
                )
                """,(INVOICE,)
            )
        conn.commit()

    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            cur.execute(
                "SELECT * FROM app_secure.record_member_subscription_finance_binding(%s,%s)",
                (SUB,INVOICE),
            )
            first=cur.fetchone()
            cur.execute(
                "SELECT * FROM app_secure.record_member_subscription_finance_binding(%s,%s)",
                (SUB,INVOICE),
            )
            replay=cur.fetchone()
        conn.commit()
    assert first[1]==term_id
    assert first[6:] == (True,False)
    assert replay[6:] == (False,True)
    assert first[0]==replay[0]
    assert first[3]==replay[3]
    return first[0]


def _finance_event(*, payment_status=None, allocated=None, payment_currency="INR"):
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE finance.invoices SET status='paid' WHERE id=%s",(INVOICE,))
            if payment_status is not None:
                cur.execute(
                    """
                    INSERT INTO finance.payments(
                        id,organization_id,legal_entity_id,provider_code,provider_payment_ref,
                        amount,currency_code,status
                    ) VALUES (%s,%s,%s,'pay4_test','pay4-payment',100,%s,%s)
                    """,(PAYMENT,ORG,ENTITY,payment_currency,payment_status)
                )
                if allocated is not None:
                    cur.execute(
                        "INSERT INTO finance.payment_allocations(payment_id,invoice_id,allocated_amount) "
                        "VALUES (%s,%s,%s)",
                        (PAYMENT,INVOICE,Decimal(allocated)),
                    )
            cur.execute(
                """
                INSERT INTO finance.outbox_events(
                    id,organization_id,aggregate_type,aggregate_id,event_type,idempotency_key,
                    payload_json,payload_sha256,status,attempt_count
                ) VALUES (
                    %s,%s,'invoice',%s,'finance.invoice.paid','pay4:invoice:paid',
                    jsonb_build_object('invoice_id',%s::text,'status','paid'),
                    %s,'pending',0
                )
                """,(EVENT,ORG,INVOICE,INVOICE,"a"*64)
            )
        conn.commit()


def _apply(org=ORG, key="pay4:activate:1"):
    with psycopg.connect(WORKER_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur,org)
            cur.execute(
                "SELECT * FROM app_secure.apply_member_subscription_finance_event(%s,%s)",
                (EVENT,key),
            )
            row=cur.fetchone()
        conn.commit()
        return row


def _status(term_id):
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status::text FROM public.subscription_terms WHERE id=%s",(term_id,))
            return cur.fetchone()[0]


def test_pending_admission_and_binding_are_canonical_and_runtime_table_blind():
    term=_seed_pending()
    binding=_seed_invoice_and_binding(term)
    assert _status(term)=="pending_payment"
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.subscription_term_id,b.finance_invoice_id,b.member_id,b.amount::text,
                       b.currency_code,b.plan_snapshot->>'plan_id',
                       c.business_reference
                FROM finance.member_subscription_finance_bindings b
                JOIN finance.payment_contexts c ON c.id=b.finance_payment_context_id
                WHERE b.id=%s
                """,(binding,)
            )
            row=cur.fetchone()
    assert row==(term,INVOICE,MEMBER,"100.00","INR",str(PLAN),f"subscription_term:{term}")

    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            with pytest.raises(InsufficientPrivilege):
                cur.execute("SELECT count(*) FROM finance.member_subscription_finance_bindings")
        conn.rollback()


def test_direct_app_runtime_active_creation_and_activation_are_denied():
    _seed_pending()
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    "UPDATE public.member_subscriptions_v2 SET status='active' WHERE id=%s",
                    (SUB,),
                )
        conn.rollback()
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    "SELECT * FROM app_secure.apply_member_subscription_finance_event(%s,'x')",
                    (EVENT,),
                )
        conn.rollback()


@pytest.mark.parametrize(
    ("payment_status","allocated","currency"),
    [
        (None,None,"INR"),
        ("created","100.00","INR"),
        ("failed","100.00","INR"),
        ("captured","90.00","INR"),
        ("captured","100.00","USD"),
    ],
)
def test_missing_pending_failed_amount_or_currency_mismatch_never_activates(
    payment_status,allocated,currency
):
    term=_seed_pending()
    _seed_invoice_and_binding(term)
    _finance_event(
        payment_status=payment_status,
        allocated=allocated,
        payment_currency=currency,
    )
    with pytest.raises(psycopg.Error):
        _apply()
    assert _status(term)=="pending_payment"


def test_fully_applied_payment_activates_exactly_once_under_concurrency():
    term=_seed_pending()
    _seed_invoice_and_binding(term)
    _finance_event(payment_status="captured",allocated="100.00")

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(lambda _: _apply(),range(2)))

    assert _status(term)=="active"
    assert sorted((row[2],row[3]) for row in rows)==[(False,True),(True,False)]
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM public.subscription_events
                WHERE term_id=%s AND event_source='finance'
                  AND metadata->>'finance_event_id'=%s
                """,(term,str(EVENT))
            )
            assert cur.fetchone()[0]==1
            cur.execute("SELECT status::text FROM public.member_subscriptions_v2 WHERE id=%s",(SUB,))
            assert cur.fetchone()[0]=="active"


def test_future_paid_term_is_scheduled_not_prematurely_active():
    term=_seed_pending(start_offset_days=5)
    _seed_invoice_and_binding(term)
    _finance_event(payment_status="settled",allocated="100.00")
    row=_apply()
    assert row[1:] == ("scheduled",True,False)
    assert _status(term)=="scheduled"
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status::text FROM public.member_subscriptions_v2 WHERE id=%s",(SUB,))
            assert cur.fetchone()[0]=="pending"


def test_cross_tenant_event_cannot_activate():
    term=_seed_pending()
    _seed_invoice_and_binding(term)
    _finance_event(payment_status="captured",allocated="100.00")
    with pytest.raises(psycopg.Error):
        _apply(OTHER_ORG)
    assert _status(term)=="pending_payment"

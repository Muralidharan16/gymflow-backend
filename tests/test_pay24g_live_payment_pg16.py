from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import os
import uuid

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege


APP_URL=os.environ.get("PAY24G_APP_DATABASE_URL")
RECON_URL=os.environ.get("PAY24G_RECONCILIATION_DATABASE_URL")
CONFIG_URL=os.environ.get("PAY24G_CONFIG_DATABASE_URL")
ADMIN_URL=os.environ.get("PAY24G_ADMIN_DATABASE_URL")

pytestmark=pytest.mark.skipif(
    not (APP_URL and RECON_URL and CONFIG_URL and ADMIN_URL),
    reason="PAY-24-G fresh PostgreSQL harness is not configured",
)

ORG=uuid.UUID("24a00000-0000-4000-8000-000000000101")
ENTITY=uuid.UUID("24a00000-0000-4000-8000-000000000102")
PAYMENT=uuid.UUID("24a00000-0000-4000-8000-000000000103")
ORDER="order_pay24gpg16"
PROVIDER_PAYMENT="pay_pay24gpg16"
SHA="a"*40
ACTOR="pay24g-pg16-certifier"


def _set_org(cur):
    cur.execute(
        "SELECT pg_catalog.set_config('app.current_org_id',%s,true)",
        (str(ORG),),
    )


def _config_call(sql: str, params):
    with psycopg.connect(CONFIG_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row=cur.fetchone()
        conn.commit()
        return row


def _snapshot():
    with psycopg.connect(CONFIG_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM app_secure.pay24a_activation_snapshot()")
            return cur.fetchone()


def _reset_stage0():
    snapshot=_snapshot()
    if snapshot[0] == 0:
        return
    generation=int(snapshot[1])
    if snapshot[2] != "closing":
        begun=_config_call(
            """
            SELECT * FROM app_secure.pay24a_begin_emergency_rollback(
                CAST(%s AS uuid),CAST(%s AS bigint),CAST(%s AS text)
            )
            """,
            (uuid.uuid4(),generation,ACTOR),
        )
        generation=int(begun[0])
    final=_config_call(
        """
        SELECT * FROM app_secure.pay24a_finalize_emergency_rollback(
            CAST(%s AS uuid),CAST(%s AS bigint),CAST(%s AS text)
        )
        """,
        (uuid.uuid4(),generation,ACTOR),
    )
    assert final[1] == 0
    assert final[2] == "blocked"


def _activate_stage1():
    snapshot=_snapshot()
    assert snapshot[0] == 0
    generation=int(snapshot[1])
    measured=datetime.now(UTC)-timedelta(seconds=2)

    release=_config_call(
        """
        SELECT * FROM app_secure.pay24a_bind_release_identity(
            CAST(%s AS uuid),CAST(%s AS bigint),
            CAST(%s AS text),CAST(%s AS text),CAST(%s AS text),
            CAST(%s AS timestamptz),CAST(%s AS text)
        )
        """,
        (
            uuid.uuid4(),generation,SHA,SHA,
            "pay24g-pg16-measurement",measured,ACTOR,
        ),
    )
    generation=int(release[0])

    auth=_config_call(
        """
        SELECT * FROM app_secure.pay24a_bind_human_authorization(
            CAST(%s AS uuid),CAST(%s AS bigint),
            CAST(%s AS text),CAST(%s AS text),CAST(1 AS smallint),
            CAST(%s AS text),CAST(%s AS timestamptz),CAST(%s AS text)
        )
        """,
        (
            uuid.uuid4(),generation,
            "pay24g-pg16-"+uuid.uuid4().hex[:12],SHA,
            "pay24g-human-approver",measured,ACTOR,
        ),
    )
    generation=int(auth[0])

    transitioned=_config_call(
        """
        SELECT * FROM app_secure.pay24a_transition_activation(
            CAST(%s AS uuid),CAST(%s AS bigint),CAST(1 AS smallint),
            CAST('open' AS text),CAST(%s AS uuid),
            true,true,true,true,false,false,false,false,
            CAST(%s AS text)
        )
        """,
        (uuid.uuid4(),generation,ORG,ACTOR),
    )
    assert transitioned[1] == 1
    assert transitioned[2] == "open"


def _seed():
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.organizations(
                    id,name,slug,tier,is_active,max_branches,default_currency_code
                ) VALUES (%s,'PAY24G PG16','pay24g-pg16','basic',true,1,'INR')
                """,
                (ORG,),
            )
            cur.execute(
                """
                INSERT INTO finance.legal_entities(
                    id,code,legal_name,status
                ) VALUES (%s,'PAY24G','PAY24G PG16 Entity','active')
                """,
                (ENTITY,),
            )
            cur.execute(
                """
                INSERT INTO finance.payments(
                    id,organization_id,legal_entity_id,provider_code,
                    provider_order_ref,amount,currency_code,status,raw_status
                ) VALUES (
                    %s,%s,%s,'razorpay',%s,1.00,'INR','created','created'
                )
                """,
                (PAYMENT,ORG,ENTITY,ORDER),
            )
        conn.commit()


def _cleanup():
    _reset_stage0()
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM finance.outbox_events WHERE organization_id=%s",
                (ORG,),
            )
            cur.execute(
                "DELETE FROM finance.payment_events WHERE payment_id=%s",
                (PAYMENT,),
            )
            cur.execute("DELETE FROM finance.payments WHERE id=%s",(PAYMENT,))
            cur.execute("DELETE FROM finance.legal_entities WHERE id=%s",(ENTITY,))
            cur.execute("DELETE FROM public.organizations WHERE id=%s",(ORG,))
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh():
    _cleanup()
    _seed()
    yield
    _cleanup()


def _confirm(url: str, provider_code: str = "razorpay"):
    canonical=(
        '{"checkout_signature_verified":true,'
        '"provider_amount_subunits":100,'
        '"provider_api_verified":true,'
        '"provider_captured":true,'
        f'"provider_code":"{provider_code}",'
        '"provider_currency":"INR",'
        f'"provider_order_ref":"{ORDER}",'
        f'"provider_payment_ref":"{PROVIDER_PAYMENT}",'
        '"provider_payment_status":"captured"}'
    )
    request_hash=hashlib.sha256(canonical.encode()).hexdigest()
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            cur.execute(
                """
                SELECT * FROM app_secure.confirm_finance_provider_evidence(
                    %s,'pay24g_pg16_event','payment.captured',
                    %s,%s,100,'INR','captured',
                    'pay24g:pg16:evidence',%s
                )
                """,
                (provider_code,ORDER,PROVIDER_PAYMENT,request_hash),
            )
            row=cur.fetchone()
        conn.commit()
        return row


def test_live_evidence_is_role_and_stage_fenced_then_rollback_blocks_apply():
    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    """
                    SELECT * FROM app_secure.confirm_finance_provider_evidence(
                        'razorpay','pay24g_pg16_app','payment.captured',
                        %s,%s,100,'INR','captured',
                        'pay24g:pg16:app',%s
                    )
                    """,
                    (ORDER,PROVIDER_PAYMENT,"b"*64),
                )
        conn.rollback()

    with pytest.raises(InsufficientPrivilege):
        _confirm(RECON_URL)

    with psycopg.connect(RECON_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    """
                    SELECT * FROM app_secure.reserve_finance_idempotency(
                        'finance.provider.capture.confirm',
                        'pay24g:direct-helper-denied',
                        %s,%s,clock_timestamp()+interval '7 days'
                    )
                    """,
                    ("e"*64,ORG),
                )
        conn.rollback()

    _activate_stage1()
    confirmed=_confirm(RECON_URL)
    assert confirmed is not None
    assert confirmed[3] == "razorpay"
    assert confirmed[6] == "created"
    assert confirmed[7] == "captured"
    assert confirmed[8] is True

    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status::text,provider_payment_ref "
                "FROM finance.payments WHERE id=%s",
                (PAYMENT,),
            )
            assert cur.fetchone() == ("captured",PROVIDER_PAYMENT)

    snapshot=_snapshot()
    begun=_config_call(
        """
        SELECT * FROM app_secure.pay24a_begin_emergency_rollback(
            CAST(%s AS uuid),CAST(%s AS bigint),CAST(%s AS text)
        )
        """,
        (uuid.uuid4(),int(snapshot[1]),ACTOR),
    )
    assert begun[0] == int(snapshot[1])+1

    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    """
                    SELECT * FROM app_secure.apply_finance_confirmed_payment(
                        %s,%s,1.00,'INR','pay24g:pg16:apply',%s
                    )
                    """,
                    (PAYMENT,uuid.uuid4(),"c"*64),
                )
        conn.rollback()

    with psycopg.connect(RECON_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    """
                    SELECT * FROM app_secure.confirm_finance_provider_evidence(
                        'razorpay_sandbox','pay24g_pg16_sandbox','payment.captured',
                        %s,%s,100,'INR','captured',
                        'pay24g:pg16:sandbox',%s
                    )
                    """,
                    (ORDER,PROVIDER_PAYMENT,"d"*64),
                )
        conn.rollback()

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege


APP_URL = os.environ.get("PAY24F_APP_DATABASE_URL")
PAYMENT_URL = os.environ.get("PAY24F_PAYMENT_DATABASE_URL")
ADMIN_URL = os.environ.get("PAY24F_ADMIN_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not (APP_URL and PAYMENT_URL and ADMIN_URL),
    reason="PAY-24-F fresh PostgreSQL harness is not configured",
)

ORG = uuid.UUID("24f00000-0000-4000-8000-000000000010")
ENTITY = uuid.UUID("24f00000-0000-4000-8000-000000000011")
PAYMENT = uuid.UUID("24f00000-0000-4000-8000-000000000012")
LEASE = uuid.UUID("24f00000-0000-4000-8000-000000000013")
REQUEST_HASH = "f" * 64


def _set_org(cur, org=ORG) -> None:
    cur.execute(
        "SELECT pg_catalog.set_config('app.current_org_id',%s,true)",
        (str(org),),
    )


def _seed() -> None:
    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.organizations(
                    id,name,tier,is_active,max_branches,default_currency_code
                ) VALUES (%s,'PAY24F PG16','basic',true,1,'INR')
                """,
                (ORG,),
            )
            cur.execute(
                """
                INSERT INTO finance.legal_entities(
                    id,code,legal_name,status
                ) VALUES (%s,'PAY24F','PAY24F PG16 Entity','active')
                """,
                (ENTITY,),
            )
            cur.execute(
                """
                INSERT INTO finance.payments(
                    id,organization_id,legal_entity_id,provider_code,
                    amount,currency_code,status,raw_status
                ) VALUES (%s,%s,%s,'razorpay',1.00,'INR','created','pay24f_pg16')
                """,
                (PAYMENT, ORG, ENTITY),
            )
        conn.commit()


def test_pay24f_live_checkout_environment_is_real_pg16_and_stage0_fenced() -> None:
    _seed()

    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW server_version_num")
            assert 160000 <= int(cur.fetchone()[0]) < 170000
            cur.execute("SELECT version_num FROM alembic_version")
            assert cur.fetchone()[0] in {
                "zzd7d8e9f0a73",
                "zze7d8e9f0a74",
            }
            cur.execute(
                """
                SELECT pg_catalog.pg_get_constraintdef(c.oid,true)
                FROM pg_catalog.pg_constraint c
                WHERE c.conrelid='finance.provider_operations'::regclass
                  AND c.conname='chk_pay8_provider_environment'
                """
            )
            definition = cur.fetchone()[0]
            assert "live" in definition

    with psycopg.connect(APP_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            cur.execute(
                """
                SELECT *
                FROM app_secure.reserve_finance_provider_operation(
                    %s,'razorpay','live','create_checkout',
                    'pay24f-pg16-live-order',%s
                )
                """,
                (PAYMENT, REQUEST_HASH),
            )
            reserved = cur.fetchone()
        conn.commit()

    assert reserved is not None
    operation_id = reserved[0]
    assert reserved[1] == "reserved"

    with psycopg.connect(PAYMENT_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            cur.execute(
                "SELECT * FROM app_secure.claim_finance_provider_operation(%s,%s)",
                (operation_id, LEASE),
            )
            claim = cur.fetchone()
        conn.commit()

    assert claim is not None
    assert claim[0] == operation_id
    assert claim[-1] is True

    with psycopg.connect(PAYMENT_URL) as conn:
        with conn.cursor() as cur:
            _set_org(cur)
            with pytest.raises(InsufficientPrivilege):
                cur.execute(
                    """
                    SELECT *
                    FROM app_secure.pay24b_request_current_provider_admission(
                        'checkout','pay24f-pg16-live-order',%s,300
                    )
                    """,
                    (REQUEST_HASH,),
                )
        conn.rollback()

    with psycopg.connect(ADMIN_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT environment,status
                FROM finance.provider_operations
                WHERE id=%s
                """,
                (operation_id,),
            )
            row = cur.fetchone()
    assert row == ("live", "in_flight")

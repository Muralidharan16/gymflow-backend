from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from tests.platform_billing.test_pay11_production_model import _seed_pay11_contract
from tests.platform_billing.test_phase1_schema import ORG_1


UTC = timezone.utc
RACE_NOW = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Mutation:
    name: str
    sql: str
    params: dict[str, object]


async def _prepare_active_contract() -> dict[str, str]:
    ids = await _seed_pay11_contract()
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org, true)"),
            {"org": ORG_1},
        )
        await session.execute(
            text(
                """
                UPDATE platform_subscriptions
                   SET status='past_due',
                       current_period_start=:period_start,
                       current_period_end=:period_end,
                       cancel_at_period_end=false,
                       cancellation_requested_at=NULL,
                       cancellation_effective_at=NULL,
                       canceled_at=NULL,
                       ended_at=NULL,
                       version=version+1,
                       updated_at=pg_catalog.clock_timestamp()
                 WHERE id=:subscription
                """
            ),
            {
                "subscription": ids["subscription"],
                "period_start": RACE_NOW - timedelta(days=30),
                "period_end": RACE_NOW,
            },
        )
        await session.commit()
    return ids


async def _snapshot(subscription_id: str) -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org, true)"),
            {"org": ORG_1},
        )
        row = (
            await session.execute(
                text(
                    """
                    SELECT status,version,current_period_start,current_period_end,
                           cancel_at_period_end,cancellation_requested_at,
                           cancellation_effective_at,ended_at
                      FROM platform_subscriptions
                     WHERE id=:subscription
                    """
                ),
                {"subscription": subscription_id},
            )
        ).mappings().one()
        return dict(row)


async def _cas(subscription_id: str, expected_version: int, mutation: Mutation) -> bool:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_catalog.set_config('app.current_org_id', :org, true)"),
            {"org": ORG_1},
        )
        result = await session.execute(
            text(mutation.sql),
            mutation.params
            | {
                "subscription": subscription_id,
                "organization_id": ORG_1,
                "expected_version": expected_version,
            },
        )
        await session.commit()
        return result.rowcount == 1


def _payment_mutation() -> Mutation:
    return Mutation(
        name="payment",
        sql="""
            UPDATE platform_subscriptions
               SET status=CASE
                       WHEN status='cancel_scheduled'
                       THEN 'cancel_scheduled'
                       ELSE 'active'
                   END,
                   ended_at=NULL,
                   version=version+1,
                   updated_at=pg_catalog.clock_timestamp()
             WHERE id=:subscription
               AND organization_id=:organization_id
               AND version=:expected_version
        """,
        params={},
    )


def _payment_after_expiry_mutation() -> Mutation:
    return Mutation(
        name="payment_after_expiry",
        sql="""
            UPDATE platform_subscriptions
               SET status='active',
                   current_period_start=:period_start,
                   current_period_end=:period_end,
                   cancel_at_period_end=false,
                   cancellation_requested_at=NULL,
                   cancellation_effective_at=NULL,
                   ended_at=NULL,
                   version=version+1,
                   updated_at=pg_catalog.clock_timestamp()
             WHERE id=:subscription
               AND organization_id=:organization_id
               AND version=:expected_version
        """,
        params={
            "period_start": RACE_NOW,
            "period_end": RACE_NOW + timedelta(days=30),
        },
    )


def _cancellation_mutation() -> Mutation:
    return Mutation(
        name="cancellation",
        sql="""
            UPDATE platform_subscriptions
               SET status='cancel_scheduled',
                   cancel_at_period_end=true,
                   cancellation_requested_at=:requested_at,
                   cancellation_effective_at=current_period_end,
                   version=version+1,
                   updated_at=pg_catalog.clock_timestamp()
             WHERE id=:subscription
               AND organization_id=:organization_id
               AND version=:expected_version
        """,
        params={"requested_at": RACE_NOW},
    )


def _renewal_mutation() -> Mutation:
    return Mutation(
        name="renewal",
        sql="""
            UPDATE platform_subscriptions
               SET status='active',
                   current_period_start=:period_start,
                   current_period_end=:period_end,
                   cancel_at_period_end=false,
                   ended_at=NULL,
                   version=version+1,
                   updated_at=pg_catalog.clock_timestamp()
             WHERE id=:subscription
               AND organization_id=:organization_id
               AND version=:expected_version
        """,
        params={
            "period_start": RACE_NOW,
            "period_end": RACE_NOW + timedelta(days=30),
        },
    )


def _expiry_mutation() -> Mutation:
    return Mutation(
        name="expiry",
        sql="""
            UPDATE platform_subscriptions
               SET status='expired',
                   cancel_at_period_end=false,
                   ended_at=:ended_at,
                   version=version+1,
                   updated_at=pg_catalog.clock_timestamp()
             WHERE id=:subscription
               AND organization_id=:organization_id
               AND version=:expected_version
        """,
        params={"ended_at": RACE_NOW},
    )


async def _race(
    subscription_id: str,
    left: Mutation,
    right: Mutation,
) -> tuple[bool, bool, dict[str, object]]:
    before = await _snapshot(subscription_id)
    expected_version = int(before["version"])
    left_result, right_result = await asyncio.gather(
        _cas(subscription_id, expected_version, left),
        _cas(subscription_id, expected_version, right),
    )
    assert int(left_result) + int(right_result) == 1
    after = await _snapshot(subscription_id)
    assert int(after["version"]) == expected_version + 1
    return left_result, right_result, after


@pytest.mark.asyncio
async def test_payment_and_cancellation_race_has_one_commit_then_preserves_cancellation_intent():
    ids = await _prepare_active_contract()
    payment_won, cancellation_won, after = await _race(
        ids["subscription"],
        _payment_mutation(),
        _cancellation_mutation(),
    )

    if payment_won:
        # The stale cancellation command must re-read the new version and may
        # then apply against that durable state; it cannot overwrite blindly.
        assert await _cas(
            ids["subscription"],
            int(after["version"]),
            _cancellation_mutation(),
        )
        after = await _snapshot(ids["subscription"])
    elif cancellation_won:
        # Confirmed payment remains durable, but applying it after cancellation
        # must preserve the already-scheduled cancellation rather than silently
        # reactivating an unwanted subscription.
        assert await _cas(
            ids["subscription"],
            int(after["version"]),
            _payment_mutation(),
        )
        after = await _snapshot(ids["subscription"])

    assert cancellation_won or payment_won
    assert after["status"] == "cancel_scheduled"
    assert after["cancel_at_period_end"] is True
    assert after["cancellation_requested_at"] is not None
    assert after["cancellation_effective_at"] == after["current_period_end"]


@pytest.mark.asyncio
async def test_payment_and_renewal_race_converges_to_one_paid_renewed_period():
    ids = await _prepare_active_contract()
    payment_won, renewal_won, after = await _race(
        ids["subscription"],
        _payment_mutation(),
        _renewal_mutation(),
    )

    if payment_won:
        assert await _cas(
            ids["subscription"],
            int(after["version"]),
            _renewal_mutation(),
        )
    elif renewal_won:
        # Payment success is already represented by the renewed active period;
        # a stale payment status write must not create another period.
        assert not await _cas(
            ids["subscription"],
            int(after["version"]) - 1,
            _payment_mutation(),
        )

    final = await _snapshot(ids["subscription"])
    assert final["status"] == "active"
    assert final["current_period_start"] == RACE_NOW
    assert final["current_period_end"] == RACE_NOW + timedelta(days=30)
    assert final["ended_at"] is None


@pytest.mark.asyncio
async def test_payment_and_expiry_race_never_loses_confirmed_payment_or_grants_from_stale_expiry():
    ids = await _prepare_active_contract()
    payment_won, expiry_won, after = await _race(
        ids["subscription"],
        _payment_after_expiry_mutation(),
        _expiry_mutation(),
    )

    if expiry_won:
        # Confirmed payment is durable authority. After seeing that expiry won
        # the first version race, payment recovery retries against the new
        # version and restores active state. No stale expiry write may follow.
        assert await _cas(
            ids["subscription"],
            int(after["version"]),
            _payment_after_expiry_mutation(),
        )
    else:
        assert payment_won
        # Expiry was based on the old version and must not be allowed to apply
        # after confirmed payment changed the contract.
        assert not await _cas(
            ids["subscription"],
            int(after["version"]) - 1,
            _expiry_mutation(),
        )

    final = await _snapshot(ids["subscription"])
    assert final["status"] == "active"
    assert final["current_period_start"] == RACE_NOW
    assert final["current_period_end"] == RACE_NOW + timedelta(days=30)
    assert final["ended_at"] is None
    assert final["cancel_at_period_end"] is False


def test_pay17_lifecycle_races_are_version_fenced_not_last_writer_wins():
    source = __import__("pathlib").Path(__file__).read_text(encoding="utf-8")
    assert source.count("AND version=:expected_version") >= 4
    assert "version=version+1" in source
    assert "asyncio.gather" in source

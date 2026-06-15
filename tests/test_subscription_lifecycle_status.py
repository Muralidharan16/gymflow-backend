from datetime import date
from uuid import UUID

import pytest

from app.domain.subscription_lifecycle import (
    FreezeSummary,
    SubscriptionFreezeStatus,
    SubscriptionOperationalStatus,
    SubscriptionSeriesStatus,
    SubscriptionTermStatus,
    available_actions,
    is_freeze_active,
    resolve_term_status,
)


def test_resolve_term_status_uses_dates_and_terminal_statuses():
    assert (
        resolve_term_status(
            SubscriptionTermStatus.scheduled,
            date(2026, 7, 1),
            date(2026, 7, 31),
            date(2026, 6, 30),
        )
        == SubscriptionOperationalStatus.scheduled
    )
    assert (
        resolve_term_status(
            SubscriptionTermStatus.scheduled,
            date(2026, 7, 1),
            date(2026, 7, 31),
            date(2026, 7, 2),
        )
        == SubscriptionOperationalStatus.active
    )
    assert (
        resolve_term_status(
            SubscriptionTermStatus.active,
            date(2026, 7, 1),
            date(2026, 7, 31),
            date(2026, 8, 1),
        )
        == SubscriptionOperationalStatus.expired
    )
    assert (
        resolve_term_status(
            SubscriptionTermStatus.cancelled,
            date(2026, 7, 1),
            date(2026, 7, 31),
            date(2026, 7, 2),
        )
        == SubscriptionOperationalStatus.cancelled
    )


def test_resolve_term_status_prefers_frozen_inside_active_window():
    freeze = FreezeSummary(
        id=UUID("90000000-0000-0000-0000-000000000001"),
        status=SubscriptionFreezeStatus.active,
        requested_starts_on=date(2026, 7, 10),
        planned_ends_on=date(2026, 7, 20),
        actual_ended_on=None,
        extension_days=10,
    )

    assert is_freeze_active(freeze, date(2026, 7, 12))
    assert not is_freeze_active(freeze, date(2026, 7, 21))
    assert (
        resolve_term_status(
            SubscriptionTermStatus.active,
            date(2026, 7, 1),
            date(2026, 7, 31),
            date(2026, 7, 12),
            has_active_freeze=True,
        )
        == SubscriptionOperationalStatus.frozen
    )


@pytest.mark.parametrize(
    ("series_status", "term_status", "expected"),
    [
        (
            SubscriptionSeriesStatus.open,
            SubscriptionOperationalStatus.active,
            {"view", "schedule_renewal", "freeze", "cancel", "terminate"},
        ),
        (
            SubscriptionSeriesStatus.open,
            SubscriptionOperationalStatus.frozen,
            {"view", "resume", "cancel", "terminate"},
        ),
        (
            SubscriptionSeriesStatus.open,
            SubscriptionOperationalStatus.expired,
            {"view", "renew", "view_history"},
        ),
        (
            SubscriptionSeriesStatus.archived,
            SubscriptionOperationalStatus.expired,
            {"view", "restore"},
        ),
    ],
)
def test_available_actions_are_display_only(series_status, term_status, expected):
    assert set(available_actions(series_status, term_status)) == expected

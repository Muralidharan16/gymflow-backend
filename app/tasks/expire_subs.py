"""Retired legacy subscription task module.

PAY-24-C keeps this import-safe for old deployments, but all direct legacy
subscription mutation and inline reminder side effects are terminally disabled.
The only scheduled compatibility expiry path is
app.tasks.platform_maintenance.expire_legacy_member_subscriptions.
"""

from celery import shared_task


_RETIRED = (
    "PAY-24-C legacy entitlement mutation is retired; "
    "use platform maintenance/canonical entitlement authority"
)


async def _expire_subscriptions_async():
    raise RuntimeError(_RETIRED)


async def _send_expiry_reminders(session):
    del session
    raise RuntimeError(_RETIRED)


async def _check_expiring_soon_async():
    raise RuntimeError(_RETIRED)


@shared_task(name="expire_subscriptions")
def expire_subscriptions():
    raise RuntimeError(_RETIRED)


@shared_task(name="check_expiring_soon")
def check_expiring_soon():
    raise RuntimeError(_RETIRED)

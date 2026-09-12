"""Fail-closed compatibility stubs for legacy global notification jobs.

The historical reminder implementations globally discovered members and
subscriptions under ``worker_runtime`` and performed inline WhatsApp delivery.
That combined cross-tenant discovery, authorization and an external side effect
without a durable idempotency boundary. P3E deliberately disables those entry
points instead of broadening worker database authority.

Notification delivery may be reintroduced only through a tenant-bound durable
outbox/claim design with current PostgreSQL authorization and idempotent
provider delivery.
"""

from celery import shared_task


_DISABLED_MESSAGE = (
    "Legacy global reminder delivery is disabled by P3E pending a "
    "tenant-bound durable notification outbox"
)


@shared_task(name="send_daily_reminders")
def send_daily_reminders():
    raise RuntimeError(_DISABLED_MESSAGE)


@shared_task(name="send_birthday_wishes")
def send_birthday_wishes():
    raise RuntimeError(_DISABLED_MESSAGE)

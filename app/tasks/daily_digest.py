"""Fail-closed compatibility stub for the legacy global daily digest.

The historical implementation scanned every gym under ``worker_runtime`` and
then opened fresh worker sessions without tenant context. P3E removes it from
Beat and keeps the registered name fail-closed so stale broker messages cannot
recover that authority path.
"""

from celery import shared_task


_DISABLED_MESSAGE = (
    "Legacy global daily digest is disabled by P3E pending a "
    "tenant-bound durable digest dispatcher"
)


@shared_task(name="app.tasks.daily_digest.run")
def run():
    raise RuntimeError(_DISABLED_MESSAGE)

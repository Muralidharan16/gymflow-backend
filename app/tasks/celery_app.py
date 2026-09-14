"""Compatibility deployment entrypoint for the canonical Celery app.

Docker and any existing process manager may continue to use
``celery -A app.tasks.celery_app ...``. This module intentionally defines no
second Celery instance or beat schedule; it exports the single application from
``app.core.celery_app``.
"""

from app.core.config import settings
from app.core.celery_app import celery_app


# P6-S: every production process loaded through the deployment entrypoint uses
# the ownership-aware scheduler class. Normal workers never instantiate it, but
# an attempted embedded ``worker -B`` does and is rejected because its process
# profile is not ``beat``. Standalone Beat acquires the renewable Redis lease.
if settings.is_production:
    celery_app.conf.beat_scheduler = (
        "app.core.celery_beat_owner:DoersOwnedPersistentScheduler"
    )

# Celery's ``-A module`` loader looks for a conventional ``app`` attribute.
app = celery_app

__all__ = ["app", "celery_app"]

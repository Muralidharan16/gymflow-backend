"""Compatibility deployment entrypoint for the canonical Celery app.

Docker and any existing process manager may continue to use
``celery -A app.tasks.celery_app ...``.  This module intentionally defines no
second Celery instance or beat schedule; it exports the single application from
``app.core.celery_app``.
"""

from app.core.celery_app import celery_app

# Celery's ``-A module`` loader looks for a conventional ``app`` attribute.
app = celery_app

__all__ = ["app", "celery_app"]

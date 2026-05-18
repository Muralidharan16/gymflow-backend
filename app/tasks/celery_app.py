from celery import Celery
from celery.schedules import crontab
from app.core.config import settings

app = Celery("doers", broker=settings.CELERY_BROKER_URL, backend=settings.CELERY_RESULT_BACKEND)

app.conf.update(
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "expire-subscriptions": {
            "task": "app.tasks.expire_subs.run",
            "schedule": crontab(hour=0, minute=5),
        },
        "member-reminders": {
            "task": "app.tasks.reminders.run",
            "schedule": crontab(hour=10, minute=0),
        },
        "daily-digest": {
            "task": "app.tasks.daily_digest.run",
            "schedule": crontab(hour=8, minute=30),
        },
        "orphan-logo-cleanup": {
            "task": "app.tasks.logos.cleanup_orphaned_logos",
            "schedule": crontab(minute=0, hour="*/6"),
        },
    },
)

# Discover tasks
app.autodiscover_tasks(['app.tasks'])

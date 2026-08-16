"""Legacy trial worker entry point retained only as a fail-closed compatibility stub.

P3E moved trial lifecycle authority to the isolated maintenance control plane.
A stale broker message targeting this historical task must not recover the old
cross-tenant worker scan.
"""

from app.core.celery_app import celery_app


_DISABLED_MESSAGE = (
    "Legacy worker trial monitor is disabled by P3E; "
    "use app.tasks.platform_maintenance.advance_trial_lifecycles"
)


@celery_app.task(name="app.tasks.trial_tasks.monitor_trial_lifecycles")
def monitor_trial_lifecycles():
    raise RuntimeError(_DISABLED_MESSAGE)

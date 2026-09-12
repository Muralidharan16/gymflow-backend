from celery import shared_task


@shared_task(name="app.tasks.covers.process_org_cover")
def process_org_cover(*_args, **_kwargs):
    """Reject stale pre-P3E cover messages carrying trusted authority fields."""

    raise RuntimeError(
        "Legacy cover queue payloads are disabled; enqueue a durable asset job instead"
    )

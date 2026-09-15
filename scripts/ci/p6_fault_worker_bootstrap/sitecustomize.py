"""Test-only Python startup bridge for inherited P5 fault-worker proofs.

The P5-D and P5-W2 pytest controllers intentionally keep their original local
plaintext Redis URL so they can observe/interrupt the disposable broker.  Their
spawned Celery process, however, is a real production worker and must satisfy
the P6 authenticated TLS Redis contract.  The workflows put this directory on
PYTHONPATH only for those explicit destructive-fault jobs and provide alternate
rediss:// URLs that point at the same Redis primary through its TLS port.

Nothing under app/ imports this module.  Outside the exact production worker
subprocesses used by P5-D/P5-W2 this module is a no-op.
"""

from __future__ import annotations

import os


_fault_job = (
    os.environ.get("P5D_PROCESS_FAULTS") == "1"
    or os.environ.get("P5W2_PROCESS_FAULTS") == "1"
)
_production_worker = (
    os.environ.get("ENVIRONMENT", "").strip().lower() == "production"
    and os.environ.get("DOERS_PROCESS_PROFILE", "").strip().lower() == "worker"
)

if _fault_job and _production_worker:
    _mapping = {
        "REDIS_URL": "P6_FAULT_WORKER_REDIS_URL",
        "CELERY_BROKER_URL": "P6_FAULT_WORKER_CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND": "P6_FAULT_WORKER_CELERY_RESULT_BACKEND",
    }
    for _target, _source in _mapping.items():
        _value = os.environ.get(_source, "").strip()
        if _value:
            os.environ[_target] = _value

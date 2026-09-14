from __future__ import annotations

import pytest


_DEDICATED_P5_RUNTIME_MODULES = {
    "tests/test_p5w_worker_fencing_runtime.py": "P5-W1 worker fencing",
    "tests/test_p5w2_worker_crash_redelivery_runtime.py": "P5-W2 process death and redelivery",
    "tests/test_p5e_provider_ack_ambiguity_runtime.py": "P5-E provider acknowledgement ambiguity",
    "tests/test_p5d_dependency_loss_runtime.py": "P5-D dependency and database loss",
    "tests/test_p5r_race_deadlock_runtime.py": "P5-R race and deadlock interleavings",
    "tests/test_p5c_compensation_crash_replay_runtime.py": "P5-C compensation crash and replay",
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep P5 disposable fault runtimes inside their dedicated same-head gates.

    The decisive P5 workflows invoke these modules with ``--noconftest`` after
    provisioning their exact disposable PostgreSQL/Redis/process-fault topology.
    Broad inherited suites intentionally collect the repository at large; they
    must not execute a fault-injection module without that module's dedicated
    topology, credentials, acknowledgements and safety guards.
    """
    del config
    for item in items:
        for module, description in _DEDICATED_P5_RUNTIME_MODULES.items():
            if item.nodeid.startswith(module):
                item.add_marker(
                    pytest.mark.skip(
                        reason=f"{description} runs only in its dedicated same-head gate"
                    )
                )
                break

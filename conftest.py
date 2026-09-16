from __future__ import annotations

import os

import pytest


_DEDICATED_P5_RUNTIME_MODULES = {
    "tests/test_p5w_worker_fencing_runtime.py": "P5-W1 worker fencing",
    "tests/test_p5w2_worker_crash_redelivery_runtime.py": "P5-W2 process death and redelivery",
    "tests/test_p5e_provider_ack_ambiguity_runtime.py": "P5-E provider acknowledgement ambiguity",
    "tests/test_p5d_dependency_loss_runtime.py": "P5-D dependency and database loss",
    "tests/test_p5r_race_deadlock_runtime.py": "P5-R race and deadlock interleavings",
    "tests/test_p5c_compensation_crash_replay_runtime.py": "P5-C compensation crash and replay",
}

_DEDICATED_PROCESS_FAULT_RUNTIME_MODULES = {
    "tests/test_p6b_broker_reconnect_runtime.py": (
        "P6-B broker restart and reconnect",
        "P6B_PROCESS_FAULTS",
    ),
    "tests/test_p6w_worker_shutdown_redelivery_runtime.py": (
        "P6-W worker shutdown and late-ack redelivery",
        "P6W_PROCESS_FAULTS",
    ),
    "tests/test_p6p_poison_message_runtime.py": (
        "P6-P poison-message containment",
        "P6P_POISON_FAULTS",
    ),
    "tests/test_p6s_scheduler_runtime.py": (
        "P6-S scheduler ownership and duplicate protection",
        "P6S_PROCESS_FAULTS",
    ),
    "tests/test_p8_production_like_observability_runtime.py": (
        "P8-O production-like observability faults",
        "P8O_PROCESS_FAULTS",
    ),
    "tests/test_p8o_stuck_lifecycle_runtime.py": (
        "P8-O stuck lifecycle observability faults",
        "P8O_PROCESS_FAULTS",
    ),
    "tests/test_p8o_otlp_sink_loss_runtime.py": (
        "P8-O OTLP sink-loss observability faults",
        "P8O_PROCESS_FAULTS",
    ),
    "tests/test_p8o_database_disconnect_runtime.py": (
        "P8-O database-disconnect observability faults",
        "P8O_PROCESS_FAULTS",
    ),
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep destructive disposable runtimes inside their dedicated same-head gates.

    The decisive P5/P6/P8 workflows provision exact disposable PostgreSQL/Redis
    and process-fault topology before invoking these modules. Broad inherited
    suites intentionally collect the repository at large; they must not execute
    a fault-injection module without that module's dedicated topology,
    credentials, acknowledgements and safety guards.
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
        else:
            for module, (
                description,
                activation_flag,
            ) in _DEDICATED_PROCESS_FAULT_RUNTIME_MODULES.items():
                if item.nodeid.startswith(module):
                    if os.environ.get(activation_flag) != "1":
                        item.add_marker(
                            pytest.mark.skip(
                                reason=(
                                    f"{description} runs only in its dedicated same-head gate"
                                )
                            )
                        )
                    break

from __future__ import annotations

import os

import pytest


_P5C_RUNTIME = "tests/test_p5c_compensation_crash_replay_runtime.py"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep destructive P5-C process-kill proofs inside their dedicated gate.

    The P5-C workflow executes the runtime with ``--noconftest`` and the explicit
    disposable-database/process-fault acknowledgements. Broad inherited suites
    intentionally collect the repository at large; they must not turn an absent
    destructive-test opt-in into a regression failure.
    """
    del config
    if os.environ.get("P5C_PROCESS_FAULTS") == "1":
        return

    marker = pytest.mark.skip(
        reason="P5-C destructive process faults run only in the dedicated same-head gate"
    )
    for item in items:
        if item.nodeid.startswith(_P5C_RUNTIME):
            item.add_marker(marker)

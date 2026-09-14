"""Narrow subprocess import bootstrap for the P5-C crash harness.

Python places the executed script's directory first on ``sys.path``.  The P5-C
fault harness intentionally executes its compensation child as a standalone
script so the child can be killed independently.  In that one mode, expose the
repository root before the child imports production ``app`` modules.

This file is inert for ordinary pytest/test processes because it is only
importable at interpreter startup when ``tests/`` is already on ``sys.path``;
the explicit environment/argv guard narrows it further to the destructive P5-C
child process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


if (
    os.environ.get("P5C_PROCESS_FAULTS") == "1"
    and Path(sys.argv[0]).name == "test_p5c_compensation_crash_replay_runtime.py"
):
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)

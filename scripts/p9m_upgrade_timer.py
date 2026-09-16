#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


EXPECTED_PREDECESSOR = "zj07d8e9f0a44"
EXPECTED_HEAD = "zk07d8e9f0a45"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    ns = parser.parse_args()

    before = _run(sys.executable, "-s", "-m", "alembic", "-c", "alembic.ini", "current")
    if before.returncode != 0 or EXPECTED_PREDECESSOR not in before.stdout:
        sys.stderr.write(before.stdout + before.stderr)
        raise SystemExit("P9-M upgrade timer requires exact zj07 predecessor")

    started_ns = time.monotonic_ns()
    upgrade = _run(
        sys.executable,
        "-s",
        "-m",
        "alembic",
        "-c",
        "alembic.ini",
        "upgrade",
        EXPECTED_HEAD,
    )
    finished_ns = time.monotonic_ns()

    combined = upgrade.stdout + upgrade.stderr
    ns.log.parent.mkdir(parents=True, exist_ok=True)
    ns.log.write_text(combined, encoding="utf-8")
    sys.stdout.write(combined)

    elapsed_ms = (finished_ns - started_ns) / 1_000_000.0
    after = _run(sys.executable, "-s", "-m", "alembic", "-c", "alembic.ini", "current")

    evidence = {
        "schema_version": 1,
        "clock": "time.monotonic_ns",
        "predecessor": EXPECTED_PREDECESSOR,
        "target": EXPECTED_HEAD,
        "upgrade_returncode": upgrade.returncode,
        "duration_ms": elapsed_ms,
        "post_current_returncode": after.returncode,
        "post_current_stdout": after.stdout.strip(),
    }
    ns.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if upgrade.returncode != 0:
        return upgrade.returncode
    if after.returncode != 0 or EXPECTED_HEAD not in after.stdout:
        sys.stderr.write(after.stdout + after.stderr)
        raise SystemExit("P9-M upgrade did not reach exact zk07 head")

    print(f"P9M_UPGRADE_DURATION_MS={elapsed_ms:.3f}")
    print("P9M_UPGRADE_DURATION_RECORDED=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

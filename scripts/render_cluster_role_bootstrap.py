"""Render the canonical create-only PostgreSQL cluster-role bootstrap to stdout."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.cluster_role_bootstrap import (
    BootstrapContractError,
    render_fresh_cluster_bootstrap,
)


def main() -> int:
    try:
        sys.stdout.write(render_fresh_cluster_bootstrap())
    except (BootstrapContractError, KeyError, TypeError, ValueError) as exc:
        print(f"cluster role bootstrap manifest rejected: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

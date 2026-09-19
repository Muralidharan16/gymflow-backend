from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.cluster_role_bootstrap import render_fresh_cluster_bootstrap
from app.core.pay2_finance_roles import predecessor_contract_bundle


def main() -> int:
    print(render_fresh_cluster_bootstrap(predecessor_contract_bundle()), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

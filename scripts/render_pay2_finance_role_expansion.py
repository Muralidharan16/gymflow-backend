from __future__ import annotations

from app.core.cluster_role_bootstrap import render_fresh_cluster_bootstrap
from app.core.pay2_finance_roles import expansion_contract_bundle


def main() -> int:
    print(render_fresh_cluster_bootstrap(expansion_contract_bundle()), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

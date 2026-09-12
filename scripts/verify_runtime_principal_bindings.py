#!/usr/bin/env python3
"""Verify live P2D runtime PostgreSQL login bindings without printing secrets."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.runtime_binding_alignment import (
    validate_runtime_binding_cluster_setting_alignment,
)
from app.core.runtime_principal_attestation import (
    RuntimePrincipalAttestationError,
    attest_configured_runtime_bindings,
)


def main() -> int:
    alignment = validate_runtime_binding_cluster_setting_alignment()
    if alignment:
        print("P2D/P2B runtime setting alignment FAILED", file=sys.stderr)
        for item in alignment:
            print(
                f"- [{item.code}] {item.subject}: {item.message}",
                file=sys.stderr,
            )
        return 1

    try:
        observations = attest_configured_runtime_bindings()
    except RuntimePrincipalAttestationError as exc:
        print(f"P2D runtime principal attestation FAILED: {exc}", file=sys.stderr)
        return 1

    for observation in observations:
        print(
            "P2D runtime principal OK: "
            f"component={observation.component} "
            f"session_user={observation.session_user} "
            f"database={observation.current_database}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

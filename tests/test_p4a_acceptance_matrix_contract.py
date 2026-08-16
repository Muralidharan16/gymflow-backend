from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs" / "architecture" / "P4A_ACCEPTANCE_MATRIX.md"


def test_acceptance_matrix_contains_all_decisive_p4a_boundaries() -> None:
    text = MATRIX.read_text(encoding="utf-8").lower()
    for phrase in (
        "external effect inventory",
        "fail-closed current handlers",
        "false-success semantics",
        "provider acceptance",
        "ambiguous outcomes",
        "idempotency",
        "lease/fencing",
        "reconciliation",
        "dead-letter",
        "webhook/callback",
        "operator replay",
        "security inheritance",
        "p3e bootstrap guard",
        "one immutable sha",
    ):
        assert phrase in text

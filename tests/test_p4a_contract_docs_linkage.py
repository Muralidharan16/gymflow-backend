from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "architecture" / "P4_EXTERNAL_EFFECTS_CONTRACT.md"
RUNBOOK = ROOT / "docs" / "operations" / "p4-external-effects-recovery.md"


def test_contract_and_runbook_cover_the_same_recovery_boundaries() -> None:
    contract = CONTRACT.read_text(encoding="utf-8").lower()
    runbook = RUNBOOK.read_text(encoding="utf-8").lower()

    for phrase in (
        "dead-letter",
        "operator",
        "reconciliation",
        "ambiguous outcome",
        "idempotency",
        "lease",
        "provider evidence",
    ):
        assert phrase in contract
        assert phrase in runbook

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "operations" / "p4-external-effects-recovery.md"


def test_recovery_runbook_forbids_manual_false_success() -> None:
    text = RUNBOOK.read_text(encoding="utf-8").lower()
    assert "never resolve an external-effect incident" in text
    assert "without authoritative downstream evidence" in text


def test_recovery_runbook_covers_all_p4_domains_and_ambiguous_outcomes() -> None:
    text = RUNBOOK.read_text(encoding="utf-8").lower()
    for phrase in (
        "search drift",
        "notification stuck/ambiguous",
        "refund stuck/ambiguous",
        "dead-letter replay",
        "provider outage",
        "same logical idempotency identity",
        "fresh valid claim/lease",
        "authoritative database state",
    ):
        assert phrase in text

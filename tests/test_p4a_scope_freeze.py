from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCOPE = ROOT / "docs" / "architecture" / "P4A_SCOPE.md"


def test_p4a_scope_forbids_provider_enablement_and_security_widening() -> None:
    text = SCOPE.read_text(encoding="utf-8").lower()
    for phrase in (
        "does **not**",
        "enable a search provider",
        "enable reminder",
        "execute real lifecycle refunds",
        "add provider credentials",
        "alter p3e runtime identities",
        "change rls policies",
        "add bypassrls",
        "widen worker table privileges",
        "change the certified p3e migrations",
    ):
        assert phrase in text

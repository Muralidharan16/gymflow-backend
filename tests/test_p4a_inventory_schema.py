from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "architecture" / "p4_external_effect_inventory.json"


def test_inventory_entries_have_required_operational_fields() -> None:
    data = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["phase"] == "P4A"

    required_event_fields = {
        "event_type",
        "domain",
        "producer",
        "consumer",
        "p4_stage",
        "current_status",
        "terminal_success_requires",
    }
    for entry in data["lifecycle_external_events"]:
        assert required_event_fields <= set(entry)

    required_gap_fields = {
        "id",
        "source",
        "method",
        "p4_stage",
        "risk",
        "required_resolution",
    }
    for entry in data["known_p4_gaps"]:
        assert required_gap_fields <= set(entry)

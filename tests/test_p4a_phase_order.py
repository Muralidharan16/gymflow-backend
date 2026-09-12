from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "architecture" / "p4_external_effect_inventory.json"


def test_p4a_inventory_routes_domains_to_recommended_follow_on_stages() -> None:
    data = json.loads(INVENTORY.read_text(encoding="utf-8"))
    by_event = {entry["event_type"]: entry["p4_stage"] for entry in data["lifecycle_external_events"]}
    assert by_event["branch.search_index"] == "P4B"
    assert by_event["branch.search_deindex"] == "P4B"
    assert by_event["branch.member_notification"] == "P4C"
    assert by_event["branch.refund_required"] == "P4D"

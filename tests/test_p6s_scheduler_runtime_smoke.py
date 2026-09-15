from pathlib import Path


def test_p6s_runtime_contract_markers_are_frozen() -> None:
    source = (Path(__file__).resolve().parents[1] / "docs/architecture/P6S_SCHEDULER_OWNERSHIP.md").read_text(encoding="utf-8")
    for marker in (
        "P6_BEAT_SINGLE_OWNER=PASS",
        "P6_DUPLICATE_SCHEDULER_EFFECT_SAFE=PASS",
        "P6_NO_LOST_DURABLE_BUSINESS_WORK=PASS",
        "P6_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED",
    ):
        assert marker in source

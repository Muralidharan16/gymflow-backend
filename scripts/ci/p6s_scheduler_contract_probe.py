from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCHEDULER = ROOT / "app/core/celery_beat_owner.py"


def main() -> int:
    source = SCHEDULER.read_text(encoding="utf-8")
    required = (
        "DoersOwnedPersistentScheduler",
        "BeatOwnershipLease",
        "CELERY_BEAT_OWNERSHIP_KEY",
        "CELERY_BEAT_OWNERSHIP_TTL_SECONDS",
        "CELERY_BEAT_OWNERSHIP_RETRY_SECONDS",
        "embedded worker -B is forbidden",
    )
    missing = [item for item in required if item not in source]
    if missing:
        raise SystemExit(f"missing P6-S scheduler contract: {missing!r}")
    print("P6S_SCHEDULER_CONTRACT_PROBE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

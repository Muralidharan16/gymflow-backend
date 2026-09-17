from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] if len(Path(__file__).resolve().parents) > 2 else Path.cwd()
EVIDENCE = Path(os.environ["EVIDENCE_DIR"])
STATE_PATH = EVIDENCE / "durable-work-state.json"
RECOVERY_PATH = EVIDENCE / "durable-recovery.json"


def _load_p5w2():
    path = Path.cwd() / "tests" / "test_p5w2_worker_crash_redelivery_runtime.py"
    spec = importlib.util.spec_from_file_location("p9d_p5w2_runtime", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load P5-W2 runtime harness from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _surface(module):
    return next(item for item in module.SURFACES if item.name == "transactional")


def prepare() -> None:
    module = _load_p5w2()
    client = module._safe_broker()
    surface = _surface(module)
    tmp = Path("/tmp/p9d-candidate-worker")
    tmp.mkdir(parents=True, exist_ok=True)
    seed = module._seed(surface)
    boundary = "before_db_commit_kill"
    with module._running_worker(
        tmp,
        target_event_id=seed.event_id,
        fault_mode="before_db_commit",
    ) as crashed_worker:
        crashed = module._send(surface, crashed_worker.queue)
        fault = module._wait_for(
            lambda: module._fault_record(crashed_worker, boundary),
            description="P9-D candidate worker pre-commit death",
        )
        processes = module._wait_for(
            lambda: (
                values
                if len(values := module._task_processes(crashed_worker, crashed.id)) >= 2
                else None
            ),
            description="P9-D candidate broker redelivery after worker death",
        )
        module._wait_for(
            lambda: module._broker_drained(client, crashed_worker.queue),
            description="P9-D candidate redelivery acknowledgement",
        )
        module._assert_claimed_not_committed(seed)
        killed_pid = int(fault["pid"])
        assert killed_pid in processes
        assert any(pid != killed_pid for pid in processes)
        state = {
            "surface": surface.name,
            "org_id": str(seed.org_id),
            "owner_id": str(seed.owner_id),
            "reader_user_id": str(seed.reader_user_id),
            "branch_id": str(seed.branch_id),
            "event_id": str(seed.event_id),
            "candidate_worker_hostname": crashed_worker.hostname,
            "killed_child_pid": killed_pid,
            "claim_state": list(module._state(seed)),
        }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("P9D_DURABLE_WORK_CLAIM_SURVIVED_BAD_RELEASE=PASS")


def recover() -> None:
    module = _load_p5w2()
    client = module._safe_broker()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    surface = _surface(module)
    seed = module._Seed(
        surface=surface,
        org_id=uuid.UUID(state["org_id"]),
        owner_id=uuid.UUID(state["owner_id"]),
        reader_user_id=uuid.UUID(state["reader_user_id"]),
        branch_id=uuid.UUID(state["branch_id"]),
        event_id=uuid.UUID(state["event_id"]),
    )
    module._expire_claim(seed)
    module.ROOT = Path(os.environ["P9D_LKG_SOURCE"]).resolve()
    tmp = Path("/tmp/p9d-lkg-worker")
    tmp.mkdir(parents=True, exist_ok=True)
    with module._running_worker(tmp) as replacement_worker:
        recovered = module._send(surface, replacement_worker.queue)
        result = module._result(recovered)
        assert result["claimed"] == 1
        assert result[surface.success_key] == 1
        module._wait_for(
            lambda: module._broker_drained(client, replacement_worker.queue),
            description="P9-D LKG replacement acknowledgement",
        )
        module._assert_single_terminal_effect(seed, fence=2)
        duplicate = module._result(module._send(surface, replacement_worker.queue))
        assert duplicate["claimed"] == 0
        module._assert_single_terminal_effect(seed, fence=2)
        projection_count, projection_version = module._projection(seed)
        final_state = list(module._state(seed))
        recovery = {
            "lkg_source": str(module.ROOT),
            "replacement_worker_hostname": replacement_worker.hostname,
            "event_id": str(seed.event_id),
            "claimed": result["claimed"],
            "processed": result[surface.success_key],
            "duplicate_claimed": duplicate["claimed"],
            "projection_count": projection_count,
            "projection_version": projection_version,
            "final_state": final_state,
            "lease_fence": 2,
        }
    RECOVERY_PATH.write_text(json.dumps(recovery, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("P9D_DURABLE_WORK_RECOVERED_BY_LAST_KNOWN_GOOD=PASS")
    print("P9D_NO_LOST_DURABLE_WORK=PASS")
    print("P9D_SINGLE_TERMINAL_EFFECT=PASS")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"prepare", "recover"}:
        raise SystemExit("usage: p9d_durable_work_harness.py prepare|recover")
    if sys.argv[1] == "prepare":
        prepare()
    else:
        recover()


if __name__ == "__main__":
    main()

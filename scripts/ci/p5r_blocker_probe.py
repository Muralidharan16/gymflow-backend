from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import uuid


ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNTIME_PATH = ROOT / "tests" / "test_p5r_race_deadlock_runtime.py"
spec = importlib.util.spec_from_file_location("p5r_runtime_probe_target", RUNTIME_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load P5-R runtime module")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def snapshot(label: str) -> None:
    sql = """
    SELECT
      pid::text || '|' || usename || '|' || state || '|' ||
      COALESCE(wait_event_type,'NULL') || '|' || COALESCE(wait_event,'NULL') || '|' ||
      pg_catalog.pg_blocking_pids(pid)::text || '|' ||
      COALESCE(EXTRACT(EPOCH FROM (pg_catalog.clock_timestamp()-xact_start))::int::text,'NULL') || '|' ||
      left(regexp_replace(query, E'\\s+', ' ', 'g'), 360)
    FROM pg_catalog.pg_stat_activity
    WHERE datname='gymflow_p5r_test'
      AND usename IN ('app_test_runtime','worker_test_runtime')
    ORDER BY pid;
    """
    print(f"P5R_BLOCKER_SNAPSHOT_{label}_BEGIN", flush=True)
    print(module._admin_psql(sql, tuples_only=True), flush=True)
    print(f"P5R_BLOCKER_SNAPSHOT_{label}_END", flush=True)


async def main() -> None:
    module._safe_database_topology()
    seed = module._seed(2)
    branch_id = seed.branch_ids[0]
    correlation_id = await module._transition(seed, branch_id, "temporarily_closed")
    parent_id = module._find_correlated(correlation_id, "branch.lifecycle_saga")
    worker_id = uuid.uuid4()
    parent_event = await module._claim_specific(parent_id, worker_id)
    module._install_transaction_b_barrier(branch_id)

    try:
        worker_task = asyncio.create_task(module._process_saga_event(parent_event, worker_id))
        worker_pid = await asyncio.to_thread(module._wait_for_sleeping_worker)
        print(f"P5R_BLOCKER_WORKER_PID={worker_pid}", flush=True)

        api_task = asyncio.create_task(module._attempt_transition(seed, branch_id, "active"))
        await asyncio.sleep(0.30)
        snapshot("EARLY")

        await asyncio.sleep(2.40)
        snapshot("AFTER_BARRIER")

        await asyncio.sleep(1.40)
        snapshot("PRE_TIMEOUT")

        worker_outcome = await worker_task
        api_outcome = await api_task
        print(f"P5R_BLOCKER_WORKER_OUTCOME={worker_outcome!r}", flush=True)
        print(f"P5R_BLOCKER_API_OUTCOME={api_outcome!r}", flush=True)
    finally:
        module._drop_barriers()


if __name__ == "__main__":
    asyncio.run(main())

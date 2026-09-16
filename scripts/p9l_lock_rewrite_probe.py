#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


EXPECTED_PREDECESSOR = "zj07d8e9f0a44"
EXPECTED_HEAD = "zk07d8e9f0a45"
CRITICAL_RELATIONS = (
    "organizations",
    "org_branches",
    "branch_outbox_events",
)
CONTROL_APPS = {
    "p9l_observer",
    "p9l_reader",
    "p9l_writer",
    "p9l_version_blocker",
}


def _dsn() -> str:
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        raise SystemExit("P9-L requires DATABASE_URL")
    return raw.replace("postgresql+asyncpg://", "postgresql://", 1)


def _connect(dsn: str, application_name: str, *, autocommit: bool = False):
    return psycopg.connect(
        dsn,
        application_name=application_name,
        autocommit=autocommit,
        row_factory=dict_row,
    )


def _current_revision(conn) -> str:
    row = conn.execute("SELECT version_num FROM public.alembic_version").fetchone()
    if row is None:
        raise SystemExit("P9-L could not read alembic_version")
    return str(row["version_num"])


def _relation_snapshot(conn) -> dict[str, dict[str, int | str]]:
    rows = conn.execute(
        """
        SELECT c.relname,
               c.oid::bigint AS oid,
               c.relfilenode::bigint AS relfilenode,
               COALESCE(pg_catalog.pg_relation_filenode(c.oid), 0)::bigint
                   AS effective_filenode,
               pg_catalog.pg_relation_size(c.oid)::bigint AS relation_size_bytes,
               pg_catalog.pg_total_relation_size(c.oid)::bigint AS total_size_bytes,
               c.relkind::text AS relkind
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname IN ('organizations', 'org_branches', 'branch_outbox_events')
        ORDER BY c.relname
        """
    ).fetchall()
    result = {str(row["relname"]): dict(row) for row in rows}
    if set(result) != set(CRITICAL_RELATIONS):
        raise SystemExit(f"P9-L critical relation set mismatch: {sorted(result)}")
    return result


def _role_timeout_settings(conn) -> dict[str, dict[str, str | int]]:
    rows = conn.execute(
        """
        SELECT name, setting, unit
        FROM pg_catalog.pg_settings
        WHERE name IN ('lock_timeout', 'statement_timeout')
        ORDER BY name
        """
    ).fetchall()
    result = {}
    for row in rows:
        result[str(row["name"])] = {
            "setting": int(row["setting"]),
            "unit": str(row["unit"]),
        }
    return result


def _relation_locks(conn, pid: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT l.locktype::text AS locktype,
               l.mode::text AS mode,
               l.granted,
               COALESCE(n.nspname, '')::text AS schema_name,
               COALESCE(c.relname, '')::text AS relation_name
        FROM pg_catalog.pg_locks AS l
        LEFT JOIN pg_catalog.pg_class AS c ON c.oid = l.relation
        LEFT JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE l.pid = %s
        ORDER BY l.locktype, schema_name, relation_name, l.mode, l.granted
        """,
        (pid,),
    ).fetchall()
    return [dict(row) for row in rows]


def _assert_control_lock(conn, pid: int, relation: str, mode: str) -> None:
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_locks AS l
            WHERE l.pid = %s
              AND l.relation = %s::regclass
              AND l.mode = %s
              AND l.granted
        ) AS ok
        """,
        (pid, relation, mode),
    ).fetchone()
    if row is None or not bool(row["ok"]):
        raise SystemExit(
            f"P9-L control session {pid} did not hold {mode} on {relation}"
        )


def _migration_sessions(conn) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT pid,
               application_name,
               state,
               wait_event_type,
               wait_event,
               pg_catalog.pg_blocking_pids(pid) AS blocking_pids
        FROM pg_catalog.pg_stat_activity
        WHERE datname = current_database()
          AND usename = current_user
          AND pid <> pg_backend_pid()
        ORDER BY pid
        """
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if str(row["application_name"] or "") not in CONTROL_APPS
    ]


def _dedupe_lock_key(lock: dict[str, object]) -> str:
    return "|".join(
        [
            str(lock["locktype"]),
            str(lock["schema_name"]),
            str(lock["relation_name"]),
            str(lock["mode"]),
            str(bool(lock["granted"])).lower(),
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--migration-log", required=True, type=Path)
    parser.add_argument("--lock-wait-budget-ms", required=True, type=float)
    parser.add_argument("--migration-duration-budget-ms", required=True, type=float)
    parser.add_argument("--controlled-block-hold-ms", required=True, type=float)
    parser.add_argument("--sample-interval-ms", required=True, type=float)
    parser.add_argument("--expected-lock-timeout-ms", required=True, type=int)
    parser.add_argument("--expected-statement-timeout-ms", required=True, type=int)
    ns = parser.parse_args()

    dsn = _dsn()
    ns.evidence.parent.mkdir(parents=True, exist_ok=True)
    ns.migration_log.parent.mkdir(parents=True, exist_ok=True)

    observer = _connect(dsn, "p9l_observer", autocommit=True)
    reader = _connect(dsn, "p9l_reader")
    writer = _connect(dsn, "p9l_writer")
    blocker = _connect(dsn, "p9l_version_blocker")

    blocker_released = False
    process: subprocess.Popen[str] | None = None
    try:
        if _current_revision(observer) != EXPECTED_PREDECESSOR:
            raise SystemExit("P9-L lock probe requires exact zj07 predecessor")

        before = _relation_snapshot(observer)
        timeouts = _role_timeout_settings(observer)
        lock_timeout = timeouts.get("lock_timeout", {})
        statement_timeout = timeouts.get("statement_timeout", {})
        if (
            lock_timeout.get("unit") != "ms"
            or lock_timeout.get("setting") != ns.expected_lock_timeout_ms
        ):
            raise SystemExit(f"P9-L lock_timeout mismatch: {lock_timeout}")
        if (
            statement_timeout.get("unit") != "ms"
            or statement_timeout.get("setting") != ns.expected_statement_timeout_ms
        ):
            raise SystemExit(f"P9-L statement_timeout mismatch: {statement_timeout}")

        reader.execute("LOCK TABLE public.branch_outbox_events IN ACCESS SHARE MODE")
        reader.execute(
            """
            SELECT count(*) AS row_count
            FROM public.branch_outbox_events
            WHERE payload->>'p9m_synthetic' = 'true'
            """
        ).fetchone()
        writer.execute("LOCK TABLE public.branch_outbox_events IN ROW EXCLUSIVE MODE")
        blocker.execute("LOCK TABLE public.alembic_version IN SHARE MODE")

        reader_pid = reader.info.backend_pid
        writer_pid = writer.info.backend_pid
        blocker_pid = blocker.info.backend_pid
        _assert_control_lock(
            observer, reader_pid, "public.branch_outbox_events", "AccessShareLock"
        )
        _assert_control_lock(
            observer, writer_pid, "public.branch_outbox_events", "RowExclusiveLock"
        )
        _assert_control_lock(
            observer, blocker_pid, "public.alembic_version", "ShareLock"
        )

        started_ns = time.monotonic_ns()
        process = subprocess.Popen(
            [
                sys.executable,
                "-s",
                "-m",
                "alembic",
                "-c",
                "alembic.ini",
                "upgrade",
                EXPECTED_HEAD,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=os.environ.copy(),
        )

        sample_interval_s = ns.sample_interval_ms / 1000.0
        hard_deadline_ns = started_ns + int(
            max(ns.migration_duration_budget_ms * 2.0, 20_000.0) * 1_000_000.0
        )
        controlled_first_seen_ns: int | None = None
        controlled_release_ns: int | None = None
        controlled_block_observed = False
        sample_count = 0
        blocked_sample_count = 0
        migration_pids: set[int] = set()
        blocking_pids_seen: set[int] = set()
        lock_records: dict[str, dict[str, object]] = {}
        wait_started_ns: dict[int, int] = {}
        lock_wait_total_ns = 0
        max_lock_wait_ns = 0
        pending_version_rowexclusive_observed = False
        critical_outbox_access_share_observed = False
        unexpected_access_exclusive: list[dict[str, object]] = []

        while process.poll() is None:
            now_ns = time.monotonic_ns()
            if now_ns >= hard_deadline_ns:
                process.terminate()
                raise SystemExit("P9-L migration exceeded hard orchestration deadline")

            sessions = _migration_sessions(observer)
            sample_count += 1
            active_pids = set()
            for session in sessions:
                pid = int(session["pid"])
                active_pids.add(pid)
                migration_pids.add(pid)
                wait_type = str(session["wait_event_type"] or "")
                blockers = [int(value) for value in (session["blocking_pids"] or [])]

                if wait_type == "Lock":
                    blocked_sample_count += 1
                    blocking_pids_seen.update(blockers)
                    wait_started_ns.setdefault(pid, now_ns)
                elif pid in wait_started_ns:
                    waited_ns = now_ns - wait_started_ns.pop(pid)
                    lock_wait_total_ns += waited_ns
                    max_lock_wait_ns = max(max_lock_wait_ns, waited_ns)

                if blocker_pid in blockers:
                    controlled_block_observed = True
                    if controlled_first_seen_ns is None:
                        controlled_first_seen_ns = now_ns

                for lock in _relation_locks(observer, pid):
                    lock_records.setdefault(_dedupe_lock_key(lock), lock)
                    relation_name = str(lock["relation_name"])
                    mode = str(lock["mode"])
                    granted = bool(lock["granted"])
                    schema_name = str(lock["schema_name"])

                    if (
                        relation_name == "alembic_version"
                        and mode == "RowExclusiveLock"
                        and not granted
                    ):
                        pending_version_rowexclusive_observed = True
                    if (
                        relation_name == "branch_outbox_events"
                        and mode == "AccessShareLock"
                        and granted
                    ):
                        critical_outbox_access_share_observed = True
                    if (
                        schema_name == "public"
                        and relation_name in CRITICAL_RELATIONS
                        and mode == "AccessExclusiveLock"
                        and granted
                    ):
                        unexpected_access_exclusive.append(lock)

            for pid in list(wait_started_ns):
                if pid not in active_pids:
                    waited_ns = now_ns - wait_started_ns.pop(pid)
                    lock_wait_total_ns += waited_ns
                    max_lock_wait_ns = max(max_lock_wait_ns, waited_ns)

            if (
                controlled_first_seen_ns is not None
                and not blocker_released
                and (
                    now_ns - controlled_first_seen_ns
                    >= int(ns.controlled_block_hold_ms * 1_000_000.0)
                )
            ):
                blocker.rollback()
                blocker_released = True
                controlled_release_ns = time.monotonic_ns()

            if unexpected_access_exclusive:
                if not blocker_released:
                    blocker.rollback()
                    blocker_released = True
                process.terminate()
                break

            time.sleep(sample_interval_s)

        finished_ns = time.monotonic_ns()
        for pid, wait_start_ns in list(wait_started_ns.items()):
            waited_ns = finished_ns - wait_start_ns
            lock_wait_total_ns += waited_ns
            max_lock_wait_ns = max(max_lock_wait_ns, waited_ns)
            wait_started_ns.pop(pid, None)

        if not blocker_released:
            blocker.rollback()
            blocker_released = True
            controlled_release_ns = time.monotonic_ns()

        migration_output, _ = process.communicate(timeout=5)
        ns.migration_log.write_text(migration_output, encoding="utf-8")
        sys.stdout.write(migration_output)

        reader.rollback()
        writer.rollback()

        duration_ms = (finished_ns - started_ns) / 1_000_000.0
        observed_lock_wait_ms = lock_wait_total_ns / 1_000_000.0
        max_contiguous_lock_wait_ms = max_lock_wait_ns / 1_000_000.0
        controlled_hold_observed_ms = (
            (controlled_release_ns - controlled_first_seen_ns) / 1_000_000.0
            if controlled_first_seen_ns is not None
            and controlled_release_ns is not None
            else 0.0
        )

        after_revision = _current_revision(observer)
        after = _relation_snapshot(observer)
        rewrites = []
        size_deltas = {}
        for relation in CRITICAL_RELATIONS:
            before_row = before[relation]
            after_row = after[relation]
            if before_row["effective_filenode"] != after_row["effective_filenode"]:
                rewrites.append(relation)
            size_deltas[relation] = {
                "relation_size_bytes": (
                    int(after_row["relation_size_bytes"])
                    - int(before_row["relation_size_bytes"])
                ),
                "total_size_bytes": (
                    int(after_row["total_size_bytes"])
                    - int(before_row["total_size_bytes"])
                ),
            }

        changed_heap_sizes = [
            relation
            for relation, delta in size_deltas.items()
            if int(delta["relation_size_bytes"]) != 0
        ]

        lock_wait_within_budget = (
            observed_lock_wait_ms > 0.0
            and observed_lock_wait_ms <= ns.lock_wait_budget_ms
            and max_contiguous_lock_wait_ms <= ns.lock_wait_budget_ms
        )
        migration_within_budget = duration_ms <= ns.migration_duration_budget_ms
        lock_mode_proof = (
            controlled_block_observed
            and pending_version_rowexclusive_observed
            and critical_outbox_access_share_observed
            and blocked_sample_count > 0
            and blocker_pid in blocking_pids_seen
        )
        rewrite_proof = not rewrites and not changed_heap_sizes
        access_exclusive_proof = not unexpected_access_exclusive

        evidence = {
            "schema_version": 1,
            "clock": "time.monotonic_ns",
            "candidate_sha": os.environ.get("P9L_CANDIDATE_SHA", ""),
            "predecessor": EXPECTED_PREDECESSOR,
            "target": EXPECTED_HEAD,
            "budgets": {
                "database_lock_timeout_ms": ns.expected_lock_timeout_ms,
                "database_statement_timeout_ms": ns.expected_statement_timeout_ms,
                "observed_lock_wait_budget_ms": ns.lock_wait_budget_ms,
                "migration_wall_clock_budget_ms": ns.migration_duration_budget_ms,
                "controlled_metadata_block_hold_ms": ns.controlled_block_hold_ms,
                "sample_interval_ms": ns.sample_interval_ms,
            },
            "postgres_settings": timeouts,
            "control_sessions": {
                "reader_pid": reader_pid,
                "reader_lock": "AccessShareLock",
                "writer_pid": writer_pid,
                "writer_lock": "RowExclusiveLock",
                "version_blocker_pid": blocker_pid,
                "version_blocker_lock": "ShareLock",
            },
            "migration": {
                "returncode": process.returncode,
                "duration_ms": duration_ms,
                "pids_observed": sorted(migration_pids),
                "post_revision": after_revision,
            },
            "lock_observation": {
                "sample_count": sample_count,
                "blocked_sample_count": blocked_sample_count,
                "blocking_pids_seen": sorted(blocking_pids_seen),
                "controlled_block_observed": controlled_block_observed,
                "controlled_hold_observed_ms": controlled_hold_observed_ms,
                "observed_lock_wait_ms": observed_lock_wait_ms,
                "max_contiguous_lock_wait_ms": max_contiguous_lock_wait_ms,
                "pending_version_rowexclusive_observed": (
                    pending_version_rowexclusive_observed
                ),
                "critical_outbox_access_share_observed": (
                    critical_outbox_access_share_observed
                ),
                "observed_locks": list(lock_records.values()),
                "unexpected_access_exclusive": unexpected_access_exclusive,
            },
            "relations": {
                "before": before,
                "after": after,
                "size_deltas": size_deltas,
                "rewrites_detected": rewrites,
                "changed_heap_sizes": changed_heap_sizes,
            },
            "decisions": {
                "lock_wait_within_budget": lock_wait_within_budget,
                "migration_within_budget": migration_within_budget,
                "lock_mode_proof": lock_mode_proof,
                "no_unexpected_access_exclusive": access_exclusive_proof,
                "no_table_rewrite": rewrite_proof,
            },
        }
        ns.evidence.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        failures = []
        if process.returncode != 0:
            failures.append(f"migration return code {process.returncode}")
        if after_revision != EXPECTED_HEAD:
            failures.append(f"post revision is {after_revision}")
        if not migration_pids:
            failures.append("no migration backend PID was observed")
        if not controlled_block_observed:
            failures.append("controlled metadata blocker was not observed")
        if not pending_version_rowexclusive_observed:
            failures.append("pending RowExclusiveLock on alembic_version was not observed")
        if not critical_outbox_access_share_observed:
            failures.append("migration AccessShareLock on branch_outbox_events was not observed")
        if not lock_wait_within_budget:
            failures.append(
                "observed migration lock wait was zero or exceeded frozen budget"
            )
        if not migration_within_budget:
            failures.append("migration wall-clock duration exceeded frozen budget")
        if unexpected_access_exclusive:
            failures.append("unexpected AccessExclusiveLock on populated relation")
        if rewrites:
            failures.append(f"table rewrite detected: {rewrites}")
        if changed_heap_sizes:
            failures.append(f"critical heap relation size changed: {changed_heap_sizes}")

        print(f"P9L_MIGRATION_DURATION_MS={duration_ms:.3f}")
        print(f"P9L_OBSERVED_LOCK_WAIT_MS={observed_lock_wait_ms:.3f}")
        print(
            "P9L_MAX_CONTIGUOUS_LOCK_WAIT_MS="
            f"{max_contiguous_lock_wait_ms:.3f}"
        )
        print(f"P9L_BLOCKED_SAMPLES={blocked_sample_count}")
        print(
            "P9L_OBSERVED_MIGRATION_LOCKS="
            + ",".join(sorted(lock_records))
        )

        if failures:
            for failure in failures:
                print(f"P9L_FAILURE={failure}", file=sys.stderr)
            return 1

        print("P9L_CONTROLLED_CONTENTION_OBSERVED=PASS")
        print("P9L_ACQUIRED_LOCK_MODES_CAPTURED=PASS")
        print("P9L_NO_UNEXPECTED_ACCESS_EXCLUSIVE=PASS")
        print("P9L_LOCK_WAIT_BOUNDED=PASS")
        print("P9_LOCK_BUDGET=PASS")
        print("P9_TABLE_REWRITE_ANALYSIS=PASS")
        print("P9_MIGRATION_DURATION_MEASURED=PASS")
        print("P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED")
        return 0
    finally:
        for conn in (blocker, writer, reader, observer):
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())

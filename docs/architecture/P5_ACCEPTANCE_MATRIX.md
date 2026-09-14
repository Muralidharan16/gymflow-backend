# P5 Acceptance Matrix

Base: merged and certified P4 commit
`99de1600636979fa355f8ba8ff665f98dc8148bf` with tree
`766243d0bdd0ccd1377d57ae726222f04ff4cf1c`.

P5 is complete only when every row below has decisive runtime evidence on the
same immutable SHA. A prior P4 proof is inherited protection, not a substitute
for the P5 fault injection named here.

| Gate | Required result |
|---|---|
| Worker death before DB commit | The attacked transaction leaves no partial durable state; an eligible job remains reclaimable |
| Worker death after DB commit | Broker redelivery observes the committed state and cannot reproduce the logical effect |
| Provider success / DB acknowledgement failure | The result remains non-terminal until same-identity replay or reconciliation persists authoritative evidence; no duplicate provider effect |
| Duplicate Celery/outbox delivery | Concurrent and sequential duplicates converge on one logical command/effect and one terminal evidence set |
| Lease expiry and reclaim | Reclaim rotates ownership authority; every stale completion/failure write is rejected, including ABA-style replay |
| Redis/broker/network loss | Business state remains authoritative in PostgreSQL; restart/reconnect produces bounded retry, reconciliation or dead letter without false success |
| DB disconnects | Claim, mutation and acknowledgement disconnects roll back or recover according to the named commit point; no job disappears |
| Deadlocks/concurrent lifecycle transitions | One authoritative transition wins; losers are explicit and retry-safe; no child effect is duplicated |
| Finance/lifecycle races | Locked current state produces no stale amount/tenant authority and at most one deterministic refund obligation |
| Compensation crash/replay | Replacement execution completes or safely re-observes one compensation; no aggregate remains frozen without recovery disposition |
| Inherited P1-P4 boundaries | Security, migration, provider-evidence and runtime-identity suites remain green on the P5 candidate |
| Same-head proof | All decisive P5 jobs and inherited gates execute on one immutable candidate SHA |

## Terminal hard gate

P5 fails if any decisive test permits a lost update, duplicate financial effect,
stuck transition or unrecoverable job. Skipped, cancelled, timed-out, neutral or
missing decisive jobs are failures, not acceptable evidence.

P5-G freezes this matrix only. It does not claim runtime P5 certification,
merge readiness, release readiness or deployment readiness.

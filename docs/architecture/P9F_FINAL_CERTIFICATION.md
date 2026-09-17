# P9-F — Final Same-Head Certification

## Purpose

P9-F is the terminal certification gate for Phase 9 — data protection, upgrade safety and disaster recovery. It adds no product behavior. It proves that the complete P9 candidate, every P9 recovery/compatibility gate, and every inherited P1-P8 hardening gate succeed on one immutable Git commit before any integration decision.

## Immutable parent checkpoint

P9-F is rooted in the certified P9-D bad-deployment rollback checkpoint:

- P9 implementation branch: `hardening/p9-data-protection-disaster-recovery`
- Certified P9-D HEAD: `63f927bef51c167d995d47ae6a65c458e38f0762`
- Certified P9-D tree: `ce88550621b94e2ecdcae12e4143cae92ff19647`
- Certified P9-D workflow run: `35209866867`
- Current Alembic head: `zk07d8e9f0a45`
- Frozen P9 hard gate: `upgrade_and_recovery_from_bad_deployment_or_data_loss_proven`

Same-head P9 evidence at that checkpoint:

- P9-G governance: `35209867024` — success
- P9-M populated predecessor rehearsal: `35209866838` — success
- P9-L lock/rewrite/duration analysis: `35209866872` — success
- P9-B backup integrity/restoreability: `35209866866` — success
- P9-R full restore/PITR/RPO/RTO: `35209866847` — success
- P9-C rolling schema/application compatibility: `35209866890` — success
- P9-D bad deployment rollback/recovery: `35209866867` — success

P9-F must remain a certification-only descendant of that exact P9-D checkpoint.

## Final certification topology

P9-F binds exactly **53 prerequisite gates** before the terminal decision:

- **46 inherited P1-P8 same-head jobs** are re-executed inside the P9-F workflow using the proven P8-F topology.
- The **seven P9 slice workflows** (P9-G, P9-M, P9-L, P9-B, P9-R, P9-C and P9-D) are required as successful normal `push` workflow runs on the exact same `GITHUB_SHA`.

The seven P9 workflows are intentionally not invoked a second time from P9-F. They already carry `cancel-in-progress` concurrency groups on the P9 branch; duplicate reusable invocations on the same ref could race with and cancel the canonical sibling push runs. P9-F instead uses read-only Actions metadata to bind their exact workflow path, event, head SHA, completion state and successful conclusion.

The terminal job must run with `if: always()`, fail if any of the 46 inherited jobs is not `success`, fail if any required P9 sibling run is missing, pending or non-successful, reject topology counts other than 46 + 7 = 53, and emit machine-readable evidence.

## Required P9 invariants

P9-F must preserve and reprove the complete Phase 9 contract:

- real PostgreSQL 16 populated predecessor-to-HEAD migration rehearsal;
- representative business, RLS, ACL, ownership and runtime-principal integrity across migration;
- measured and bounded migration lock wait, wall-clock duration and rewrite analysis;
- checksummed logical backup and verified physical base backup;
- isolated full restore and named-target PITR with pre-target survival and post-target exclusion;
- measured RPO/RTO within the frozen synthetic CI targets;
- old/new application overlap and rollback on the upgraded schema without destructive contract migration;
- bad-release detection, traffic return to last-known-good and recovery of committed durable work;
- no duplicate terminal/financial effects and no lost durable work;
- PostgreSQL remains durable business authority;
- Redis/Celery/Beat remain coordination/delivery infrastructure;
- backup/recovery artifacts remain evidence rather than business authority;
- restored/certification runtimes cannot contact live providers;
- refund-provider execution remains `deferred_fail_closed`;
- production/customer data is never copied into CI.

## Inherited safety invariants

P9-F must not weaken any certified P1-P8 boundary. The inherited 46-job topology reproves architecture, migrations, Finance, external-effect authority, worker crash/redelivery, dependency-loss, race/deadlock, compensation, Redis/broker/scheduler production boundaries, API drain/shutdown orchestration, and the complete P8 observability/actionability contract on the same candidate SHA.

## Hard stops and non-goals

P9-F does **not** authorize or perform:

- merge to `main`;
- tag or release creation;
- deployment or production configuration changes;
- production/customer data copy into CI;
- destructive database downgrade as first-line rollback;
- privilege broadening to make certification pass;
- refund-provider activation;
- live money movement.

Any failed gate is a hard stop. A code/workflow repair creates a new candidate SHA and requires the full final same-head certification to run again on that exact head. Integration remains a separate explicitly authorized lifecycle step.

## Terminal evidence and markers

A successful terminal decision writes:

- `p9f-evidence/inherited-needs.json`
- `p9f-evidence/p9-same-head-runs.json`
- `p9f-evidence/decision.json`

The final decision must show 46 inherited gates, seven P9 slice gates, total prerequisite count 53, Alembic head `zk07d8e9f0a45`, PostgreSQL durable authority, production-data-copy `forbidden`, refund execution `deferred_fail_closed`, and decision `PASS`.

The terminal job must emit the frozen P9 acceptance markers, including:

```text
P9_DATA_LOSS_RECOVERY=PASS
P9_P1_P8_INHERITED=PASS
P9_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
P9_UPGRADE_AND_RECOVERY_PROVEN=PASS
P9F_FINAL_SAME_HEAD_CERTIFICATION=PASS
```

Only `P9F_FINAL_SAME_HEAD_CERTIFICATION=PASS` together with `P9_UPGRADE_AND_RECOVERY_PROVEN=PASS` on an immutable candidate where all 53 prerequisite gates are proven constitutes final Phase 9 certification. Integration remains a separate explicitly authorized lifecycle step.

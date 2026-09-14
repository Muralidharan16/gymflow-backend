# P5-C — Compensation Crash and Replay

## Certified predecessor

P5-C starts only from the immutable P5-R certification and does not reopen
P5-G, P5-W1, P5-W2, P5-E, P5-D or P5-R.

- P5-R commit: `49877b224fd987b6f1d90b78db96bdc4074aa2aa`
- P5-R tree: `7fc97c9a76c06641140036e0b89ef521e164ade3`
- P5-R workflow run: `34817614212`
- P5-R workflow job: `103891609964`
- certified Alembic head: `zj07d8e9f0a44`
- P5-R static/governance contracts: `79 passed`
- P5-R real race/deadlock runtime: `6 passed`
- P5-R inherited boundary reproof: `91 passed`

P5-C initially adds no schema migration or new runtime privilege. The exact
database head therefore remains `zj07d8e9f0a44`.

## Scope

P5-C certifies the frozen P5 fault `compensation_crash_and_replay` against the
production lifecycle compensation path. It proves process-death recovery at the
two decisive transaction boundaries without provider refund execution.

### C1 — Process death before compensation commit

A lifecycle saga is driven to its bounded retry limit and held by a live,
fenced worker lease. A disposable PostgreSQL trigger pauses the compensation
transaction after compensation writes have begun but before the transaction can
commit. The worker process is killed with `SIGKILL`.

The independent verification connection must prove that every uncommitted
compensation mutation rolled back:

- authoritative branch state is still the Transaction-A target and remains
  `lifecycle_transition_in_progress`;
- no `compensation_completed` event committed;
- no `saga_compensation` history row committed;
- no compensation search-restoration child committed;
- the parent remains `processing` under the killed worker's fence.

CI may expire that already-owned lease after process death. A replacement
worker must reclaim the same parent, receive a strictly newer `lease_fence`, and
complete compensation. The replacement commit must atomically persist the
restored branch state, one logical compensation event/history/effect set, and
the parent dead-letter disposition.

### C2 — Process death after compensation commit before task acknowledgement

A separate compensation worker performs the same exhausted compensation. After
`_fail_event` has returned, its database transaction is durably committed. The
test process emits a local commit marker and then intentionally remains alive
without crossing its simulated task-acknowledgement boundary. The controller
kills that process with `SIGKILL`.

A replacement execution then redelivers the same persisted event identity.
Because the parent is already terminal and the branch no longer has an
in-progress lifecycle transition, replay must converge on the committed truth.
It may not create another compensation event, history row, search-restoration
child, or financial/provider effect.

The canonical Celery app remains configured with `task_acks_late=True` and
`task_reject_on_worker_lost=True`; P5-W2 already certified real Redis
redelivery. P5-C uses a direct separately-killable worker process so the exact
compensation database commit can be deterministically attacked without adding
a production fault hook.

### C3 — Single logical compensation set

For each correlation, successful recovery must leave exactly:

- one `compensation_completed` lifecycle event;
- one `BranchStatusHistory` row with `transition_source='saga_compensation'`;
- one deterministic `branch.search_index` compensation child;
- one terminal parent durable job.

The search child identity remains deterministic through the existing UUIDv5
construction. Refund-provider execution remains deferred and fail-closed.

### C4 — No frozen unrecoverable aggregate from the attacked crashes

After the pre-commit crash, the aggregate is temporarily frozen only while the
killed owner's finite lease remains authoritative. Once that lease is expired,
replacement ownership is a durable recovery path. After recovery, the branch is
stable and the parent is terminal.

After the post-commit crash, the aggregate is already compensated and stable;
redelivery can only re-observe terminal durable state. Neither crash may leave
partial compensation or require an administrative business-state rewrite.

## Runtime requirements

P5-C decisive evidence must use:

- disposable local PostgreSQL 16;
- exact Alembic head `zj07d8e9f0a44`;
- reduced production-equivalent auth/API/worker login identities with RLS on;
- a real separately killable OS process for each compensation crash;
- `SIGKILL`, not an in-process exception presented as process death;
- independent database verification after process death;
- a database-clock lease expiry followed by replacement-worker reclaim;
- monotonic lease-fence rotation and stale-owner rejection;
- PostgreSQL trigger barriers only as disposable CI fault-injection
  infrastructure;
- finite statement, lock and idle-transaction timeouts;
- one immutable Git SHA and clean same-head evidence.

The PostgreSQL superuser is CI infrastructure only. It may install/remove the
disposable barrier, inspect committed state and expire the already-owned killed
lease. It may not perform application compensation, broaden runtime privilege,
or bypass the worker's fenced recovery path.

## Hard failures

Any of the following fails P5-C:

- any compensation mutation survives the killed pre-commit transaction;
- the killed lease cannot be reclaimed by a replacement worker;
- replacement ownership does not advance `lease_fence`;
- compensation commits more than one logical event/history/search effect set;
- stale post-commit redelivery mutates already compensated state;
- a crash leaves the branch frozen without a finite lease or terminal recovery
  disposition;
- refund-provider execution is activated;
- a broad table grant, `BYPASSRLS`, superuser application identity or
  administrative business-state repair is introduced;
- source changes during certification or any decisive job is skipped/neutral.

## Exclusions

P5-C does not certify P5-F aggregate certification, refund-provider execution,
live provider credentials, PR, merge, tag, release or deployment.

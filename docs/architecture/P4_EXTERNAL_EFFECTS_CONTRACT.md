# P4 External Business Effects Contract

Status: P4A architecture contract
Base: certified P3E SHA `882406537584861da2b2b6d44fd37b016a9f8462`

## 1. Purpose

P4 completes real external business integrations without weakening the certified P1/P2/P3 database, tenant, runtime-identity, RLS, worker, maintenance, lifecycle, or migration boundaries.

The governing rule is:

> A local command, task attempt, successful HTTP write, queue acknowledgement, log line, or provider request submission is not proof that the external business effect succeeded.

DOERS may expose or persist a terminal external-success state only when durable downstream evidence supports that state.

This contract applies to at least:

- branch search indexing and de-indexing;
- lifecycle member notifications;
- reminder, birthday and digest notifications when re-enabled;
- lifecycle-driven Finance/refund execution;
- external provider webhooks/callbacks;
- reconciliation and operator replay of those effects.

## 2. Current P3E baseline and P4 inventory

The certified P3E line already persists lifecycle external commands in `public.branch_outbox_events`. The current lifecycle external event types are:

- `branch.search_index`
- `branch.search_deindex`
- `branch.member_notification`
- `branch.refund_required`

`app/tasks/branch_outbox_poller.py` deliberately fails these commands closed because no production handler is wired. That fail-closed behavior remains correct until the relevant P4 provider boundary is certified.

The current lifecycle service also has a search reconciliation path that advances local search synchronization markers without downstream search-provider evidence. P4B must replace that marker-only behavior; a reconciliation sweep may not declare synchronization merely because a database update succeeded.

P3E deliberately leaves the historical global reminder and digest entry points fail-closed. P4C may re-enable notification products only through tenant-bound durable discovery and delivery commands satisfying this contract.

The Finance domain already contains payment, refund, credit-note, ledger, provider-event, idempotency and outbox primitives. P4D must connect lifecycle refund obligations to those authoritative Finance records and then to the real refund provider without trusting queue-supplied amounts or treating request submission as refund completion.

## 3. Canonical external-effect lifecycle

Each logical effect must have a durable internal identity and move through states equivalent to the following model. Domain-specific names are allowed, but their semantics must not be weakened.

1. `pending`
   - authoritative internal intent exists;
   - no provider success is claimed.
2. `processing`
   - one fenced worker owns a live lease;
   - an attempt may be made.
3. `provider_accepted` (when the provider exposes acceptance separately from completion)
   - provider returned a durable reference/acknowledgement;
   - business completion is not yet claimed unless provider semantics make acceptance terminal for that effect.
4. `succeeded`
   - durable downstream evidence proves the requested business effect completed or is authoritatively present at the desired version/state.
5. `retry_pending`
   - last attempt failed or was ambiguous and retry is safe under the same logical idempotency identity.
6. `dead_lettered`
   - automatic attempts are exhausted or the command is permanently invalid;
   - operator action/reconciliation is required.
7. `cancelled` / `superseded`
   - authoritative local state makes the old intent obsolete;
   - stale workers are fenced from committing success.

No state machine may transition directly from `pending`/`processing` to terminal success solely because application code reached the end of a handler.

## 4. Required durable evidence

For every external effect, persist or make reconstructable at minimum:

- internal command/effect ID;
- tenant/organization ID where applicable;
- authoritative aggregate/entity ID;
- effect type;
- deterministic idempotency key;
- desired state or desired version;
- command payload hash or canonical request hash;
- current status;
- attempt count and maximum/terminal policy;
- next eligible attempt time;
- lease/fence owner and expiry/token;
- provider code;
- provider request/reference ID when available;
- provider event/callback ID when available;
- provider acknowledgement/evidence hash or normalized evidence record;
- first-attempted timestamp;
- last-attempted timestamp;
- provider-acknowledged timestamp when applicable;
- terminal/completed timestamp;
- last classified error;
- correlation/trace ID;
- dead-letter reason when applicable.

A domain may use dedicated tables rather than one generic table. The semantic contract is shared even when storage is not.

## 5. Idempotency contract

The same logical business obligation must reuse the same deterministic idempotency identity across retries, broker redelivery, process crashes and reconciliation repair.

An idempotency identity must be derived from authoritative local data. It must not be an arbitrary queue-provided key that can redirect authority.

At minimum:

- duplicate task delivery must not create duplicate external effects;
- retry after timeout must not create a second refund/message/document version when the first may already exist;
- a changed business request must produce a new logical version or new idempotency identity rather than mutating the meaning of an existing key;
- stale/superseded work must not overwrite newer desired state.

## 6. Lease and fencing contract

External effects must use exclusive bounded claims where concurrent processing is possible.

Required properties:

- bounded batches;
- `FOR UPDATE SKIP LOCKED` or an equivalent safe claim primitive for global reconciliation/discovery;
- finite lease duration;
- a fence token or equivalent ownership check on terminal writes;
- an expired worker may not commit success after a newer worker owns the command;
- lease loss is not provider success;
- operator replay creates/obtains a new valid claim rather than bypassing fencing.

## 7. Provider outcome taxonomy

Every adapter must classify provider outcomes into explicit categories.

### Definite success

The provider gives authoritative evidence that the requested effect exists/completed at the required identity/version. The evidence is persisted before or atomically with the local terminal-success transition.

### Provider accepted / not terminal

The provider accepted the request and returned a durable provider reference, but final business completion is asynchronous. Persist the provider reference and remain non-terminal until callback/reconciliation evidence establishes final outcome.

### Definite permanent rejection

Examples include invalid destination, non-retryable validation failure or an authoritative provider rejection. The command may move to terminal failure/dead letter according to domain policy. It must not be marked successful.

### Definite retryable failure

Examples include rate limits, provider 5xx, temporary dependency failure or an explicit retryable provider status. Retry with backoff and the same logical idempotency identity.

### Ambiguous outcome

Timeout, connection loss or process crash can occur after the provider may have accepted the request. Ambiguous outcomes must not be converted to success or blindly re-issued under a new identity. They enter retry/reconciliation logic using the same idempotency identity/provider lookup capability.

## 8. Retry and backoff contract

Retries must be bounded and classified, not blanket exception loops.

Required behavior:

- exponential backoff with a bounded ceiling;
- jitter where many commands may become eligible simultaneously;
- provider `Retry-After` or equivalent respected when applicable;
- permanent validation/rejection failures are not retried forever;
- ambiguous outcomes prefer provider lookup/reconciliation before unsafe recreation when the provider supports it;
- max-attempt exhaustion produces a durable dead-letter state, not disappearance from the queue.

## 9. Reconciliation contract

Every external-effect domain must have a bounded reconciliation path able to compare authoritative local desired state with authoritative downstream evidence.

Reconciliation must:

- discover bounded candidates through the maintenance/control-plane identity when discovery is global;
- not seed global tenant context or widen worker database rights;
- claim rows with lease/fencing semantics;
- isolate per-item failures;
- respect provider pagination/rate limits;
- repair missing effects;
- remove/compensate stale effects where the domain requires it;
- detect local-success/provider-missing contradictions;
- detect provider-success/local-pending contradictions;
- persist reconciliation evidence;
- never advance a local `synced`, `sent`, `delivered`, `indexed`, `deindexed` or `refunded` marker merely because the reconciliation SQL itself succeeded.

## 10. Dead-letter and operator recovery contract

Dead-letter is a durable operational state, not a log message.

Required capabilities:

- query dead-lettered effects with correlation and provider evidence;
- distinguish permanent rejection from retry exhaustion/ambiguity;
- controlled replay/reconciliation command;
- replay authorization independent of arbitrary provider request payloads;
- reconstruction of provider requests from authoritative local records;
- immutable audit of replay, cancellation or forced resolution;
- runbooks for provider outage and drift recovery.

No operator endpoint may accept arbitrary tenant IDs, provider IDs, destination addresses, amounts or object identities and pass them directly to an external provider as authority.

## 11. Webhook/callback contract

When a provider supports callbacks/webhooks:

- verify provider authenticity before applying business state;
- enforce replay/idempotency using provider event identity and/or payload hash;
- map callbacks to pre-existing authoritative internal/provider references;
- do not let the callback select an arbitrary tenant;
- tolerate duplicate and out-of-order events;
- persist callback evidence before/with state transition;
- an unverified callback can never produce terminal business success.

## 12. Domain-specific success evidence

### Search

`indexed` means the configured search provider contains the desired branch document/version or has returned an acknowledgement whose semantics are authoritatively terminal for that operation.

`deindexed` means authoritative provider evidence proves the document is absent/deleted at the required version.

A local timestamp/version increment alone is never search success.

### Notifications

`queued` means DOERS created durable intent.

`provider_accepted` means the provider returned a durable message/request reference.

`delivered` means the provider's authoritative delivery status/callback/reconciliation reports delivery when that channel exposes delivery confirmation.

A successful HTTP submission is never relabelled `delivered` when the provider only acknowledges acceptance.

### Refunds

`requested` / `processing` means DOERS has a refund obligation/attempt.

`provider_accepted` or provider-specific processing means the provider accepted the refund and returned a durable reference.

`FinanceRefund.status = 'succeeded'` requires authoritative provider evidence that the refund succeeded. Lifecycle command creation or provider request submission is not refund success.

## 13. Security boundary

P4 must preserve the certified P1/P2/P3 principles:

- no RLS weakening;
- no `BYPASSRLS`;
- no broad table/column grants to make integrations convenient;
- no migration/admin credential fallback for workers;
- no queue, Redis, callback or provider payload treated as authorization authority;
- secrets exposed only to the processes/adapters that require them;
- TLS for production provider traffic;
- provider secrets and sensitive payloads are redacted from logs;
- global discovery remains maintenance/control-plane work and does not grant cross-tenant ORM authority to tenant workers.

Any new database capability must be least-privilege, principal/context-bound where applicable, migration-certified and independently regression tested.

## 14. Observability contract

Each provider boundary must expose enough telemetry to detect business inconsistency, including:

- pending/backlog count;
- age of oldest pending command;
- processing lease count/age;
- retries by classified reason;
- provider latency and response class;
- ambiguous outcome count;
- dead-letter count/age;
- reconciliation drift count;
- successful reconciliation repairs;
- domain-specific terminal success/failure counts.

Alerts should target business inconsistency and stuck obligations, not only transport exceptions.

## 15. P4 execution sequence

P4 will proceed in this order:

1. P4A — external-effect architecture, inventory and hard gate.
2. P4B — real lifecycle search index/de-index plus reconciliation.
3. P4C — durable real notification delivery, provider evidence and DLQ/reconciliation.
4. P4D — lifecycle to Finance/refund integration and provider reconciliation.
5. P4E — cross-domain operational recovery, observability and final external-effect certification.
6. Final P4 same-head regression/certification including inherited P1/P2/P3.

## 16. P4A hard-stop criteria

P4A is complete only when repository contracts prove:

- the four lifecycle external event types are inventoried;
- the lifecycle outbox external handler remains fail-closed until a certified provider handler exists;
- reminder/digest legacy global paths remain fail-closed during P4A;
- search reconciliation cannot be accepted as production-complete while it only advances local markers;
- Finance refund infrastructure exists but lifecycle refund commands are not falsely treated as completed;
- the canonical evidence/idempotency/lease/reconciliation/dead-letter requirements in this document cannot be removed silently;
- inherited P3E certification boundaries remain unchanged.

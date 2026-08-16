# P4 External Effects Recovery Runbook

Status: P4A baseline runbook. Domain-specific commands are added only when their provider stages are certified.

## Non-negotiable rule

Never resolve an external-effect incident by changing a local row to `sent`, `delivered`, `indexed`, `deindexed`, `refunded` or equivalent terminal success without authoritative downstream evidence.

## First response

For a stuck or contradictory external effect:

1. identify the internal effect/command ID and correlation ID;
2. identify the authoritative tenant/aggregate from persisted DOERS records, not operator input;
3. inspect current lease/fence ownership;
4. inspect attempt count, last classified error and next eligible attempt;
5. inspect persisted provider reference/event IDs;
6. query or reconcile with the provider using that authoritative reference where supported;
7. preserve ambiguous outcomes as non-terminal until reconciled;
8. use controlled replay only after the current lease is absent/expired and replay authorization succeeds;
9. record the operator action and evidence.

## Search drift

Examples:

- database says branch should be visible but provider document is missing;
- database says branch should be hidden but provider document remains;
- document exists at an obsolete version;
- local search sync marker exists without current provider evidence.

Required response:

- do not advance local sync markers by SQL alone;
- compare desired database state/version with provider state;
- reissue the same logical indexed/deindexed obligation or a newer authoritative version;
- fence stale work;
- persist provider evidence after repair.

## Notification stuck/ambiguous

Examples:

- provider request timed out after submission;
- provider accepted a message but delivery callback is missing;
- duplicate webhook arrives;
- destination is permanently rejected.

Required response:

- do not convert provider acceptance into delivery;
- reuse the same logical idempotency identity;
- query/reconcile provider status before unsafe recreation when possible;
- deduplicate callbacks by authoritative provider event/reference;
- dead-letter permanent rejection or exhausted ambiguity according to the certified channel policy.

## Refund stuck/ambiguous

Examples:

- refund submission timed out;
- provider reports success while Finance remains processing;
- Finance has a processing refund but provider has no matching refund;
- duplicate lifecycle command exists.

Required response:

- never create a new refund from queue-supplied amount/reference data;
- reconstruct amount/payment/refund identity from authoritative Finance records;
- reconcile using persisted provider payment/refund references;
- use the same logical idempotency identity for safe retry;
- only mark `FinanceRefund.status = 'succeeded'` from authoritative provider evidence;
- create accounting/credit-note effects according to the certified Finance contract.

## Dead-letter replay

Replay must:

- be explicitly authorized;
- obtain a fresh valid claim/lease;
- reconstruct the provider request from authoritative database state;
- preserve the original logical idempotency identity unless the business obligation itself changed;
- never accept an arbitrary provider destination, amount, tenant or object ID from an operator request;
- create an immutable audit record for replay/cancellation/forced reconciliation.

## Provider outage

During a provider outage:

- keep durable commands pending/retryable;
- apply bounded exponential backoff and provider rate-limit instructions;
- alert on backlog age and dead-letter growth;
- do not bypass the provider/evidence boundary to make dashboards green;
- after recovery, reconcile ambiguous attempts before bulk replay.

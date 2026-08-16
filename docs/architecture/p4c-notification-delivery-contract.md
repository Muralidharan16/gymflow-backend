# P4C Durable Notification Delivery Contract

P4C is stacked on certified P4B SHA `351fe7680fcf0614bd651b49cd8aae11e689d5e8`.
The certified P4A/P4B external-effect, PostgreSQL identity, RLS, and least-privilege
boundaries remain authoritative.

## Governing truth rule

A queue row, Celery attempt, provider HTTP 2xx, SMTP hand-off, local log line, or
provider API submission is **not** proof that a member received a notification.
P4C keeps local intent, provider acceptance, and terminal provider evidence as
separate states.

A notification may become `delivered` only from durable downstream delivery
evidence appropriate to the selected provider/channel. Providers that expose
acceptance but no terminal delivery evidence may end at `provider_accepted` and
must never be relabelled `delivered` by elapsed time or local retry exhaustion.

## Authorization and tenant authority

1. Queue payloads are data, never recipient or tenant authorization authority.
2. A worker must claim a live leased notification through a bounded database
   capability that re-reads current tenant/member/contact/preference state.
3. Lifecycle `branch.member_notification` payloads may identify the lifecycle
   event/branch, but recipient expansion is derived from authoritative
   PostgreSQL state.
4. Current communication suppression/preferences are rechecked before each new
   external provider effect.
5. Webhook payloads cannot select a tenant. Provider message/event identifiers
   must resolve to an existing internal notification command first.
6. Operator replay reconstructs recipient/template/channel from authoritative
   state. Operator input may identify a notification to recover, but may not
   supply an arbitrary destination or message body.

## Durable entities

P4C requires a tenant-bound notification command and immutable-ish provider
attempt/evidence records. The command carries the logical notification identity,
recipient member reference, channel/template, provider state, lease/fence,
retry schedule, correlation, and terminal/dead-letter state. Provider attempts
carry request/evidence hashes and provider identifiers without making logs the
system of record.

The logical idempotency key is deterministic and unique for one intended
recipient/channel/logical business notification. Retries reuse that identity.

## State machine

Allowed command states are:

- `queued`
- `sending`
- `provider_accepted`
- `delivered`
- `rejected`
- `bounced`
- `dead_lettered`
- `suppressed`
- `cancelled`

Only `queued` work with a due retry time, or an expired `sending` lease, may be
claimed. A claimant receives a fence token/lease and stale workers cannot
acknowledge, reject, or reschedule after ownership changes.

`provider_accepted` is not automatically retryable: ambiguous outcomes are
reconciled against provider state or a provider-supported idempotency key before
any second external effect is attempted.

## Failure semantics

- transport failure before a provable provider commit point: retryable;
- unknown commit point: ambiguous and reconciliation-first;
- provider 429/5xx: bounded retry respecting `Retry-After` where available;
- permanent destination/template/provider validation failure: rejected;
- retry exhaustion without terminal downstream success: dead-lettered;
- worker crash: lease expiry permits fenced reclaim;
- database acknowledgement failure after provider acceptance: next execution
  reconciles provider state/idempotency before sending again.

No failure path may synthesize `delivered`.

## Provider boundary

Provider adapters return classified outcomes plus evidence, not booleans.
Provider-specific credentials are available only to the ordinary worker/webhook
surface that requires them. P4C must not spread email/WhatsApp credentials to
API, maintenance, beat, or Flower merely for convenience.

Email and WhatsApp are independent channels. Enabling one does not imply the
other is configured or production-ready.

## Webhook boundary

Where a provider exposes delivery callbacks:

- verify the provider signature before parsing authoritative effect data;
- use replay-safe provider event IDs / signed timestamps where available;
- persist webhook-event idempotency;
- tolerate duplicate and out-of-order events;
- bind provider message ID to the internal command before deriving tenant;
- persist a hash of accepted provider evidence;
- reject an event that attempts an impossible or regressive state transition.

## Reconciliation and recovery

Maintenance may perform only bounded discovery/enqueue/reconciliation control
work. It does not gain direct notification-provider success authority.
Reconciliation covers stale sending leases, ambiguous attempts, accepted
notifications missing terminal evidence, and missed/duplicate webhooks.

Dead-letter/operator recovery is explicit, audited, and never implemented by
manual edits to delivery timestamps/provider IDs/evidence hashes.

## Communication policy

P4C must enforce current recipient/channel eligibility and suppression before a
new provider effect. Invalid/bounced destinations are suppressible. A later
preference or contact change affects future attempts and must not be bypassed by
stale queue data.

## Initial business flow

The first admitted business flow is lifecycle `branch.member_notification`.
`branch.refund_required` remains fail-closed for P4D. Legacy reminder, birthday,
and digest schedulers remain disabled until each has a bounded tenant-authorized
discovery path feeding this same durable notification contract.

## Security invariants

P4C must not introduce RLS weakening, BYPASSRLS, PUBLIC DML/capability execution,
broad runtime table grants, worker ownership escalation, migration/admin
credential fallback, global tenant-context seeding, or direct provider-success
state writable by ordinary application code.

## Certification rule

P4C is complete only when all decisive P4C gates are green on one immutable SHA.
Any repository commit after that SHA invalidates same-head certification and
requires the decisive matrix to rerun.

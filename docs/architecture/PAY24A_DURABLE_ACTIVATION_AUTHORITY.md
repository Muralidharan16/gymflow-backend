# PAY-24-A — Durable Activation Authority

## Status and immutable starting point

This document is the design contract for PAY-24-A. Its current status is
`implementation_pending`; local certification is pending. Nothing in this
document is human authorization for Stage 1, permission to deploy, or proof
that a provider boundary is wired.

PAY-24-A starts from this exact immutable repository state:

- commit SHA: `e7fa726b946ef2444b62377b5ca3a3fe0a3128e8`;
- tree: `f8c1c88f3e8d13c66ed5f3aa9c68a101ab3762c4`;
- Alembic head: `zz47d8e9f0a64`;
- PAY-24-A revision: `zz57d8e9f0a65`, revising
  `zz47d8e9f0a64`.

The certified PAY-23 worktree is reference-only. The failed PAY-24 research
worktree is also reference-only: its entitlement-claim work and process-level
configuration approach are not the architecture of this slice.

## Inspected predecessor and the PAY-22 gap

PAY-22 defines the right eight capability names, progressive rollout stages,
an exact-SHA comparison, an egress fence, and fail-closed policy decisions. Its
authority is nevertheless process-local:

- `ActivationRuntime`, `ActivationAuthorization`, and `KillSwitches` are
  immutable Python values supplied when a service is constructed;
- `PaymentActivationService.with_kill_switches`, `with_provider_egress`, and
  `transition` return a new in-process service rather than changing a shared
  authority;
- the PAY-22 authorization value does not bind `authorized_stage` in the
  policy object, even though the separate Stage-0 JSON envelope contains a
  stage field;
- `deployed_sha` and `certified_sha` are caller-provided strings, not a
  measured release identity;
- there is no durable generation, compare-and-swap transition, cross-process
  serialization, append-only transition evidence, or provider-admission
  lease.

Consequently two processes can hold different postures, a Stage-0 evidence
object is not structurally prevented from being reused for a requested Stage
1 posture, and rollback cannot fence an operation already admitted in another
process. PAY-24-A moves that authority into PostgreSQL. PAY-22 remains useful
as predecessor policy vocabulary, but its Python objects are not the PAY-24-A
production security boundary.

## Canonical identities and ownership

The inspected role bootstrap provides these relevant identities:

- `migration_owner`: the sole application migration LOGIN; `NOINHERIT` and
  `NOBYPASSRLS`, with explicit SET-only edges to approved owner roles;
- `app_security_owner`: `NOLOGIN`, `NOINHERIT`, and `NOBYPASSRLS`; owner of
  reviewed `app_secure` SECURITY DEFINER functions;
- `finance_config_runtime`: `NOLOGIN` and `NOBYPASSRLS`; the separately bound
  Finance control-plane capability;
- `finance_payment_runtime`: `NOLOGIN` and `NOBYPASSRLS`; the bounded provider
  payment-operation capability, currently reserved/unbound by the runtime
  binding manifest;
- `finance_refund_runtime`: `NOLOGIN` and `NOBYPASSRLS`; the peer-isolated
  refund provider-operation capability, also reserved/unbound;
- `finance_read_runtime`: `NOLOGIN` and `NOBYPASSRLS`; the bounded Finance
  operational read capability;
- `finance_maintenance_runtime`: `NOLOGIN` and `NOBYPASSRLS`; the bounded
  Finance maintenance capability;
- `lifecycle_maintenance_runtime`: `NOLOGIN` and `NOBYPASSRLS`; bounded
  maintenance capability;
- `app_runtime` and `worker_runtime`: ordinary API and worker capabilities,
  both `NOLOGIN` and `NOBYPASSRLS`, with no PAY-24-A table mutation authority.

Deployment logins inherit their designated capability with `ADMIN FALSE`,
`INHERIT TRUE`, and `SET FALSE`. No runtime can assume
`app_security_owner`, and `migration_owner` does not inherit runtime
capabilities.

PAY-24-A uses no new login and grants no `BYPASSRLS`, schema ownership,
migration privilege, or broad table access to a runtime. The five durable
tables are private behind bounded functions. Functions are owned by the
non-login `app_security_owner`, are SECURITY DEFINER only where required, set
a fixed safe `search_path`, set `row_security=on`, revoke EXECUTE from PUBLIC,
and perform an in-function caller-role check as defense in depth. The tables
remain owned by `migration_owner`; only `app_security_owner` receives the
exact SELECT/INSERT/UPDATE privileges needed to implement the functions.

## Durable schema

All PAY-24-A state is in the `finance` schema:

| Relation | Purpose |
| --- | --- |
| `finance.payment_activation_release_identities` | Immutable bindings of a certified candidate SHA to a separately measured deployed SHA and attestor/operation identity. |
| `finance.payment_activation_authorizations` | Immutable, stage-specific human authorization metadata: authorization ID, authorized SHA, authorized stage, authorizer, authorization time, and a non-secret evidence identity. |
| `finance.payment_activation_authority` | Exactly one current authority row, including stage, monotonically increasing generation/concurrency version, selected release and authorization bindings, internal organization UUID, provider-egress posture, all eight switches, update identity/time, and posture digest. |
| `finance.payment_activation_transition_events` | Append-only, hash-chained evidence for every successful material authority change, with prior/new generation and stage, binding identities, actor/operation identity, timestamp, and resulting posture digest. |
| `finance.provider_admission_leases` | Durable, idempotent authority for one logical provider operation, bound to generation, organization, capability, logical operation identity, expiry, lifecycle state, and non-secret outcome metadata. |

There is one deterministic singleton authority key. A uniqueness/check
constraint prevents a second current row. Direct table DML is not granted to
normal runtimes. RLS is enabled and forced where installed; only the narrowly
defined owner policy needed by the SECURITY DEFINER routines can reach the
rows.

The initial row is Stage 0, generation zero, provider egress blocked, no
organization, no active release/authorization binding, and all capabilities
off. An absent trusted release or human authorization therefore cannot become
an active posture by migration alone.

This first durable slice represents only Stages 0 and 1. Supporting PAY-22
Stages 2–5 will require a later, separately reviewed schema extension; an
unknown or out-of-range stage is not accepted now.

### Database-level posture constraints

The authority row and transition functions jointly enforce these invariants:

- Stage 0 has provider egress `blocked`, no internal organization, and every
  capability off.
- Stage 1 has exactly one non-null, server-controlled internal organization
  UUID. Email, slug, organization name, role name, frontend flags, and request
  classification are never cohort authority.
- Stage 1 requires an authorization whose `authorized_stage` is exactly 1 and
  whose `authorized_sha` equals both stored certified SHA and separately
  measured deployed SHA.
- A Stage-0 authorization cannot satisfy Stage 1. Missing, malformed,
  future-dated, wrong-stage, or wrong-SHA evidence fails closed.
- The eight independent switches are exactly `checkout`, `webhooks`,
  `payment_application`, `subscription_activation`, `refund_execution`,
  `recurring_billing`, `dunning`, and `platform_billing`.
- The Stage-1-ready shape can enable checkout, webhooks, payment application,
  and subscription activation independently while refund execution, recurring
  billing, dunning, and Platform Billing remain off.

No Stage-1 authorization is included, created, or implied. Tests may create
synthetic authorizations inside isolated test databases only.

## Trusted release identity versus transition input

Release measurement is deliberately separate from transition requests.
`app_secure.pay24a_bind_release_identity` accepts data only through the
restricted release-attestation/control-plane boundary and persists the
certified/measured pair. `app_secure.pay24a_transition_activation` consumes
the current, previously bound release identity from the singleton; it accepts
no certified or deployed SHA argument and does not treat transition input as
proof of what is deployed.

The future deployment attestor is responsible for deriving the running image's
Git SHA from immutable, signed release metadata (or an equivalent protected
platform measurement), checking the candidate approved by release automation,
and invoking the binding function through the dedicated deployment identity.
Environment text, a frontend value, and arbitrary application input are not a
measurement. If that attestor has not installed a valid binding, transition
and admission fail closed.

Local tests may inject a fake implementation of the trusted
release-identity-provider interface. Production construction must use the
deployment-attested provider and must not offer direct arbitrary construction
of a trusted identity from request data.

Human evidence is installed separately through
`app_secure.pay24a_bind_human_authorization`. The durable record binds at
least `authorization_id`, `authorized_sha`, `authorized_stage`,
`authorized_by`, and `authorized_at`. The transition consumes that immutable
record by identity and revalidates its full binding inside the same database
transaction.

## Generation, transition, and idempotency protocol

Every materially relevant change locks the singleton authority row, validates
an expected generation, validates the full target posture, and then advances
generation exactly once. This includes a change to stage, egress posture,
internal organization, any switch, human-authorization binding, or trusted
release/certification binding.

Forward stage movement is same-stage or exactly one stage. A skipped forward
stage is rejected. Emergency rollback may move directly to any lower stage,
including Stage 1 to Stage 0.

Each request carries a non-secret operation identity. The operation identity
is unique in transition evidence:

- an exact replay returns the already committed result without another
  generation increment;
- reuse with a different request digest is rejected;
- two distinct concurrent requests for one expected generation serialize on
  the authority row, so at most one can consume that generation;
- invalid or stale requests write neither authority state nor success
  evidence.

For a successful request, authority update, generation increment, and the
append-only evidence row commit atomically. The evidence records prior/new
generation, prior/new stage, authorization identity and stage, exact SHA
identity, actor/operation identity, transition time, and a deterministic
digest of the resulting non-secret posture.

## Provider-admission lease protocol

PAY-24-A creates a control-plane primitive, not a provider adapter. A future
provider path must use this sequence:

1. request admission before any network effect;
2. in one transaction, lock/read current authority and verify expected
   generation, exact trusted SHA binding, stage-bound authorization, exact
   internal organization, requested capability, open egress, and bounded
   admission arguments;
3. create or idempotently recover one durable lease for the logical provider
   operation;
4. commit the `admitted` lease;
5. in a new short transaction, start that same lease, rechecking its expiry
   and current generation/egress fence;
6. commit the `active` transition **before any provider HTTP/I/O**; only a
   durably active lease may authorize the external call;
7. perform provider I/O with the same logical/idempotency identity;
8. record and commit the terminal result in a later transaction.

The unique logical-operation identity yields one durable admission under
concurrent duplicate requests. Reuse with a different organization,
capability, or request digest is rejected. An expired or terminal lease is
never silently recycled into authority for a new side effect.

The organization is resolved from the verified transaction-local
`app.current_org_id` database context; it is intentionally absent from the
Python and SQL request-admission arguments. The payment and refund capability
roles are peer-isolated, and the function checks that the caller's role is
allowed for the specific requested provider capability.

The lifecycle is:

| State | Meaning and allowed consequence |
| --- | --- |
| `admitted` | Committed but provider I/O has not started. It may move to `active` only while unexpired and while its generation is still current with egress open. |
| `active` | The provider call may have begun. Rollback cannot assume that no side effect occurred. |
| `completed` | The caller durably recorded a known completed outcome. The lease cannot be started again. |
| `expired` | An unstarted lease passed its deadline. It conveys no authority for any later side effect. |
| `revoked` | An unstarted lease was invalidated by rollback or generation fencing. It conveys no authority. |
| `unknown` | An active call crashed, timed out, or otherwise lost a trustworthy outcome. It conveys no retry authority and requires provider reconciliation under the same idempotency identity. |

The bounded functions are
`app_secure.pay24a_request_provider_admission`,
`app_secure.pay24a_start_provider_admission`, and
`app_secure.pay24a_finish_provider_admission`. No PAY-24-A function performs
HTTP, stores a provider secret, or moves money.

## Linearized emergency rollback

Rollback is two phase so "closed" cannot hide a lease that is still free to
start:

`closing` is a one-way rollback state. While it is current, ordinary activation
transitions and release/authorization rebinds are denied; only bounded admission
expiry/drain work and rollback finalization may progress the control plane.

1. `app_secure.pay24a_begin_emergency_rollback` locks the authority, changes
   egress from open to closing, advances generation, writes transition
   evidence, and commits. From that commit onward, all new admissions fail and
   stale-generation leases cannot start.
2. Unstarted prior-generation admissions are revoked or expire. Active leases
   must become completed or unknown; a crashed active caller is classified
   unknown at bounded expiry rather than being silently treated as unused.
   `app_secure.pay24a_expire_provider_admissions` performs the bounded state
   maintenance, and `app_secure.pay24a_admission_drain_snapshot` exposes only
   aggregate drain state.
3. `app_secure.pay24a_finalize_emergency_rollback` succeeds only after there
   is no prior lease in `admitted` or `active`. It installs the complete Stage
   0 invariant, keeps egress blocked, advances generation again, and records
   atomic evidence.

`completed`, `expired`, `revoked`, and `unknown` are terminal and never permit
a new start. Unknown outcomes remain explicitly visible for reconciliation;
they are not proof of failure, proof of success, or permission to retry. A
future provider integration must reuse the provider idempotency identity and
resolve the unknown outcome before deciding on any compensating operation.

## Function/ACL matrix

The intended least-privilege matrix is shown below. Ownership never implies a
runtime membership edge, and PUBLIC has no EXECUTE authority.

| Function | Purpose | Runtime EXECUTE authority |
| --- | --- | --- |
| `pay24a_bind_release_identity` | Persist trusted certified/deployed release measurement | `finance_config_runtime` |
| `pay24a_bind_human_authorization` | Persist immutable stage-bound human metadata | `finance_config_runtime` |
| `pay24a_transition_activation` | Compare-and-swap full target posture | `finance_config_runtime` |
| `pay24a_begin_emergency_rollback` | Close new admissions and advance generation | `finance_config_runtime` |
| `pay24a_expire_provider_admissions` | Revoke/expire unstarted leases and classify expired active work | `finance_config_runtime`, `finance_maintenance_runtime`, `lifecycle_maintenance_runtime` |
| `pay24a_finalize_emergency_rollback` | Install Stage 0 only after drain | `finance_config_runtime` |
| `pay24a_activation_snapshot` | Bounded current non-secret posture | `finance_config_runtime`, `finance_read_runtime`, `finance_maintenance_runtime`, `lifecycle_maintenance_runtime` |
| `pay24a_transition_evidence` | Bounded append-only transition projection | `finance_config_runtime`, `finance_read_runtime`, `finance_maintenance_runtime`, `lifecycle_maintenance_runtime` |
| `pay24a_request_provider_admission` | Create/idempotently recover one operation lease | `finance_payment_runtime`, `finance_refund_runtime` |
| `pay24a_start_provider_admission` | Fence and mark the committed lease active | `finance_payment_runtime`, `finance_refund_runtime` |
| `pay24a_finish_provider_admission` | Record completed or unknown outcome | `finance_payment_runtime`, `finance_refund_runtime` |
| `pay24a_admission_drain_snapshot` | Aggregate non-secret rollback/drain observation | `finance_config_runtime`, `finance_read_runtime`, `finance_maintenance_runtime`, `lifecycle_maintenance_runtime` |

`app_runtime`, `worker_runtime`, `app_user`, and PUBLIC receive neither direct
table DML nor mutation-function EXECUTE. The provider capability remains
reserved/unbound until a later deployment slice explicitly provisions a login
and wires a provider implementation.

## Bounded observation and immutable evidence

`app_secure.pay24a_activation_snapshot` returns only current stage,
generation, egress state, enabled capability names, the internal-canary UUID
to an authorized operational caller, current authorization ID, and safe
certified/measured SHA metadata. It does not return an authorization document,
provider secret, customer data, or cross-tenant financial rows.

`app_secure.pay24a_transition_evidence` returns the bounded transition fields
needed to prove generation and authorization binding. Transition history is
append-only and protected by immutable-history guards compatible with PAY-16;
fixture cleanup must not disable those guards. Failed transitions do not
create success evidence.

`app_secure.pay24a_admission_drain_snapshot` returns aggregate counts needed
to decide whether rollback can finalize, including admitted, active,
completed, expired, revoked, and unknown states. It does not expose another
tenant's payment objects.

## Migration lifecycle

Upgrade from `zz47d8e9f0a64` is fail-closed:

- require the canonical reduced roles and exact migration-owner SET edge;
- require `session_user = current_user = migration_owner`;
- reject unexpected pre-existing PAY-24-A types, relations, functions,
  policies, grants, or singleton state;
- create objects without broad CASCADE cleanup;
- install exact ownership, RLS, fixed function settings, PUBLIC revocations,
  and explicit EXECUTE grants;
- seed only the inert Stage-0 singleton.

Downgrade is safe only while the PAY-24-A store remains pristine. It refuses
before dropping anything if release measurements, human authorizations,
provider admissions, non-bootstrap transition evidence, or a non-initial
authority generation exist. That refusal preserves financial/security
evidence. For a pristine store it revokes only PAY-24-A grants, removes exact
functions/policies/types/tables with `RESTRICT`, and supports
upgrade–downgrade–upgrade without privilege drift. Externally managed roles
are never created, repaired, or dropped by Alembic.

## Deliberate exclusions and terminal posture

PAY-24-A does not modify the PAY-5 entitlement claim/consume functions. It
does not claim to protect entitlement mutation, and it does not introduce an
alternate entitlement path. It also does not integrate checkout, member
payments, refunds, recurring billing, dunning, Platform Billing, or any other
provider adapter with the admission functions. No live provider request is
made.

The required terminal posture remains:

```text
PAY24_PROVIDER_BOUNDARIES_WIRED=NO
PAY24_ENTITLEMENT_BOUNDARY_WIRED=NO
PAY24_STAGE1_LIVE_ACTIVATION=NOT_AUTHORIZED
PAY24_REAL_MONEY_MOVEMENT=0
PAY23_ENTERPRISE_PAYMENT_SYSTEM=NOT_CERTIFIED
```

No PAY-24-A PASS marker is asserted until focused static/runtime tests,
PostgreSQL 16 concurrency tests, ACL/ownership proofs, migration round-trip,
and the required Finance, Platform Billing, PAY-22, and PAY-23 regressions have
all completed locally.

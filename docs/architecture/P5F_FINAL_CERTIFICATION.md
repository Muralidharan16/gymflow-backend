# P5-F Final Same-Head Certification

Status: final P5 certification contract

Certified P5-C predecessor commit: `4c21830edafda605b17194037ba0d78fe0492a86`

Certified P5-C predecessor tree: `baef13057c8671e373255a9d9c2ab342e531b58d`

Required Alembic head: `zj07d8e9f0a44`

Candidate branch: `hardening/p5f-final-certification-temp`

## 1. Purpose

P5-F is a certification-only fan-in. It adds no new business behavior, worker
capability, database authority, provider execution or Finance policy. Its only
purpose is to run the complete P5 fault-tolerance evidence together with the
inherited P1-P4 security, migration, provider-evidence and runtime-identity
gates on one immutable Git SHA and make one terminal decision.

The machine-readable topology is
`docs/architecture/p5f_certification_matrix.json`.

## 2. Same-head rule

Every reusable workflow in the final topology is resolved from the P5-F
candidate itself. Every current-head-aware P3/P4/P5 workflow receives
`zj07d8e9f0a44` explicitly. Any source change creates a new candidate and
invalidates every earlier decisive P5-F result.

Historical P4E workflows remain unchanged. P5-F uses two certification-only
wrappers to re-execute the inherited P4E external-effect contract and its
PostgreSQL 16 operational runtime semantics against the current P5 migration
head. This prevents an old `zf07d8e9f0a40` head assertion from weakening or
blocking current-head inheritance.

P5-E intentionally replaced the legacy unfenced P4B/P4C provider functions
with lease-fence-bearing overloads. P5-F therefore uses current-head
compatibility gates for the inherited P4B evidence, P4B drift and P4C
notification logical slots. Those gates re-run their historical source/provider
contracts against the P5 candidate and bind them to the single decisive P5-E
runtime gate. P5-E itself re-runs the P4B evidence/provider/adversarial/drift
suite and P4C notification/provider/inherited suite after upgrading to the
current P5 head. Historical P4B/P4C workflows remain unchanged and their
obsolete provider capabilities are never re-granted.

P5-W2 explicitly re-runs the P5-W1 stale-owner fencing runtime on the current
head. The P5-W1 logical slot therefore uses a certification-only compatibility
gate that freezes W1 static contracts and binds them to the single decisive
P5-W2 runtime gate. This avoids the historical W1 ACL assertion that predates
P5-D's bounded `INSERT(lease_fence)` column authority while still forbidding
table-wide INSERT and lease-fence UPDATE.

The final fan-in calls P5-E exactly once and P5-W2 exactly once. This is
required because their reusable workflows use `cancel-in-progress` concurrency
groups; duplicating either decisive workflow inside one aggregate could cancel
a sibling proof and can never count as certification.

## 3. Required inherited gates

P5-F preserves the complete Final P4 regression surface: architecture and
runtime-boundary contracts; general, Platform Billing and Finance regressions;
full migration lifecycle, preservation, adversarial recovery and semantics;
P1-P3 certification; maintenance boundaries; real OpenSearch behavior; P4B
provider evidence and drift repair; P4C notification evidence; P4D refund
obligation authority; and P4E external-effect/operational observability.

These are protection gates. A historical green P4 run is not enough: the
inherited gates must execute from the same P5-F candidate SHA.

Broad inherited pytest suites must not execute the dedicated P5 fault-runtime
modules merely because they collect the repository. The repository-level test
hook routes the W1 fencing, W2 worker-death/redelivery, P5-E provider-ack,
P5-D dependency-loss, P5-R race/deadlock and P5-C compensation-crash runtime
modules out of broad suites. Those tests require their own disposable database,
broker, reduced identities, process/network fault acknowledgements or race
barriers and are not ordinary general-regression tests.

No decisive P5 runtime proof is skipped by P5-F. Each corresponding P5 workflow
invokes its runtime explicitly with `--noconftest` after provisioning the exact
certified disposable topology, so the repository-level broad-suite routing hook
cannot affect the decisive same-head fault proof. The terminal fan-in still
requires all P5-G/W1/W2/E/D/R/C logical gates to report `success`.

## 4. Required P5 gates

P5-F executes all of the following from the same candidate:

- P5-G governance and frozen fault matrix;
- P5-W1 monotonic claims, fencing and stale-owner rejection;
- P5-W2 real worker crash, Redis redelivery and duplicate convergence;
- P5-E provider-success/local-ack ambiguity recovery;
- P5-D Redis/provider-network/PostgreSQL dependency loss;
- P5-R real lifecycle, worker and Finance race/deadlock interleavings;
- P5-C compensation process death and replay convergence.

The terminal job fails unless every required reusable workflow and the P5-F
contract job reports exactly `success`. Skipped, cancelled, neutral, timed-out
or missing decisive gates are failures.

## 5. Terminal hard gate

P5-F cannot pass if any decisive evidence permits a lost update, duplicate
financial effect, stuck transition or unrecoverable job. The final job checks
all prerequisite results before emitting its marker.

No live refund API call is introduced or authorized. Refund-provider execution
remains `DEFERRED_FAIL_CLOSED`; no live money movement or live provider
credential is permitted. Telemetry, queue labels and operator input never become business authority.

## 6. Runtime and migration authority

Current-head PostgreSQL gates use PostgreSQL 16 and reduced production-shaped
identities with RLS enabled. Infrastructure/migration authority may provision
disposable databases and observe test state, but it must not broaden API or
worker table authority to make a proof pass.

The P5-F P4E operational wrapper generates disposable CI passwords inside the
runner and exports them only for the verification/provisioning commands that
require them. It proves the inherited P4E static and runtime semantics after
upgrading to the exact P5 head. Full current-head migration lifecycle and
reversibility remain independently owned by the inherited migration-lifecycle
gates.

## 7. Scope and post-pass state

P5-F may change only final-certification artifacts, reusable certification
topology, test-only runtime routing, and exact inherited certification
inventories proven stale by the same-head fan-in. It must not modify production
behavior, schema semantics, RLS policy, worker authority, Finance authority,
provider adapters or entitlement policy.

This phase does not perform or authorize a merge, retarget, tag, release or
deployment. A passing P5-F candidate requires a separate exact-candidate
merge-authorization review before the P5 integration branch may move.

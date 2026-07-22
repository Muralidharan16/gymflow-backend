# Doers Cluster Role Policy v2

STATUS: DRAFT FRAMEWORK
APPROVED ARCHITECTURAL DIRECTIONS: D1–D8
OPERATIONAL VALUES: NOT YET APPROVED
CLUSTER STATE: NOT YET CORRECTED
BASELINE MANIFEST: NOT YET APPROVED

## Scope

This document records approved architectural directions. It does not approve
operational role names, credentials, limits, expiry values, cluster changes, or
the current PostgreSQL state. Cluster mutations require later phase-specific
authorization and expected-current-state guards.

## Approved Directions

### D1-B: Peer-admin bootstrap and reduced migration authority

Peer-admin owns cluster bootstrap and privileged role operations. A deployment
LOGIN will execute reviewed migrations through a reduced NOLOGIN ownership
capability. Migration authority must not retain permanent SUPERUSER, CREATEDB,
CREATEROLE, or BYPASSRLS.

### D2: Edge-by-edge least privilege

Memberships are approved individually. Blanket ADMIN, ambient inheritance, and
unexplained role nesting are prohibited. Ownership-transfer capabilities use
bounded SET access only when a migration proves it is required.

### D3-A: Separate production runtime and worker identities

API runtime and internal billing workers use separate LOGIN identities,
credentials, engines, pools, sessionmakers, health checks, and deployment
configuration. Production must fail closed instead of falling back between
runtime, worker, and migration credentials.

### D4-A: Remove pg_monitor from test_runner

The ordinary runtime test identity must not receive broad monitoring access.
Catalogue checks use peer-admin or a separately approved monitoring-test path.

### D5-A: Dedicated worker test identity

Worker behavior uses a dedicated test LOGIN and worker capability. Runtime,
worker, fixture-admin, catalogue, and negative-security tests remain distinct.

### D6-A: Function-mediated audit writes

Audit writes cross a hardened function boundary. Application roles do not
receive direct audit-table mutation privileges. Function ownership, fixed
search path, caller validation, sequence access, immutability, and read access
must be verified by a new forward migration and focused security tests.

### D7-B: Audited, expiring JIT support

Support capability remains NOLOGIN with no standing member. Access requires a
requester, approver, reason or ticket, environment, start, expiry, verified
revocation, audit evidence, and pre/post role manifests. Break glass requires
alerting and after-action review.

### D8-B: Deployment LOGIN plus NOLOGIN ownership capability

The migration deployment identity is a LOGIN; `migration_owner` becomes a
NOLOGIN, NOINHERIT ownership capability. Credential rollover and rollback must
never retain or replay raw password verifiers.

## Invariants

- Application and worker roles are NOBYPASSRLS and do not own tenant tables.
- Capability and owner roles are NOLOGIN unless a later approved policy says
  otherwise.
- Historical migrations remain immutable; object corrections use new forward
  migrations and cluster roles use peer-admin bootstrap.
- Every correction aborts when role, membership, password classification, D11,
  revision, residue, or repository state differs from its expected state.
- Passwords use SCRAM classification; no verifier value enters evidence.
- Current evidence is never promoted automatically to an approved baseline.

## Unresolved Operational Decision Register

The following are recommendations or missing deployment inputs, not approved
values:

| Decision | Status |
|---|---|
| Final production runtime LOGIN name | OWNER_DECISION_REQUIRED |
| Final production worker LOGIN name | OWNER_DECISION_REQUIRED |
| Final worker test LOGIN name | OWNER_DECISION_REQUIRED |
| Fixture-administrator identity and capability model | OWNER_DECISION_REQUIRED |
| Per-LOGIN connection limits and pool budgets | DEPLOYMENT_INPUT_REQUIRED |
| Credential rotation cadence and rollover mechanism | OWNER_DECISION_REQUIRED |
| Finite or externally enforced VALID UNTIL policy | OWNER_DECISION_REQUIRED |
| JIT support duration and enforcement mechanism | OWNER_DECISION_REQUIRED |
| Audit-reader population and access interface | OWNER_DECISION_REQUIRED |
| Local-development identity and fallback policy | OWNER_DECISION_REQUIRED |
| Deployment topology, process counts, and secret ownership | DEPLOYMENT_INPUT_REQUIRED |

Candidate names discussed during planning are deliberately excluded from the
managed-role target set until the owner approves them.

## Baseline Approval Boundary

An approved baseline requires corrected cluster state, dual deterministic
captures, structural and hash equality, intended/current equality, D11 and
persistent-revision stability, independent validation, secret scanning, and an
explicit owner approval record. None of those approval artifacts exists in
R19A.

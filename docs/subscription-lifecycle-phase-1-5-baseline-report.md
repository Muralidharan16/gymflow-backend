# Subscription Lifecycle Phase 1.5 Baseline Report

Date: 2026-06-15

## 1. Repository State

This phase inspected the dirty backend worktree before any subscription lifecycle Phase 2 implementation. No code, migrations, frontend files, git staging, commits, resets, or cleanup actions were performed.

Commands run:

```bash
pwd
git branch --show-current
git rev-parse HEAD
git status --short
git diff --stat
git diff --name-status
git diff -- app/models/member.py
git diff -- app/repositories/member_repo.py
git diff -- app/routers/members.py
git diff -- app/schemas/member.py
git diff -- app/services/member_service.py
git diff -- tests/test_member_subscriptions_v2.py
git diff -- tests/test_members.py
sed -n '1,260p' alembic/versions/b1c2d3e4f5a6_add_member_numbers.py
git diff -- docs/subscription-lifecycle-phase-0-audit.md
git diff -- docs/subscription-lifecycle-phase-1-domain-design.md
git log --oneline --decorate -15
git worktree list
git branch --all
```

Working directory:

```text
/home/jeevashri/gymflow-backend
```

`git status --short` before this report:

```text
 M app/models/member.py
 M app/repositories/member_repo.py
 M app/routers/members.py
 M app/schemas/member.py
 M app/services/member_service.py
 M tests/test_member_subscriptions_v2.py
 M tests/test_members.py
?? alembic/versions/b1c2d3e4f5a6_add_member_numbers.py
?? docs/subscription-lifecycle-phase-0-audit.md
?? docs/subscription-lifecycle-phase-1-domain-design.md
```

`git diff --stat` before this report:

```text
 app/models/member.py                  |   7 +-
 app/repositories/member_repo.py       |  95 +++++++++++--
 app/routers/members.py                |   8 +-
 app/schemas/member.py                 |   5 +
 app/services/member_service.py        |  34 ++++-
 tests/test_member_subscriptions_v2.py |   9 +-
 tests/test_members.py                 | 253 ++++++++++++++++++++++++++++++++++
 7 files changed, 387 insertions(+), 24 deletions(-)
```

`git diff --name-status` before this report:

```text
M	app/models/member.py
M	app/repositories/member_repo.py
M	app/routers/members.py
M	app/schemas/member.py
M	app/services/member_service.py
M	tests/test_member_subscriptions_v2.py
M	tests/test_members.py
```

Untracked files are not shown by `git diff --name-status`; they are listed in `git status --short`.

## 2. Current Branch And Commit

Current branch:

```text
basic-information
```

Current HEAD:

```text
649f626d15255cda43c596a3da4ec21f42c5c43c
```

Recent commits:

```text
649f626 (HEAD -> basic-information, origin/basic-information) docs: add database rollout notes
0860628 fix: clear operational state on branch deletion
7d3d5c3 fix: make fresh database migrations reliable
7e6dd42 test: isolate pytest from development database
f19d640 fix: enforce signup polling token for pending status
c5d853b fix: harden signup status security gate
9be1eee fix: align modern subscription route contract
73e6487 fix: harden auth signup verification flow
46c36e4 fix: refine member emergency contact validation
af781ba feat: add modern member subscription foundation
81c812b fix: harden modern member profile backend
ead5663 feat(members): modernize Member Profiles to use org_id and optional home_branch_id
472a583 test: expand membership plans reliability coverage
de05844 feat(membership_plans): P1.1 DB-atomic plan code sequence generation
53a82fc feat(membership_plans): implementation and tests for P1
```

Worktrees:

```text
/home/jeevashri/gymflow-backend  649f626 [basic-information]
```

Branches:

```text
* basic-information
  development
  main
  remotes/origin/basic-information
  remotes/origin/development
  remotes/origin/main
```

## 3. Dirty-File Classification

| File | Change purpose | Feature group | Required for current modern subscription baseline | Required for member-number functionality | Required for Phase 2 | Independent of Phase 2 | Test coverage | Migration dependency | Safe commit group | Risk |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `app/models/member.py` | Adds non-null `member_number`, org/member unique constraint, search indexes. | MN1 member-number implementation | No for existing v2 tables; useful for display and member selection. | Yes | Recommended baseline, not required for pure schema. | Partly | `tests/test_members.py`; subscription tests updated for non-null field. | `b1c2d3e4f5a6` | Commit A | Non-null column requires migration/backfill before model deploy. |
| `app/repositories/member_repo.py` | Adds atomic org-scoped member number allocation; adds member search by number; adds active-subscription projection and availability filter. | Mixed: MN1 + shared member/subscription code | No for v2 persistence; yes for admission/member availability UX. | Yes | Recommended if Phase 2 keeps current admission/member selection behavior. | No | `tests/test_members.py` includes search and active-subscription projection. | `organization_counters`; `member_subscriptions_v2` committed baseline. | Commit A, with note that it references committed subscription v2. | Mixed concern: member repo now imports subscription v2. |
| `app/routers/members.py` | Adds `branch_id` alias, search description, and `has_active_subscription` query param. | Shared member/subscription code | No for v2 storage; yes for frontend admission availability flow. | No direct | Recommended API baseline before Phase 2 frontend work. | No | `tests/test_members.py` branch/search/subscription availability tests. | None | Commit A | API addition is backward-compatible. |
| `app/schemas/member.py` | Adds `member_number`, display code, branch name, active subscription projection fields. | MN1 + shared member/subscription code | No for v2 persistence; useful for display. | Yes | Recommended for Phase 2 read models/member display. | No | `tests/test_members.py` response assertions. | `b1c2d3e4f5a6` for non-null field. | Commit A | Response contract expands; clients should tolerate added fields. |
| `app/services/member_service.py` | Allocates member numbers, builds display code from org slug, passes subscription filters. | MN1 member-number implementation | No for v2 persistence; useful for current UI. | Yes | Recommended baseline. | Partly | `tests/test_members.py` sequential/concurrent number tests. | `b1c2d3e4f5a6`; `organization_counters`. | Commit A | Needs model/migration together. |
| `tests/test_member_subscriptions_v2.py` | Adds `member_number` to test fixtures because model now requires it. | Tests / existing modern subscription-v2 implementation | Yes if MN1 model is committed; otherwise unnecessary. | Yes | Required if MN1 becomes baseline. | No | Focused tests pass. | `b1c2d3e4f5a6` | Commit A | Test-only fixture adjustment. |
| `tests/test_members.py` | Adds member-number tests, concurrency tests, search by number/branch, active-subscription projection tests. | Tests / MN1 + shared member/subscription code | Supports admission availability baseline. | Yes | Recommended baseline tests. | No | Focused tests pass. | `b1c2d3e4f5a6`; committed `fe1543f281fc`. | Commit A | Large test expansion; should be committed only with implementation. |
| `alembic/versions/b1c2d3e4f5a6_add_member_numbers.py` | Adds/backfills `members.member_number`, initializes member counter, adds indexes. | MN1 member-number implementation | No for v2 table existence; yes if member model changes are deployed. | Yes | Recommended before Phase 2 if member display/member selection uses member numbers. | Partly | Alembic graph/current verified on test DB. | Down revision `fe1543f281fc`. | Commit A | Downgrade deletes member counters; upgrade assumes `pg_trgm` exists. Existing earlier migration creates `pg_trgm`. |
| `docs/subscription-lifecycle-phase-0-audit.md` | Current-state subscription audit. | Phase 0 documentation | Documents committed v2 and dirty frontend/member assumptions. | No | Yes, as design reference. | No | Not executable. | None | Commit C | None. |
| `docs/subscription-lifecycle-phase-1-domain-design.md` | Parent/term lifecycle redesign. | Phase 1 documentation | Documents target model. | No | Yes, as design reference. | No | Not executable. | None | Commit C | None. |
| `docs/subscription-lifecycle-phase-1-5-baseline-report.md` | This baseline isolation report. | Phase 1.5 documentation | Documents baseline decision. | No | Yes, as safety gate. | No | Not executable. | None | Commit C | None. |

## 4. Feature-Group Analysis

### MN1 Member-Number Implementation

Files:

- `app/models/member.py`
- `app/repositories/member_repo.py`
- `app/schemas/member.py`
- `app/services/member_service.py`
- `tests/test_members.py`
- `tests/test_member_subscriptions_v2.py`
- `alembic/versions/b1c2d3e4f5a6_add_member_numbers.py`

This group is coherent. The model requires `member_number`, the service allocates it atomically through `organization_counters`, the migration backfills and enforces it, and tests cover sequence, org scoping, concurrency, immutability, search, and response fields.

### Existing Modern Subscription-v2 Implementation

The actual modern v2 foundation is already committed at `af781ba feat: add modern member subscription foundation`.

Committed files include:

- `app/models/member_subscription_v2.py`
- `app/repositories/member_subscription_v2_repo.py`
- `app/services/member_subscription_v2_service.py`
- `app/routers/member_subscriptions_v2.py`
- `app/schemas/member_subscription_v2.py`
- `app/utils/subscription_dates.py`
- `alembic/versions/fe1543f281fc_add_modern_member_subscriptions.py`
- `tests/test_member_subscriptions_v2.py`

The dirty worktree does not create `member_subscriptions_v2` or `subscription_members`; it only adapts member fixtures/projections around them.

### Shared Member/Subscription Code

`app/repositories/member_repo.py`, `app/routers/members.py`, and `app/schemas/member.py` include subscription-aware member listing:

- `has_active_subscription`
- `active_subscription_id`
- branch display projection
- member search by number/name/phone

This is not the future subscription-series architecture, but it is useful for the existing admission/subscription UI baseline.

### Documentation

Phase 0, Phase 1, and this Phase 1.5 document should be committed separately from code. They describe design and safety decisions only.

## 5. Alembic Graph Analysis

Commands run:

```bash
.venv/bin/alembic heads
.venv/bin/alembic history --verbose
env TEST_DATABASE_URL=postgresql+asyncpg://postgres:Murali%4007@localhost:5432/gymflow_test .venv/bin/alembic current
```

Results:

- `alembic heads`: `b1c2d3e4f5a6 (head)`
- `alembic current` against `gymflow_test`: `b1c2d3e4f5a6 (head)`
- `b1c2d3e4f5a6` parent: `fe1543f281fc`
- committed modern subscription migration: `fe1543f281fc`
- committed Alembic head at current `HEAD`, excluding untracked files: `fe1543f281fc`
- no second Alembic head was created by the untracked member-number migration
- no revision conflict was observed

`b1c2d3e4f5a6_add_member_numbers.py`:

- adds nullable `member_number`
- backfills per `org_id` using row number starting at 100
- seeds `organization_counters` with max member number per org
- alters `member_number` to non-null
- adds unique `(org_id, member_number)`
- adds branch/status, phone, lower-name, and trigram indexes
- downgrade drops indexes, constraint, column, and deletes member counters

Model/migration agreement:

- `Member.member_number` exists in model and migration.
- unique `(org_id, member_number)` exists in both model and migration.
- model includes `ix_members_org_branch_status` and `ix_members_org_phone`, matching migration.
- migration adds `ix_members_org_name_lower` and `ix_members_name_trgm`; these are database performance indexes not declared in ORM.

Risk notes:

- downgrade is data-destructive for member numbers and member counter rows.
- upgrade assumes `pg_trgm`; earlier migration `00f277c748ea...` creates `pg_trgm`, so this is currently covered by graph order.
- because the migration is untracked, any clean worktree from committed `HEAD` will not include the `member_number` column while dirty model code expects it.

## 6. Committed HEAD Versus Dirty-Worktree Comparison

| Capability | Committed HEAD | Dirty worktree | Assumed by Phase 0/1 | Required for Phase 2 |
| --- | --- | --- | --- | --- |
| `member_subscriptions_v2` | Yes | Yes | Yes | Yes as source/compatibility baseline |
| Subscription snapshots | Yes | Yes | Yes | Yes |
| `subscription_members` | Yes | Yes | Yes | Yes as source/compatibility baseline |
| Member numbers | No | Yes | Not core to lifecycle design; useful in current UI/read models | Recommended before UI/read-model Phase 2+; not required for pure new schema |
| Modern organisation scope | Yes | Yes | Yes | Yes |
| Modern subscription tests | Yes | Yes, adjusted for member numbers | Yes | Yes |

Can Phase 2 be built from committed `HEAD`?

- For pure backend lifecycle schema: technically yes, because committed `HEAD` already contains `member_subscriptions_v2`, `subscription_members`, snapshots, org scope, routes, and tests from `af781ba`.
- For the exact audited working baseline: no. Phase 0/1 were written while the dirty member-number and member availability projection changes existed, and the current frontend/admission UX benefits from those fields. Building from committed `HEAD` would omit `member_number`, member display codes, search by member number, and `has_active_subscription` filtering.

Required baseline before Phase 2:

1. Commit or otherwise isolate the MN1 member-number baseline if Phase 2 will build on current member display/admission behavior.
2. Commit Phase 0, Phase 1, and Phase 1.5 docs separately.
3. Create a clean dedicated worktree from that verified baseline.

## 7. Phase 0/1 Assumption Validation

Validated assumptions:

- `member_subscriptions_v2` exists in committed code.
- `subscription_members` exists in committed code.
- plan price/duration/capacity snapshots exist in committed code.
- current API `/organizations/{org_id}/member-subscriptions` exists in committed code.
- legacy gym-scoped subscriptions/payments are separate from modern v2.
- modern v2 lacks series, terms, renewal lineage, freeze history, and events.

Partially dirty-worktree-dependent assumptions:

- member-number display and search are dirty-only.
- `has_active_subscription` member availability filtering is dirty-only.
- tests that create members now require `member_number` because the dirty model makes it non-null.

Conclusion:

Phase 0/1 subscription architecture is valid against committed `HEAD`, but Phase 2 should not start from current `HEAD` if the product baseline includes member numbers and admission availability filtering.

## 8. Test Commands And Results

Focused tests:

```bash
env TEST_DATABASE_URL=postgresql+asyncpg://postgres:Murali%4007@localhost:5432/gymflow_test PYTHONPATH=. .venv/bin/pytest -q tests/test_member_subscriptions_v2.py tests/test_members.py
```

Result:

```text
45 passed, 2 warnings in 67.13s
```

Full suite:

```bash
env TEST_DATABASE_URL=postgresql+asyncpg://postgres:Murali%4007@localhost:5432/gymflow_test PYTHONPATH=. .venv/bin/pytest -q
```

Result:

```text
1 failed, 172 passed, 197 warnings, 3 errors in 184.75s
```

Repository tooling discovery:

- `pytest.ini` exists and configures `testpaths = tests`, `asyncio_mode = auto`.
- no `pyproject.toml` was present.
- no `Makefile` was present.
- no `.github/workflows` directory was present.
- `.venv/bin` contains `pytest`.
- `.venv/bin` did not contain `ruff`, `black`, `flake8`, `mypy`, `pyright`, or `isort`.
- `pyrightconfig.json` exists, but no local `pyright` binary was found.

No formatter, linter, or type-checker command was run because no configured/local tool was found.

## 9. Failure Classification

Focused member/subscription tests:

- passed.
- no failure to classify.

Full suite failure:

- `tests/test_rbac_phases_15_to_19.py::test_audit_key_registry_bootstrap`
- expected key alias: `alias/gymflow-audit-v1`
- actual key alias: `local/audit-signing-key-v1`
- classification: pre-existing repository/test-data expectation mismatch, not caused by the dirty member-number or subscription baseline changes.

Full suite errors:

- `tests/test_staff_roles.py::test_organization_user_flow`
- `tests/test_staff_roles.py::test_branch_staff_role_assignment`
- `tests/test_staff_roles.py::test_access_control_via_dependency`

Observed causes:

- Redis async client event loop error during fixture cleanup.
- missing relation `public.branch_audit_log_y2026_m05` during staff-role fixture grants.

Classification:

- environment/test-database setup issues outside the member/subscription focused baseline.
- not caused by Phase 1.5 documentation.
- not evidence that the dirty member-number baseline is broken, because the focused tests passed.

## 10. Safe Commit Groups

### Commit A: Member-number baseline

Recommended files:

```bash
git add \
  app/models/member.py \
  app/repositories/member_repo.py \
  app/routers/members.py \
  app/schemas/member.py \
  app/services/member_service.py \
  tests/test_members.py \
  tests/test_member_subscriptions_v2.py \
  alembic/versions/b1c2d3e4f5a6_add_member_numbers.py
```

Suggested message:

```text
feat(members): add organisation-scoped member numbers
```

Notes:

- This commit includes member-number implementation plus member availability projection used by admission/subscription UI.
- `tests/test_member_subscriptions_v2.py` belongs here because the dirty `Member` model requires `member_number` in fixtures.
- The commit should mention that the member repository now reads modern subscription v2 for availability projection.

### Commit B: Modern subscription-v2 baseline

No separate dirty Commit B is currently needed. The modern flat subscription-v2 foundation is already committed in:

```text
af781ba feat: add modern member subscription foundation
```

If desired, do not create an empty or duplicate commit.

### Commit C: Subscription lifecycle design documents

Recommended files:

```bash
git add \
  docs/subscription-lifecycle-phase-0-audit.md \
  docs/subscription-lifecycle-phase-1-domain-design.md \
  docs/subscription-lifecycle-phase-1-5-baseline-report.md
```

Suggested message:

```text
docs(subscriptions): define lifecycle redesign and migration strategy
```

Commit C should be separate from Commit A.

## 11. Recommended Isolation Strategy

Recommended strategy: Strategy A — Commit the required baseline, then create a clean worktree.

Reason:

- modern subscription-v2 baseline is already committed.
- dirty member-number baseline is coherent and focused tests pass.
- Phase 0/1 docs depend on the modern v2 architecture and should travel with the verified branch.
- starting Phase 2 directly in the current dirty worktree would mix schema redesign with member-number and documentation changes.

Do not begin Phase 2 until Commit A and Commit C are reviewed and committed, or until the user explicitly chooses a different isolation path.

## 12. Exact Commands For Recommended Strategy

Review before commit:

```bash
cd /home/jeevashri/gymflow-backend
git status --short
git diff --stat
git diff --check
```

Commit A:

```bash
git add \
  app/models/member.py \
  app/repositories/member_repo.py \
  app/routers/members.py \
  app/schemas/member.py \
  app/services/member_service.py \
  tests/test_members.py \
  tests/test_member_subscriptions_v2.py \
  alembic/versions/b1c2d3e4f5a6_add_member_numbers.py

git diff --cached --stat
git diff --cached --check
git commit -m "feat(members): add organisation-scoped member numbers"
```

Commit C:

```bash
git add \
  docs/subscription-lifecycle-phase-0-audit.md \
  docs/subscription-lifecycle-phase-1-domain-design.md \
  docs/subscription-lifecycle-phase-1-5-baseline-report.md

git diff --cached --stat
git diff --cached --check
git commit -m "docs(subscriptions): define lifecycle redesign and migration strategy"
```

Verify:

```bash
git status
git log --oneline -5
```

Create clean worktree for Phase 2:

```bash
git worktree add \
  -b feature/subscription-lifecycle-phase-2 \
  ../gymflow-subscription-lifecycle \
  HEAD
```

Then run Phase 2 only inside:

```bash
cd ../gymflow-subscription-lifecycle
```

## 13. Risks

- Starting Phase 2 from committed `HEAD` would omit member-number fields and availability projections present during audit.
- Starting Phase 2 from the current dirty worktree would mix unrelated baseline changes with lifecycle schema work.
- The untracked member-number migration is the current Alembic head locally; forgetting to commit it would leave models ahead of migrations.
- Full-suite failures exist outside the focused member/subscription tests and should be tracked separately.
- The member-number migration downgrade is data-destructive for member numbers and member counters.
- Current modern v2 table uses cascade deletes in places that Phase 1 correctly flagged as unsuitable for final financial-grade retention.

## 14. Whether Phase 2 Is Safe To Begin

Phase 2 is not safe to begin from the current dirty worktree.

Phase 2 becomes safe after:

1. Commit A is reviewed and committed, or explicitly excluded from the baseline.
2. Commit C is reviewed and committed.
3. A clean dedicated worktree is created from the accepted baseline.
4. The focused member/subscription tests remain passing.
5. The known broader RBAC/staff-role full-suite failures are accepted as unrelated or fixed separately.

## 15. Preconditions Still Outstanding

- User authorization to commit Commit A and Commit C.
- Decision whether MN1 member numbers are mandatory baseline for subscription Phase 2.
- Clean worktree creation after commits.
- Optional separate investigation/fix for full-suite RBAC/staff-role failures.
- No frontend lifecycle work should begin until backend Phase 2 baseline is isolated.

## Completion Confirmation

- Existing work was not discarded.
- Every dirty file was examined and classified.
- Alembic graph was verified.
- Committed HEAD contains the modern subscription-v2 baseline.
- Dirty worktree contains additional MN1/member availability baseline.
- Focused member/subscription tests passed.
- Full suite has unrelated failures/errors documented above.
- Safe commit groups are proposed.
- Strategy A is recommended.
- No Phase 2 schema, model, route, or frontend implementation was started.
- No commit was created.

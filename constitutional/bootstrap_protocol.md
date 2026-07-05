# Constitutional Bootstrap Protocol v1

## Purpose

Defines how a brand-new environment transitions from **UNTRUSTED** to
**CONSTITUTIONALLY CERTIFIED**. Without this protocol, deterministic trust
starts ambiguously.

## Prerequisites

| Requirement | Version | Source |
|---|---|---|
| PostgreSQL | 16.x | `docker-compose.yml` |
| Python | 3.12+ | `Dockerfile` |
| Redis | 7+ | `docker-compose.yml` |
| requirements.txt installed | all packages | `requirements.txt` (30 packages) |

## Bootstrap Sequence

### Step 1: Dependency Verification

```bash
python -m geo_constitutional_enforcement.dependency_governance
```

**Expected:** `Dependency lockfile verified.`
**On first run:** `WARNING: No dependency lockfile found. Run with --freeze to create one.`

```bash
# First-time only:
python -m geo_constitutional_enforcement.dependency_governance --freeze
```

### Step 2: Schema Deployment

```bash
alembic upgrade head
```

Applies all 50 migrations from `alembic/versions/`.

### Step 3: Database Execution Contract Verification

Verify PostgreSQL configuration matches `database_execution_contract.yaml`:

```sql
SHOW lc_collate;          -- Must return 'C'
SHOW timezone;            -- Must return 'UTC'
SHOW server_version;      -- Must start with '16'
```

Apply session initialization:

```sql
SET timezone = 'UTC';
SET extra_float_digits = 0;
SET lc_messages = 'C';
```

### Step 4: Semantic Registry Validation

```bash
python -m geo_constitutional_enforcement.drift_detector
```

Compares `app/models/geo.py` against `constitutional/semantic_registry/*.yaml`.
**Must produce zero drift findings.**

### Step 5: Purity Scan

```bash
python -m geo_constitutional_enforcement.purity_scanner
```

**Must produce zero violations** on all canonical paths.

### Step 6: Runtime Governance Validation

```bash
python scripts/governance_validation.py
```

All 12 checks must pass:

1. `SECURITY_DEFINER_OWNERSHIP`
2. `PUBLIC_EXECUTE_REVOCATION`
3. `SEARCH_PATH_HARDENING`
4. `TABLE_OWNERSHIP_DRIFT`
5. `TIMESTAMP_INDEX_DETECTION`
6. `RLS_FORCE_ENFORCEMENT`
7. `CONSTRAINT_VALIDATION`
8. `SOFT_DELETE_RESURRECTION`
9. `AUDIT_APPEND_ONLY`
10. `PRIMARY_CONTACT_TRIGGERS`
11. `ADVISORY_LOCK_FUNCTION`
12. `INDEX_COVERAGE`

### Step 7: Replay Certification Initialization

```bash
python -m geo_constitutional_enforcement.replay_validator \
  --mode=bootstrap --corpus=tests/corpora/
```

1. Seeds corpus data into clean DB
2. Executes replay twice
3. Compares snapshot hashes — must match
4. Freezes baseline `manifests/*.sha256`

### Step 8: Spec Version Snapshot

```bash
python -m geo_constitutional_enforcement.complexity_auditor \
  --verify-spec-version
```

Verifies `constitutional/spec_versions/v1/` contains all current specs.

### Step 9: Enforcement Integrity Freeze

```bash
python -m geo_constitutional_enforcement.enforcement_integrity_checker --freeze
```

Records SHA256 hashes of all critical enforcement modules.

### Step 10: Certification

All steps passed → environment is **CONSTITUTIONALLY CERTIFIED**.

Record certification event:
- Timestamp
- Environment fingerprint (from `runtime_fingerprint.yaml`)
- All gate verdicts (Steps 1–9)
- Certifying engineer

## Bootstrap Failure Recovery

If any step fails, the environment is **NOT certified**.
Do NOT proceed with data import until all 10 steps pass.
Re-run from Step 1 after fixing the failure.
No partial certification is valid.

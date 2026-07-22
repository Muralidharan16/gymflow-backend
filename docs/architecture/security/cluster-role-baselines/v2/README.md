# Cluster Role Baseline v2 Framework

## Status

This directory is a secret-free, offline-validation framework. It is not an
approved cluster baseline and it does not authorize PostgreSQL execution.

The approved architectural directions are D1-D8. Operational role names,
connection limits, credential lifetime, JIT duration, audit-reader access,
local-development identity behavior, deployment topology, and secret
management remain unresolved.

## Why This Exists

A SHA-256 value alone cannot explain which role attribute, setting, comment, or
membership changed. Hash-only evidence is therefore forbidden. Every retained
hash must be paired with canonical, schema-valid, secret-free JSON that can be
compared structurally.

The R4-R17 current-evidence hash
`b3bd126d84deedbb19fde3342dd5180696e115d17369f0a8a1a308cd13fd749b`
is not an approved baseline. The historical `87f4...` hash is historical
evidence only and is not a restoration target.

Automatic re-baselining is prohibited. A difference must be explained,
corrected or explicitly approved through the later R19F contract.

## Package Layout

- `CLUSTER_ROLE_POLICY_V2.md`: draft D1-D8 policy and unresolved decisions.
- `managed_roles.json`: frozen R4-R17 relevant-role set.
- `manifest_schema_v2.json`: secret-free manifest JSON Schema.
- `generator.py`: deterministic state normalization and hashing.
- `capture_peer_admin.sh`: unapproved, read-only capture template.
- `validate_manifest.py`: fail-closed offline manifest validation.
- `compare_manifests.py`: safe structural comparison.
- `SHA256SUMS`: integrity hashes for the R19A framework files.

No approved manifest, approved hash, approval record, credential, live capture,
restoration SQL, or rollback SQL belongs in this R19A package.

## Canonical State

The generator hashes only the `state` object. It uses UTF-8 and:

```python
json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

Role, setting, membership, and relevant-role arrays are semantically sorted
before serialization. Capture timestamp, execution mode, source-database label,
and operator label are non-hashed metadata. Database-specific settings use
operator-supplied semantic database names, never OIDs or temporary names.

## Capture Lifecycle

1. Freeze the policy, schema, generator checksum, and managed-role set.
2. Validate the capture template offline.
3. Obtain separate owner authorization for a peer-admin capture.
4. Capture twice into an explicitly supplied mode-700 directory.
5. Validate each manifest and compare their canonical structures and hashes.
6. Compare intended policy with current state after corrections.
7. Perform independent validation, secret scanning, D11 checks, and revision
   checks.
8. Create approved artifacts only in R19F after explicit owner approval.

The template never writes approved artifacts or repository checksums.

## Validation

Offline commands from this directory:

```bash
python3 validate_manifest.py current-evidence.json
python3 compare_manifests.py first.json second.json --format human
sha256sum README.md CLUSTER_ROLE_POLICY_V2.md managed_roles.json \
  manifest_schema_v2.json generator.py capture_peer_admin.sh \
  validate_manifest.py compare_manifests.py > SHA256SUMS
sha256sum -c SHA256SUMS
```

Checksum generation is a reviewed framework-maintenance action. Neither the
capture template nor any validation tool runs it automatically.

The comparator returns zero only for comparable, identical state. Invalid or
non-comparable inputs and unexplained differences return nonzero.

## Secret Safety

Manifests may contain password classification only. They must not contain raw
verifiers, plaintext passwords, tokens, private keys, credentials, URLs,
filesystem paths, OIDs, or environment secret values. Capture metadata uses
semantic labels. Temporary catalogue material is private and is deleted unless
the operator explicitly selects a secure output directory.

## Phase Sequence

R19A provides this framework only. R19B prepares guarded correction procedures;
R19C performs separately authorized role expansion and contraction; R19D
separates application and worker identities; R19E corrects the audit boundary;
R19F captures and approves the corrected baseline; R19G corrects Platform
Billing privileges; R19H performs fresh isolated verification.

Current evidence and an approved baseline are different lifecycle states. This
framework must never infer approval from a matching hash.

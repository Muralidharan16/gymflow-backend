# Decision 0002: Replay Stability Requirements
**Date:** 2026-06-03
**Status:** Accepted
**Authors:** Architecture Team

## Context
Replay equivalence is the constitutional mechanism that guarantees the same
input corpus always produces the same output. Without canonical serialization,
replay hashing can produce false mismatches due to JSON key ordering, float
formatting, timezone strings, and Unicode normalization.

## Decision

### 1. Canonical Serialization Contract
All replay evidence must be serialized through `canonical_serialization.yaml`:
- UTF-8 with NFC normalization
- Sorted JSON keys, compact separators
- Decimal values as fixed-point strings with 6-digit precision
- ISO8601 datetimes with UTC offset (+00:00)
- JSON null for missing values

### 2. Replay Corpus Requirements
Per `corpus_governance.yaml`, the corpus must contain:
- Minimum 5 countries, 20 subdivisions, 50 cities, 200 postal codes
- Minimum 40 replay operations
- 5 mandatory edge case categories (Unicode, timezone conflicts, etc.)
- Append-only mutation policy

### 3. Database Execution Constraints
Per `database_execution_contract.yaml`, replay environments must use:
- `lc_collate = C` (binary collation)
- `timezone = UTC`
- `max_parallel_workers_per_gather = 0` (deterministic planner)

## Consequences
- Replay harness (Phase 3) must integrate canonical serializer
- Corpus changes trigger full replay recertification
- DB execution contract is verified during bootstrap Step 3

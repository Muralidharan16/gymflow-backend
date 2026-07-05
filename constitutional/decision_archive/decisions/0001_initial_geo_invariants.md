# Decision 0001: Initial Geo Invariants
**Date:** 2026-06-03
**Status:** Accepted
**Authors:** Architecture Team

## Context
The gymflow-backend geo infrastructure (`app/models/geo.py`) defines a 4-table
canonical hierarchy: `countries → subdivisions → cities → postal_codes`. All
tables share a `geo_record_status` ENUM with unidirectional state transitions.

This decision establishes the foundational invariants that the constitutional
enforcement layer will protect.

## Decision

### 1. Deterministic Purity
Canonical geo data paths (`geo_service.py`, `geo_repository.py`, `geo.py`)
must be free of nondeterministic primitives (random, wall-clock, mutable globals).

### 2. Nondeterminism Isolation
Infrastructure modules (`concurrency.py`, `crypto.py`, `db_retry.py`,
`geocoding.py`) are authorized nondeterminism boundaries. Canonical paths
may call these modules but may not contain nondeterministic primitives directly.

### 3. Approved Exemptions
- `geo_service.py`: `float()` for Pydantic serialization of `Numeric(9,6)` (L55-56)
- `topology.py`: `asyncio` and `asyncio.sleep` for migration orchestration (L17, L95)

### 4. Schema Immutability (Frozen Tables)
After canonical data promotion, `countries`, `subdivisions`, `cities`, and
`postal_codes` tables enter frozen epoch. DROP COLUMN, ALTER COLUMN TYPE,
DROP TABLE, and RENAME COLUMN are prohibited.

### 5. Status State Machine
`geo_record_status` transitions are unidirectional:
`pending_validation → active → deprecated → historical`.
No reversal permitted. `historical` is terminal.

## Consequences
- Purity scanner enforces these rules at CI time
- Exemptions are capped at 5 per file
- Boundary modules are capped at 6

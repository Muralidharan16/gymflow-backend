# Decision 0003: Canonical Serialization Adoption
**Date:** 2026-06-03
**Status:** Accepted
**Authors:** Architecture Team

## Context
Replay hashing can produce false mismatches when serialization is not
deterministic. The `geo_service.py` timezone cascade (L43) and `Numeric(9,6)`
to `float` conversion (L55-56) are specific locations where serialization
nondeterminism can arise.

## Decision
Adopt `canonical_serialization.yaml` as the mandatory serialization contract
for all replay evidence. Key rules:

1. **Decimals:** Fixed-point string with 6 decimal places (`"1.500000"`)
2. **JSON:** Sorted keys, compact separators, no indent
3. **Unicode:** NFC normalization form
4. **Temporal:** ISO8601 with UTC offset
5. **Arrays:** Preserve SQL ORDER BY ordering
6. **Nulls:** JSON `null`, never empty string

## Rationale
Without this contract, two bit-identical replay runs can hash differently
because Python `json.dumps` does not guarantee key ordering by default,
and `float(Decimal("1.5"))` can serialize as `1.5` or `1.500000` depending
on formatting.

## Consequences
- `canonical_serializer.py` (Phase 2) implements this contract
- All replay snapshot outputs pass through `canonical_hash()` before comparison
- Changes to serialization rules require an epoch upgrade

# Decision 0005: Service Layer Constitutional Perimeter Expansion

**Date:** 2026-06-03
**Status:** Accepted
**Authors:** Architecture Team

## Context

The constitutional purity scanner was initially scoped to the geo canonical paths:
`geo_service.py`, `geo_repository.py`, `geo.py`, `topology.py`. These were
the first paths audited and confirmed clean.

Three service files contained confirmed constitutional violations that were
remediated as part of Phase 0:

- `app/services/invoice_service.py:234` — `datetime.now()` without timezone
  in a canonical PDF invoice timestamp write (PV-003)
- `app/services/onboarding_service.py:85` — `datetime.now(IST)` in canonical
  trial start/end/grace/hard_lock timestamp writes (PV-004)
- `app/services/trial_service.py:44` — `datetime.now(IST)` in trial status
  comparison logic (PV-005)

All three violations were remediated. The source of truth is now
`datetime.now(timezone.utc)` in all canonical writes.

## Decision

1. Add all three service files to `canonical_paths` in
   `constitutional/deterministic_purity_rules.yaml`. The scanner will now
   guard these files against future nondeterministic regressions.

2. Add exemptions for `strftime` (invoice formatting) and `astimezone`
   (IST display conversion). These are deterministic operations on already-UTC
   datetimes and do not represent nondeterministic sources.

3. The `pincode_service.py` file (which called the India Post external API)
   is deleted. The onboarding pincode lookup is now served by `GeoService`
   using the self-hosted `postal_codes` table. This is documented as PV-006
   remediated.

## Consequences

- The purity scanner now guards 7 canonical paths (was 4).
- Any future `datetime.now()` without `timezone.utc` in any of the 7 paths
  will fail the constitutional pipeline immediately.
- The geo router (`/v1/geo/lookup`) is registered in `main.py` and reachable.
- `pincode_service.py` is permanently deleted. Any re-introduction of external
  postal API calls requires a constitutional amendment.

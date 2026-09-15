# P8-R — Operational runbooks

P8-R binds every P8-A critical alert to one repository-controlled operational runbook. It does not authorize release, deployment, live money movement or refund-provider activation.

## Parent boundary

P8-R starts from certified P8-A head `b19714b6c0a0d2b50ca862e35bf096d822014b71`, tree `104621750b126c302f890adbf27c13460df5f3a4`.

## Coverage contract

The machine-readable contract is `docs/architecture/p8_runbook_contract.json`. Its failure-mode keys must exactly equal the frozen P8 governance critical failure modes and the P8-A alert contract. Its paths must exactly equal the runbook links emitted by P8-A alert rules.

Every runbook must contain:

- alert and owning operational team;
- customer impact;
- incident evidence that must be preserved;
- ordered diagnosis;
- safe first actions;
- explicit forbidden actions;
- escalation condition;
- recovery verification.

Runbooks live under `docs/runbooks/p8/` and are incident-response instructions, not a second business-control API.

## Safety invariants

PostgreSQL remains the durable business authority. Redis/Celery/Beat remain coordination surfaces. Observability evidence never authorizes business mutation. Operators must preserve evidence before repair and use only already-certified replay, reconciliation, compensation, drain/restart or infrastructure paths.

For external effects, especially Finance, an ambiguous provider result is not failure and is not success. The runbooks forbid blind retry, replacement monetary effects and local terminal-success edits without authoritative downstream evidence. Idempotency, leases, fences, late acknowledgements, scheduler ownership, tenant/RLS boundaries, TLS and runtime identities must not be weakened to clear an alert.

Backup success remains infrastructure-owned. Application telemetry may report a verified backup signal but may not fabricate one.

## Recovery standard

An incident is not closed just because an alert stops firing. Recovery verification must re-check authoritative state, confirm backlog/error-rate recovery, prove no duplicate business effect was created, and retain the incident/operator evidence.

## P8-R decision markers

The exact-head P8-R gate emits only:

- `P8_RUNBOOK_COVERAGE=PASS`
- `P8_ALERT_RUNBOOK_BINDING=PASS`
- `P8_SAFE_FIRST_RESPONSE=PASS`
- `P8_RECOVERY_VERIFICATION=PASS`
- `P8_OBSERVABILITY_NON_AUTHORITATIVE=PASS`
- `P8_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`

P8-R does **not** emit `P8_PRODUCTION_LIKE_OBSERVABILITY=PASS`, `P8_EVERY_CRITICAL_FAILURE_VISIBLE_ACTIONABLE=PASS`, or the final P8-F same-head marker. Those require later slices.

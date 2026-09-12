# P4E Acceptance Matrix

Candidate lineage: `hardening/p4e-operational-recovery-observability` with
Alembic head `zf07d8e9f0a40`.

P4E completes cross-domain recovery visibility and final external-effect
contract certification for the currently authorized boundaries. Certification
requires both P4E workflows to pass on one immutable commit.

| Gate | Required result |
|---|---|
| Inventory truth | Search and notification certified; Finance refund-obligation boundary certified; provider refund execution explicitly deferred and fail-closed |
| Attempt versus success | A local attempt, submission or enqueue never proves downstream success |
| Search | Provider evidence, external version fencing, reconciliation and drift repair remain certified |
| Notification | Provider acceptance remains non-terminal; delivery, webhook, replay and reconciliation remain evidence-bound |
| Refund obligation | Lifecycle input is resolved through authoritative Finance state with idempotency, leases, fencing and sanitized recovery discovery |
| Refund provider execution | No live provider refund call or money movement is enabled or claimed by P4E |
| Aggregate observability | Search and Refund snapshots expose numeric aggregates only, with no caller arguments or identifiers |
| Metric export | Real OTLP protobuf export uses only the fixed `domain` and `state` attribute keys |
| Process isolation | Snapshot reads and P4E metric configuration belong only to the maintenance process and database capability |
| Recovery runbook | Alerts never authorize manual terminal-success state or replacement refund creation |
| Migration lifecycle | `zf07` upgrades, downgrades and re-upgrades without changing predecessor table/column ACLs |

## Same-head hard stop

P4E is certified only when these checks succeed on the same commit:

- `.github/workflows/p4e-operational-snapshots-pg16.yml` emits
  `P4E_SLICE1B_PG16_CERTIFICATION=PASS`;
- `.github/workflows/p4e-final-external-effects-certification.yml` emits
  `P4E_FINAL_EXTERNAL_EFFECT_CERTIFICATION=PASS`.

The subsequent full P4 same-head regression across inherited P1/P2/P3 and all
P4 runtime suites is a separate final-P4 gate. P4E does not claim that gate,
merge readiness, release readiness or deployment readiness.

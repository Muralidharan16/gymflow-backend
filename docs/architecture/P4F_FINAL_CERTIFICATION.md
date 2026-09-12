# P4F / Final P4 Same-Head Certification

Final P4 is a certification-only phase stacked on the certified P4E commit
`373199c418e7451b20f05e14e17010aa5167d672` (tree
`fa097b644c5622259370319d147fbc52b8eb7f50`). Its candidate branch is
`hardening/p4f-final-certification`, and its required Alembic head is
`zf07d8e9f0a40`.

No production application behavior, provider adapter, RLS policy or runtime
authority is added by this phase. In addition to the final certification
topology, matrix, contract tests and reusable workflow entry points, Final P4
repairs a P4D downgrade defect exposed by the fan-in: removing P4D's temporary
table-wide outbox read now restores exactly the narrower grants owned by its
P4C/P4B/lifecycle predecessor revisions.

## Governing decision rule

Certification belongs to one immutable Git commit. Every required workflow is
called from `.github/workflows/p4f-final-same-head-certification.yml` at that
same `github.sha`. A changed candidate invalidates every prior decisive result.
The terminal marker is emitted only after all required called workflows and the
frozen-contract job report `success`:

`P4F_FINAL_SAME_HEAD_CERTIFICATION=PASS`

Skipped, cancelled, neutral, timed-out or failed prerequisites are not success.
The terminal job uses `always()` only so it can turn every non-success result
into an explicit failure rather than disappearing as a skipped job.

## Required evidence groups

| Group | Decisive evidence |
|---|---|
| P1/P2 architecture and identity | Architecture hardening, canonical cluster roles, runtime non-escalation and inherited P3 certification |
| Migration safety | Current graph/semantics, fresh lineage, full downgrade/re-upgrade, populated preservation and adversarial predecessor recovery |
| General product regression | Broad application regression plus isolated Platform Billing and Finance Core suites |
| P4B search | Real local OpenSearch, provider evidence, external-version fencing, reconciliation and drift repair on PostgreSQL 16 |
| P4C notifications | Provider acceptance versus delivery truth, webhook evidence, crash ambiguity, DLQ replay and broad regression on PostgreSQL 16 |
| P4D refund obligation | Authoritative Finance resolution, idempotency, concurrency, fencing and downgrade refusal on PostgreSQL 16 |
| P4E operations | Aggregate maintenance-only snapshots, OTLP export isolation, operator safety and reversible migration lifecycle |

The exact reusable workflow list and job topology are frozen in
`docs/architecture/p4f_certification_matrix.json`.

## Hard stops and limitations

- Refund-provider execution remains explicitly `deferred_fail_closed`.
- No live refund API call, provider refund mutation or money movement is run.
- Local OpenSearch is the only real external-service runtime used; notification
  and Finance provider boundaries remain deterministic test transports.
- Telemetry, queue labels and operator input never become business authority.
- Final P4 does not perform or authorize a merge, retarget, tag, release or
  deployment.
- A passing terminal marker permits only a separate merge-authorization review
  of the exact candidate SHA and tree.

The final PASS/FAIL decision is the GitHub Actions result for the immutable
candidate, not a claim embedded into this source document.

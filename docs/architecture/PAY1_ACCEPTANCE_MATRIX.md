# PAY-1 Acceptance Matrix

| Area | Required proof | Terminal marker |
| --- | --- | --- |
| Baseline | Exact certified PAY-0 parent/tree and unchanged Alembic head | `PAY1_PAY0_INHERITED=PASS` |
| Canonical model | Explicit independent aggregates/statuses for acquisition, attempts, invoices, allocations, settlement, refund, disputes and adjustments | `PAY1_CANONICAL_FINANCIAL_MODEL=PASS` |
| State separation | Captured payment is not rewritten by settlement/refund/dispute/chargeback; overdue is derived | `PAY1_STATE_MACHINE_SEPARATION=PASS` |
| Money | Decimal business money, integer provider minor units, no float, explicit currency exponent | `PAY1_MONEY_CONTRACT=PASS` |
| Compatibility | Current overloaded Finance/Platform states have explicit safe target mappings | `PAY1_COMPATIBILITY_MAPPING=PASS` |
| Migration baseline | PAY-1 adds no migration and all inherited migration suites remain green | `PAY1_MIGRATION_BASELINE=PASS` |
| Provider/refund authority | Live money stays disabled and refund-provider execution stays fail-closed | terminal safety markers |
| Final | All PAY-1 and inherited gates pass on one exact SHA/tree | `PAY1_FINAL=PASS` |

## Hard gate

Any automatic backward-state acceptance based only on enum/status ordering is a
PAY-1 failure. Provider staleness requires authoritative evidence.

Any design that mutates a captured payment to encode settlement, refund,
dispute or chargeback state is a PAY-1 failure.

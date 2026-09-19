# PAY-0 Acceptance Matrix

| Area | Required proof | Terminal marker |
| --- | --- | --- |
| Exact baseline | PAY-0 descends from certified P10 SHA/tree with no rewrite | `PAY0_P10_INHERITED=PASS` |
| Architecture inventory | All existing member-commerce, Finance Core and Platform Billing money surfaces are classified | `PAY0_ARCHITECTURE_INVENTORY=PASS` |
| Migration baseline | Single Alembic head remains `zk07d8e9f0a45`; PAY-0 adds no migration | `PAY0_MIGRATION_BASELINE=PASS` |
| Authority | PostgreSQL/Finance authority and provider-evidence boundary are frozen | `PAY0_ARCHITECTURE_INVENTORY=PASS` |
| Live money | Finance and Platform Billing live money paths remain disabled | `PAY0_LIVE_MONEY_MOVEMENT=DISABLED` |
| Refund boundary | Provider refund execution remains fail-closed | `PAY0_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED` |
| Inherited regression | Architecture, General/Platform Billing, Finance and migration gates pass on the PAY-0 SHA | `PAY0_FINAL=PASS` |

## Hard gate

PAY-0 cannot pass with an application-code change, an Alembic revision change,
an ACL/RLS/security change, or any live-provider/money-movement enablement.

A PAY-0 pass is an inventory/governance certificate only.

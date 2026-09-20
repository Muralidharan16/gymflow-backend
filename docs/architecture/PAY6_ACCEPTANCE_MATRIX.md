# PAY-6 Acceptance Matrix

| Area | Required proof | Marker |
| --- | --- | --- |
| PAY-5 inheritance | Exact certified predecessor SHA/tree and exact PAY-6 changed-path scope | \`PAY6_PAY5_INHERITED=PASS\` |
| Evidence | Method + structured reference + SHA-256 proof + exact amount/currency are mandatory | \`PAY6_PROOF_REFERENCE_REQUIRED=PASS\` |
| Maker/checker | Same actor cannot prepare and approve/reject; terminal replay remains actor-bound | \`PAY6_MAKER_CHECKER=PASS\` |
| Idempotency | Prepare/approve/reject persist in PAY-3 monetary-command authority | \`PAY6_IDEMPOTENT_RECORDING=PASS\` |
| Full payment | Manual payment → allocation → ledger → invoice paid → \`finance.invoice.paid\` | \`PAY6_INVOICE_RECONCILIATION=PASS\` |
| Partial payment | Invoice remains partially paid and no paid event exists | \`PAY6_INVOICE_RECONCILIATION=PASS\` |
| Stale request | Approval revalidates current outstanding and cannot overpay after another payment wins | \`PAY6_INVOICE_RECONCILIATION=PASS\` |
| Concurrent approval | Two checkers race; exactly one payment/allocation effect is created | \`PAY6_IDEMPOTENT_RECORDING=PASS\` |
| Rejection | Terminal, immutable, reason-coded, zero payment effect | \`PAY6_IMMUTABLE_AUDIT=PASS\` |
| Tenant isolation | Cross-tenant invoice/request use fails closed | \`PAY6_DIRECT_DML_DENIAL=PASS\` |
| Runtime authority | app_runtime cannot directly read/write PAY-6 or Finance money tables | \`PAY6_DIRECT_DML_DENIAL=PASS\` |
| Migration | Empty ACL/relfilenode roundtrip; populated downgrade fail-closed | \`PAY6_MIGRATION_LIFECYCLE=PASS\` |
| Safety | No provider call, live payment initiation, refund execution, release or deploy | terminal safety markers |

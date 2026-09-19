# PAY-4 Acceptance Matrix

| Area | Required proof | Marker |
| --- | --- | --- |
| PAY-3 inheritance | Exact certified predecessor SHA/tree and exact PAY-4 scope | `PAY4_PAY3_INHERITED=PASS` |
| Admission | V2 compatibility row is pending; canonical term is pending_payment | `PAY4_PENDING_PAYMENT_ADMISSION=PASS` |
| Canonical binding | Immutable term/invoice/context/member/plan/amount/currency binding | `PAY4_MEMBER_FINANCE_BINDING=PASS` |
| Missing/pending/failed payment | No activation | `PAY4_FINANCE_EVENT_ACTIVATION=PASS` |
| Amount/currency mismatch | No activation | `PAY4_FINANCE_EVENT_ACTIVATION=PASS` |
| Fully applied payment | Exactly one active/scheduled transition and Finance lifecycle event | `PAY4_EXACTLY_ONCE_ACTIVATION=PASS` |
| Authority | Direct API/runtime/provider-style activation is denied by DB guard | `PAY4_ACTIVATION_BYPASS_DENIAL=PASS` |
| Tenant isolation | Cross-tenant term, binding and event use fail closed | `PAY4_ACTIVATION_BYPASS_DENIAL=PASS` |
| Migration | Empty roundtrip exact; populated downgrade fails closed; predecessor ACL preserved | `PAY4_MIGRATION_LIFECYCLE=PASS` |
| Safety | No live provider credentials/capture/refund/release/deploy | terminal safety markers |

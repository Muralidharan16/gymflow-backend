# PAY-3 Acceptance Matrix

| Area | Required proof | Marker |
| --- | --- | --- |
| Exact baseline | Exact certified PAY-2 parent/tree | `PAY3_PAY2_INHERITED=PASS` |
| Command identity | tenant/scope/key/hash/business-ref/correlation/actor-hash are durable | `PAY3_MONETARY_COMMAND_PROTOCOL=PASS` |
| Replay | same key+payload replays; different payload conflicts | `PAY3_IDEMPOTENT_REPLAY=PASS` |
| Ambiguity | unknown cannot silently create replacement effect | `PAY3_UNKNOWN_OUTCOME_FENCING=PASS` |
| DB authority | runtime cannot directly read/write journal | `PAY3_DIRECT_DML_DENIAL=PASS` |
| Retention | no TTL; populated downgrade fails closed | `PAY3_DURABLE_EVIDENCE=PASS` |
| Migration | empty lifecycle passes; existing Finance tables are not rewritten | `PAY3_MIGRATION_LIFECYCLE=PASS` |
| Safety | live money/refund-provider execution remain disabled | terminal safety markers |

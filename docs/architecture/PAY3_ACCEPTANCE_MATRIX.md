# PAY-3 Acceptance Matrix

| Area | Required proof | Marker |
| --- | --- | --- |
| Exact baseline | Exact certified PAY-2 parent/tree and exact PAY-3 changed-path allowlist | \`PAY3_PAY2_INHERITED=PASS\` |
| Command identity | tenant/scope/key/hash/business-ref/correlation/originating-actor are durable | \`PAY3_MONETARY_COMMAND_PROTOCOL=PASS\` |
| Replay | same key+payload+actor replays; different payload/actor conflicts | \`PAY3_IDEMPOTENT_REPLAY=PASS\` |
| Ambiguity | unknown blocks replacement effect; ambiguity reason/time survive terminal reconciliation | \`PAY3_UNKNOWN_OUTCOME_FENCING=PASS\` |
| Concurrency/crash | one concurrent insert; pre-commit crash rolls back; post-commit crash replays | \`PAY3_CONCURRENCY_CRASH=PASS\` |
| DB authority | runtime cannot directly read/write journal | \`PAY3_DIRECT_DML_DENIAL=PASS\` |
| Retention | no TTL; repair/full populated downgrade fails closed without evidence loss | \`PAY3_DURABLE_EVIDENCE=PASS\` |
| Migration | immutable pushed predecessor; additive repair; empty lifecycle passes; existing Finance tables not rewritten | \`PAY3_MIGRATION_LIFECYCLE=PASS\` |
| Safety | live money/refund-provider execution remain disabled | terminal safety markers |

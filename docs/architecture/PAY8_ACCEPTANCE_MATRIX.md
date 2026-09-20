# PAY-8 Acceptance Matrix

| Area | Required proof | Marker |
| --- | --- | --- |
| PAY-7 inheritance | Exact PAY-7 SHA/tree is ancestor; PAY-8 is 0 behind | \`PAY8_PAY7_INHERITED=PASS\` |
| Outbound durability | Provider operation is durable before provider call | \`PAY8_DURABLE_PROVIDER_OPERATION=PASS\` |
| Tenant integrity | Provider operation has composite payment/org FK and tenant-checked reservation | \`PAY8_DURABLE_PROVIDER_OPERATION=PASS\` |
| Idempotency | Same logical operation replays; changed request conflicts | \`PAY8_DURABLE_PROVIDER_OPERATION=PASS\` |
| Retry safety | Only \`failed_retryable\` may be claimed again; new attempt gets a new fence | \`PAY8_UNKNOWN_OUTCOME_FENCING=PASS\` |
| Crash after POST | Expired \`in_flight\` becomes \`unknown\`, not retryable | \`PAY8_UNKNOWN_OUTCOME_FENCING=PASS\` |
| Unknown resolution | Only reconciliation runtime may resolve unknown to success/final failure with evidence hash | \`PAY8_PROVIDER_RECONCILIATION=PASS\` |
| Provider object uniqueness | Same provider object cannot bind to two Finance payments | \`PAY8_UNKNOWN_OUTCOME_FENCING=PASS\` |
| Signature first | Webhook signature and normalized evidence are verified before inbox persistence | \`PAY8_WEBHOOK_INBOX=PASS\` |
| Durable inbox | Verified normalized inbox commits before Finance mutation | \`PAY8_WEBHOOK_INBOX=PASS\` |
| Replay conflict | Same event id + same evidence replays; changed evidence conflicts | \`PAY8_WEBHOOK_REPLAY_FENCING=PASS\` |
| Crash/reclaim | Expired processing webhook can be reclaimed by finance-payment runtime | \`PAY8_WEBHOOK_CRASH_RECOVERY=PASS\` |
| Stale worker | Old lease owner/fence cannot complete/fail a reclaimed webhook | \`PAY8_WEBHOOK_CRASH_RECOVERY=PASS\` |
| Dead letter | Retry exhaustion terminates instead of looping forever | \`PAY8_WEBHOOK_CRASH_RECOVERY=PASS\` |
| Existing Finance truth | Webhook uses certified provider-evidence state machine rather than parallel money logic | \`PAY8_WEBHOOK_INBOX=PASS\` |
| Direct DML | app/payment/reconciliation/worker runtimes have no direct PAY-8 table DML | \`PAY8_DIRECT_DML_DENIAL=PASS\` |
| Migration upgrade | No predecessor relation rewrite | \`PAY8_MIGRATION_LIFECYCLE=PASS\` |
| Migration downgrade | Populated downgrade fails closed; empty ACL/relfilenode roundtrip and re-upgrade pass | \`PAY8_MIGRATION_LIFECYCLE=PASS\` |
| Inherited system | Finance/general/Platform Billing/architecture/migration gates all green on exact head | \`PAY8_PAY7_INHERITED=PASS\` |
| Activation safety | Live provider, live money, refunds, merge/release/deploy remain unauthorized | terminal safety markers |

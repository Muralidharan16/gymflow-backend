# PAY-7 Acceptance Matrix

| Area | Required proof | Marker |
| --- | --- | --- |
| PAY-6 inheritance | Exact predecessor SHA/tree; branch is 0 behind; PAY-6 remains ancestor | \`PAY7_PAY6_INHERITED=PASS\` |
| Provider-neutral contract | Checkout/member business services import no Razorpay concrete adapter type | \`PAY7_PROVIDER_NEUTRAL_CONTRACT=PASS\` |
| Registry | Only registered sandbox/test adapters resolve; duplicate/live/unknown providers fail closed | \`PAY7_SERVER_OWNED_REGISTRY=PASS\` |
| Retry safety | Only pre-submit connection failure is retryable | \`PAY7_FAILURE_TAXONOMY=PASS\` |
| Unknown outcome | Timeout/network/5xx/ambiguous/malformed success requires reconciliation and is not auto-retryable | \`PAY7_UNKNOWN_OUTCOME_RECONCILIATION=PASS\` |
| Deterministic failure | Ordinary deterministic 4xx/local safety violations are final | \`PAY7_FAILURE_TAXONOMY=PASS\` |
| Secret isolation | Provider errors/config repr/browser payload expose no key secret, webhook secret, auth header or raw error payload | \`PAY7_SECRET_ISOLATION=PASS\` |
| Finance authority | Adapter imports no SQLAlchemy/session/repository/Finance DML surface | \`PAY7_NO_FINANCE_DML=PASS\` |
| Browser authority | Browser cannot select provider or author amount/status/entitlement truth | \`PAY7_PROVIDER_NEUTRAL_CONTRACT=PASS\` |
| Migration | No new Alembic revision; head remains \`zq07d8e9f0a51\` | \`PAY7_NO_MIGRATION_CHANGE=PASS\` |
| Real PG16 inheritance | Finance/general/Platform Billing + lifecycle/preservation/adversarial safety green on exact head | \`PAY7_PAY6_INHERITED=PASS\` |
| Activation safety | Live provider, live money, refund-provider execution, deploy/release remain disabled | terminal safety markers |

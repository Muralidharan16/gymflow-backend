# PAY-7 Provider Adapter Boundary

**Certified PAY-6 predecessor:** \`e3eecc04ff65284e9d96bece3c2b304673ee6663\`  
**PAY-6 tree:** \`90b614b1e914772f454f4bd4394121203903e145\`  
**Alembic head inherited unchanged:** \`zq07d8e9f0a51\`

PAY-7 defines the provider adapter boundary. It deliberately does **not** add a
provider-operation table, webhook inbox, reconciliation worker, refund call, or
live credential path. Those durable checkout/webhook concerns begin in PAY-8.

## Authority boundary

Finance Core owns invoices, payment intents, payment truth, allocations, ledger
effects and entitlement-driving events.

A provider adapter owns only translation and transport:

1. translate a server-authored checkout request into provider format;
2. perform the configured sandbox/test provider call;
3. validate the provider response against server-authored amount/currency/reference;
4. return normalized provider identifiers and minimal browser-safe fields;
5. normalize transport/provider failures into provider-neutral safety classes.

The adapter cannot read or mutate Finance persistence. It imports no SQLAlchemy
session/repository/model surface.

## Provider-neutral contract

Business orchestration depends on \`CheckoutIntentProvider\`, not Razorpay.

The contract exposes:

- \`provider_code\`;
- \`environment\` limited to \`sandbox | test\`;
- \`create_checkout_intent(request)\`;
- \`build_checkout_fields(provider_order_ref)\`.

A server-owned \`CheckoutProviderRegistry\` accepts only sandbox/test adapters,
rejects duplicate/invalid provider codes and resolves only explicitly registered
providers. Provider selection is not accepted from browser/customer request
payloads.

Razorpay remains the only configured PAY-7 adapter, but it is composed at the
HTTP boundary and passed inward through the generic interface.

## Failure taxonomy

Every outbound provider-operation error has a normalized class:

| Class | Meaning | Automatic retry |
|---|---|---|
| \`retryable\` | failure occurred before request submission; provider cannot have accepted it | permitted by later durable orchestration |
| \`final\` | deterministic local/provider rejection | no |
| \`unknown\` | request may have reached provider or successful response cannot be trusted | **never blindly retry**; reconciliation required |

PAY-7 is deliberately conservative for POST order creation:

- connection establishment failure before submit → \`retryable\`;
- timeout/network error after submit → \`unknown\`;
- HTTP 408/409/425/429/5xx → \`unknown\`;
- malformed/mismatched successful response → \`unknown\`;
- deterministic ordinary 4xx rejection → \`final\`;
- unsafe local URL/timeout/metadata validation → \`final\`.

PAY-8 must persist and reconcile \`unknown\` outcomes before issuing another
provider-side create.

## Secret/config safety

Razorpay configuration remains test/sandbox-only:

- key id must be \`rzp_test_...\`;
- live-mode material is rejected;
- secrets are redacted from repr/errors;
- transport host/base URL is fixed to the approved Razorpay endpoint;
- no third-party provider SDK is introduced;
- raw provider response/error payloads are not surfaced.

Browser output contains only provider public key id + provider order id. Browser
fields never include authoritative amount, tax, payment status, secret material,
or entitlement instructions.

## Existing compatibility

The historical \`razorpay_adapter=\` constructor/method keyword remains accepted
as a compatibility alias for inherited tests/callers, but business services
store and use it only as the generic \`_provider_adapter\`.

This avoids a risky big-bang migration while preventing new provider-specific
business coupling.

## Database / migration posture

PAY-7 adds **no Alembic revision and no database object**.

The certified PAY-6 head \`zq07d8e9f0a51\` must remain the sole Alembic head.
PAY-7 certification therefore proves:

- no changed path under \`alembic/versions/\`;
- fresh PostgreSQL lineage still reaches \`zq07d8e9f0a51\`;
- inherited migration lifecycle/preservation/adversarial contracts all remain green;
- Finance/general/Platform Billing real PG16 regressions remain green.

## Safety posture

PAY-7 does not authorize:

- merge/release/deploy;
- live provider mode;
- live money movement;
- customer-facing production checkout;
- refund-provider execution;
- automatic retry of unknown provider outcomes.

## Terminal markers

\`\`\`text
PAY7_PROVIDER_NEUTRAL_CONTRACT=PASS
PAY7_SERVER_OWNED_REGISTRY=PASS
PAY7_FAILURE_TAXONOMY=PASS
PAY7_UNKNOWN_OUTCOME_RECONCILIATION=PASS
PAY7_SECRET_ISOLATION=PASS
PAY7_NO_FINANCE_DML=PASS
PAY7_NO_MIGRATION_CHANGE=PASS
PAY7_PAY6_INHERITED=PASS
PAY7_LIVE_PROVIDER=DISABLED
PAY7_LIVE_MONEY_MOVEMENT=DISABLED
PAY7_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED
PAY7_FINAL=PASS
\`\`\`

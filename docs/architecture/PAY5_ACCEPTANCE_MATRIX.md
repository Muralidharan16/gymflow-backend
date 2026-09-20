# PAY-5 Acceptance Matrix

| Gate | Required result |
| --- | --- |
| Certified predecessor | Exact PAY-4 SHA/tree and Alembic predecessor are bound |
| Claim race | Two workers racing one ready Finance event produce one live owner |
| Worker death before consumer commit | No entitlement mutation and no consumption record; event remains reclaimable |
| Worker death after consumer commit / before Finance ack | Replay performs no duplicate business effect and completes acknowledgement |
| Duplicate delivery | Published event is not claimable; duplicate live consumers converge on one effect |
| Stale lease / ABA | Old worker or old fence cannot consume, acknowledge or release |
| Product ownership | Product consumption journal has unique Finance-event and idempotency keys |
| Finance authority | Consumer reuses PAY-4 Finance-gated activation; raw queue payload is never entitlement authority |
| Runtime authority | worker_runtime has bounded function EXECUTE only, no direct event/consumption table DML |
| Retry | Retryable release returns pending; next normal claim increments attempt/fence |
| Terminal failure | Permanent/exhausted delivery becomes failed and is not silently reclaimed |
| Migration | Empty predecessor→PAY5→predecessor→PAY5 roundtrip restores predecessor ACL/relations |
| Populated downgrade | Fails closed if PAY-5 delivery or consumption evidence exists |
| Inherited | Architecture, general, Finance, Platform Billing and migration gates all green |
| Safety | Live money disabled; refund-provider execution deferred fail-closed |

Terminal gate: \`PAY5_FINANCE_EVENT_DELIVERY=PASS\`.

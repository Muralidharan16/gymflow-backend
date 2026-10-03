# PAY-24-C — Entitlement / Subscription Activation Boundary

PAY-24-C separates financial truth from member business effects.

```text
Finance/provider fact
        ↓
ordinary worker durable enqueue
        ↓
member_entitlement_commands
        ↓
entitlement_runtime
        ↓
app_secure PAY-24-C state machine
        ↓
canonical subscription state
```

The ordinary worker no longer receives an activation exception. PAY-4's
certified financial revalidation remains the paid-activation implementation,
but PAY-24-C changes its execution authority to `entitlement_runtime`.

Refund completion is also only a durable fact. The worker converts
`finance.refund.completed` into a replay-safe recomputation command. The
entitlement runtime determines whether the remaining net applied financial
support still covers the bound invoice before any access state changes.

Interactive legacy subscription writes are retired. Member attendance/access
must resolve from canonical lifecycle terms, assignments, dates and freezes,
not `public.member_subscriptions`.

Stage 0 remains closed: the production identity overlay contains no entitlement
worker process and no `ENTITLEMENT_DATABASE_URL` credential.

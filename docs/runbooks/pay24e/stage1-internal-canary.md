# PAY-24-E runbook — Stage-1 internal canary

This runbook prepares and later operates Stage 1. The repository state by itself
does not authorize activation.

## Before human authorization

1. Deploy the exact certified PAY-24-E application SHA with the normal API,
   worker and maintenance processes unchanged.
2. Load the PAY-24-D/PAY-24-E critical alerts.
3. Provision the isolated entitlement database login/secret and verify-full TLS.
4. Start the canary entitlement worker and PAY-24-E scheduler with explicit
   scale overrides while PAY-24-A is still Stage 0. Their database effect must
   remain zero because the PAY-24-E claim returns no work in Stage 0.
5. Run the read-only preflight with `PAY24E_EXPECTED_STAGE=0`.
6. Confirm provider calls and real money movement are still zero.

## Activation transaction

Only after explicit human authorization:

1. bind the trusted measured deployed SHA to the certified SHA through PAY-24-A;
2. bind the human authorization for Stage 1 and that exact SHA;
3. set exactly one internal organization;
4. transition to Stage 1 with provider egress `open`;
5. enable exactly checkout, webhooks, payment_application and
   subscription_activation;
6. keep refund execution, recurring billing, dunning and platform billing off;
7. run the read-only preflight with `PAY24E_EXPECTED_STAGE=1`;
8. perform one bounded internal-organization transaction and observe every
   durable Finance/provider/entitlement record before continuing.

Do not use an environment variable or scheduler process as activation authority.
PAY-24-A PostgreSQL state is authoritative.

## Immediate rollback

At any anomaly:

1. begin PAY-24-A emergency rollback; egress becomes `closing`;
2. PAY-24-E automatically denies new entitlement claims and mutation rechecks;
3. scale the canary scheduler and entitlement worker to zero;
4. preserve all commands, queues and provider admissions;
5. classify/expire provider admissions and reconcile unknown outcomes;
6. finalize rollback only when admitted/active provider leases are zero;
7. verify Stage 0, blocked egress, zero switches, worker/scheduler scaled to zero.

Never delete entitlement commands, fabricate provider success, retry an unknown
provider outcome as failure, or widen the internal-organization cohort.

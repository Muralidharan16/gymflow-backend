# PAY-24-D runbook — Stage-1 entitlement worker and emergency rollback

## Preconditions

Do not start Stage 1 unless the exact certified application SHA is deployed,
PAY-24-A release identity and human authorization are bound to that SHA, the
internal organization is explicit, provider egress is still blocked until the
transition transaction commits, and this PAY-24-D readiness gate is green.

## Alert diagnosis

For a missing worker heartbeat, old pending command, failed/review-required
command, or expired processing lease:

1. preserve the activation-authority snapshot and entitlement aggregate snapshot;
2. verify the deployed SHA and runtime database principal;
3. check Redis/Celery delivery only after checking PostgreSQL durable commands;
4. inspect lease fences and command status without editing command rows;
5. if financial truth is ambiguous, keep the command in review-required and
   reconcile; never infer success.

## Safe rollback

1. Call the durable PAY-24-A begin-emergency-rollback capability.
2. Confirm provider egress is `closing`; no new provider admissions are allowed.
3. Remove Stage-1 scheduling and scale the entitlement worker to zero.
4. Allow an in-flight DB transaction to finish or roll back; never kill it after
   an unknown external provider commit point without classifying the provider
   admission.
5. Preserve the `member_entitlement_commands` journal and queues. Do not purge.
6. Expire/reconcile provider admissions until admitted=0 and active=0.
7. Finalize PAY-24-A rollback; verify Stage 0, egress blocked and all switches off.
8. Verify no duplicate activation, refund, provider call or payment application.

## Forbidden actions

Never grant the entitlement worker payment/refund/API/AWS/Razorpay credentials.
Never give it direct DML/SELECT on the entitlement command journal. Never clear
an alert by deleting durable commands, forcing command status, disabling RLS,
changing lease fences, or retrying an unknown provider outcome as if it failed.

## Recovery verification

The worker may be restored only after exact-SHA/runtime-principal/TLS checks pass,
failed/review-required commands are understood, expired leases are safely
reclaimable, and the PAY-24-A posture matches the authorized stage.

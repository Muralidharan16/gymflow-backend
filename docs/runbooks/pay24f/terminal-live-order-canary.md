# PAY-24-F terminal canary — live order only

PAY-24-F intentionally separates provider connectivity from real-money movement.

## What this phase can do

- bind the exact certified/deployed SHA using trusted deployment measurement;
- bind explicit Stage-1 human authorization;
- open PAY-24-A Stage 1 for exactly one internal organization;
- prepare a durable member-subscription Finance checkout locally;
- create exactly one live Razorpay order after PAY-24-B admission;
- close provider admission immediately through emergency rollback.

A Razorpay order is not a payment capture. PAY-24-F1 does not introduce a live
webhook receiver, capture endpoint, refund execution, recurring billing, or any
other real-money mutation.

## Required release measurement

Prefer a deployment-generated root-owned JSON attestation with schema_version 1,
the exact deployed_sha, measured_by, and a timezone-aware measured_at value.
The file must be a regular non-symlink owned by root and not group/other
writable. The operator chooses only its path. The SHA cannot be supplied as a
terminal argument.

For a deployment whose running application is literally the same clean Git
checkout, --release-git-root may be used instead.

## Operator sequence

1. deploy the certified PAY-24-F application SHA while PAY-24-A remains Stage 0;
2. verify snapshot;
3. run prepare-live-order and inspect its mode-0600 evidence file;
4. run activate-stage1 interactively for the exact internal organization;
5. re-run snapshot;
6. run execute-live-order interactively;
7. verify the returned live Razorpay order ID and Finance intent;
8. do not attempt payment/capture under PAY-24-F1;
9. run rollback-stage0 unless the controlled next gate is immediately being
   executed.

Any unknown provider outcome is a hard stop: do not retry the live order.
Reconcile the same provider operation/idempotency identity first.

## Secrets

Live key ID/secret are read only from terminal environment variables during
execute-live-order. They are not accepted as command-line arguments and are not
persisted in evidence.

## Status after PAY-24-F1

PAY24F_LIVE_RAZORPAY_ORDER=PREPARED
PAY24F_LIVE_WEBHOOK=NOT_IMPLEMENTED
PAY24F_LIVE_CAPTURE=NOT_IMPLEMENTED
PAY24F_REAL_MONEY_MOVEMENT=0

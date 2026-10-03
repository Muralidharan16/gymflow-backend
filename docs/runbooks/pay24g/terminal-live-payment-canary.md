# PAY-24-G — terminal real-money internal canary

PAY-24-G is the first phase capable of moving real money, but source code and
CI do not authorize or execute it.

The live canary is intentionally restricted to:
- one PAY-24-A internal organization;
- INR only;
- a hard maximum of INR 10.00;
- one pre-existing PAY-24-F live Razorpay order;
- an interactive terminal command;
- a one-time Razorpay Checkout page bound only to 127.0.0.1;
- finance_reconciliation_runtime for live provider evidence;
- the existing app_runtime Finance payment-application capability;
- the PAY-24-E entitlement boundary.

The terminal command performs no Capture API request. Razorpay Checkout must
return a successful callback, the callback signature must verify with the live
key secret, and a backend GET of the payment must independently report
status=captured and captured=true before Finance can record captured state.

If Checkout ends without a verified successful callback, the PAY-24 admission
is closed as unknown and the payment must not be retried until provider
reconciliation.

Before the real canary, start the PAY-24-E canary entitlement worker/scheduler
with explicit non-zero scale overrides while Stage 1 is active. After Finance
application, the controller waits for the subscription projection to become
active. A timeout is a canary failure requiring investigation, not a reason to
repeat payment.

After the canary is fully observed, execute the certified PAY-24-F emergency
rollback command to return to Stage 0.

PAY24G_REAL_MONEY_EXECUTION=TERMINAL_ONLY
PAY24G_PUBLIC_LIVE_PAYMENT_ROUTE=ABSENT
PAY24G_MAX_CANARY_AMOUNT_INR=10.00
PAY24G_CAPTURE_API=ABSENT
PAY24G_LIVE_PROVIDER_NETWORK_CI=0
PAY24G_REAL_MONEY_MOVEMENT_CI=0
PAY24_STAGE1_LIVE_ACTIVATION=NOT_AUTHORIZED_BY_SOURCE

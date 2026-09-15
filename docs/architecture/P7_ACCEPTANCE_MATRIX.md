# P7 Acceptance Matrix

P7 is accepted only when every required row is proved on the final immutable candidate.

| ID | Scenario | Required evidence | Failure condition |
|---|---|---|---|
| P7-S1 | Unauthorized preStop | Missing/invalid system credential returns non-success and drain state is unchanged | Any public/tenant caller can enter DRAINING |
| P7-S2 | Authorized preStop | Dedicated orchestration credential enters DRAINING without exposing secret | Credential bypass, logging, or tenant-JWT dependency |
| P7-S3 | Probe split | Liveness 200 while draining; readiness 503 immediately after drain begins | Same dependency-sensitive semantics used for both probes |
| P7-D1 | New request admission | Ordinary request arriving after DRAINING is rejected before business side effects | New business work reaches route/service/DB mutation |
| P7-D2 | In-flight completion | Request admitted before drain completes within grace budget | Admitted request is cancelled solely because drain begins |
| P7-D3 | Self-count exclusion | preStop/probes are excluded from business in-flight accounting | Drain waits on its own system request |
| P7-D4 | Repeated drain | Repeated authorized preStop is idempotent and bounded | Repeated calls extend/duplicate drain indefinitely |
| P7-R1 | Redis cleanup | API Redis client closes during lifespan shutdown | Redis connection remains process-owned after shutdown |
| P7-R2 | DB cleanup | API async engine and optional API sync engine are disposed | API pool remains open after graceful exit |
| P7-R3 | Supervisor cleanup | API-local supervised tasks terminate cleanly and shutdown is idempotent | Orphan process-local task survives or cleanup hangs |
| P7-W1 | Worker termination inheritance | Real worker SIGTERM/redelivery/fencing evidence remains PASS under P7 candidate | P7 weakens P6 worker recoverability |
| P7-O1 | Production-like rollout | Real API process: slow admitted request + authorized drain + readiness false + liveness true + new request rejected + clean SIGTERM exit | Any required sequencing invariant fails |
| P7-O2 | Dependency disturbance | Liveness does not restart the process merely because readiness dependency checks fail | Dependency outage causes false liveness failure |
| P7-F1 | Final same-head | All P7 and inherited P1-P6 critical gates pass on one exact SHA/tree | Mixed-head evidence or any prerequisite non-success |

## Terminal markers

The final P7 decision may emit PASS only after all final-candidate prerequisites succeed:

- `P7_PRESTOP_AUTHORIZATION=PASS`
- `P7_LIVENESS_READINESS_SEPARATED=PASS`
- `P7_READINESS_FALSE_BEFORE_SHUTDOWN=PASS`
- `P7_NEW_REQUEST_DRAIN=PASS`
- `P7_INFLIGHT_COMPLETION=PASS`
- `P7_API_RESOURCE_SHUTDOWN=PASS`
- `P7_WORKER_TERMINATION_RECOVERABLE=PASS`
- `P7_PRODUCTION_LIKE_ROLLOUT=PASS`
- `P7_NO_LOST_DURABLE_BUSINESS_WORK=PASS`
- `P7_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`
- `P7F_FINAL_SAME_HEAD_CERTIFICATION=PASS`

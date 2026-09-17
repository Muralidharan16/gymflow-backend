# P10 Acceptance Matrix

P10 is not accepted slice-by-slice by narrative claim. Each row below requires machine-readable evidence on an exact candidate SHA.

| Slice | Required proof | Terminal marker |
| --- | --- | --- |
| P10-G | Baseline, authority, hard stops, slices and evidence contract frozen | `P10_GOVERNANCE=PASS` |
| P10-B | Production-shaped synthetic baseline completed and numeric budgets frozen before dependent slices | `P10_BASELINE_BUDGETS=PASS` |
| P10-L | Representative multi-tenant load plus concurrency stress within frozen budgets with inherited correctness intact | `P10_REPRESENTATIVE_LOAD=PASS` and `P10_CONCURRENCY_STRESS=PASS` |
| P10-Q | Queue throughput/backlog drain measured; worker replacement recovers with no lost/duplicate durable effect | `P10_QUEUE_BACKLOG_RECOVERY=PASS` |
| P10-D | Critical plans/indexes reviewed; N+1/query-count and bounded-pagination contracts proven | `P10_QUERY_PERFORMANCE=PASS` |
| P10-S | Long soak remains inside frozen latency/resource/backlog growth budgets | `P10_SOAK=PASS` |
| P10-X | Dependency/image/static/secret scans, SBOM, and fail-closed config audit pass | `P10_SECURITY_SCANS=PASS` and `P10_SECRETS_CONFIG_FAIL_CLOSED=PASS` |
| P10-H | Hardened production container behavior and runtime restrictions proven | `P10_CONTAINER_HARDENING=PASS` |
| P10-F | All inherited P1-P9 gates plus every P10 gate pass on one immutable SHA/tree | `P10_FINAL_PRODUCTION_CERTIFICATION=PASS` |

## Final evidence requirements

The final P10-F evidence must bind:
- exact candidate SHA and tree;
- merged P9 base SHA/tree;
- Alembic head;
- frozen performance-budget artifact digest;
- load/concurrency measurements;
- queue throughput/backlog recovery measurements;
- query-plan/index/N+1/pagination evidence;
- soak duration and resource time series;
- SBOM and security-scan artifact digests;
- production image digest and runtime-hardening evidence;
- every inherited P1-P9 gate result;
- every P10 slice result.

The final decision must also emit:
- `P10_P1_P9_INHERITED=PASS`
- `P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED`
- `P10_FINAL_PRODUCTION_CERTIFICATION=PASS`

No P10 certification authorizes a tag, release, production deployment, live provider, live refund execution or live money movement.

# P4A Acceptance Matrix

Base: certified P3E `882406537584861da2b2b6d44fd37b016a9f8462`

P4A is an architecture and safety-contract phase. It does not enable real providers. Its purpose is to make P4B/P4C/P4D implementation semantics explicit and mechanically guarded before provider code is admitted.

| Gate | Required result |
|---|---|
| External effect inventory | Exact current lifecycle external event set inventoried |
| Fail-closed current handlers | Search, notification and refund lifecycle commands cannot be marked delivered without real provider handlers |
| Legacy notifications | Historical global reminder/birthday/digest entry points remain fail-closed |
| False-success semantics | Only definite downstream success can justify terminal success |
| Provider acceptance | Explicitly non-terminal unless the certified provider contract defines the acknowledgement itself as terminal evidence |
| Ambiguous outcomes | Non-terminal and routed to retry/reconciliation using the same logical idempotency identity |
| Idempotency | Deterministic logical identity required across retry/redelivery/reconciliation |
| Lease/fencing | Stale workers cannot commit terminal success after lease loss/supersession |
| Reconciliation | Cannot advance success/sync markers from local SQL success alone |
| Dead-letter | Durable operational state with controlled audited recovery |
| Webhook/callback | Authenticated, replay-safe, idempotent and mapped to pre-existing internal/provider references |
| Operator replay | Reconstructs requests from authoritative DB state; arbitrary provider authority is forbidden |
| Security inheritance | No RLS weakening, BYPASSRLS, broad worker grants or migration/admin worker fallback |
| P3E bootstrap guard | Canonical HEAD workflow bootstrap scanner remains green |

## Hard stop

P4A may be certified only when all P4A workflows are green on one immutable SHA and PR #11 remains Draft/Open/Unmerged. Any code change after certification invalidates the P4A same-head result.

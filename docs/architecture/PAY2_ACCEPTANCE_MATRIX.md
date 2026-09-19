# PAY-2 Acceptance Matrix

| Area | Required proof |
| --- | --- |
| Cluster identity | Six dedicated Finance roles exist as exact NOLOGIN/NOINHERIT/NOBYPASSRLS peers |
| Existing-cluster expansion | Predecessor graph verified before additive role creation; full graph verified after |
| Alembic boundary | Migration creates/alters/drops no PostgreSQL role |
| Direct DML | Runtime/capability roles have no raw Finance relation authority |
| Ownership | Finance schema/tables/functions are included in canonical ownership governance |
| Immutable evidence | Audit/provider/tax/ledger-line/credit-note-line evidence rejects runtime UPDATE/DELETE |
| Posted ledger | Posted ledger entries reject runtime UPDATE/DELETE |
| Migration | `zk07d8e9f0a45 -> zl07d8e9f0a46 -> zk07d8e9f0a45 -> zl07d8e9f0a46` preserves populated evidence |
| Regression | PAY-1, Finance Core, Platform Billing, architecture and inherited migration gates stay green |
| Money safety | Live money remains disabled and provider refund execution remains fail-closed |

| Lock/rewrite | Migration uses transaction-local 3s lock timeout/30s statement timeout and real PG16 relfilenode equality proof | `PAY2_ZERO_TABLE_REWRITE=PASS` |

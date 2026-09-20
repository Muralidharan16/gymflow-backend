# PAY-9 Acceptance Matrix

PAY-9 base: `ad0292e48bccc8b5e99c1082d6b0f7933f42789c`  
PAY-9 predecessor: `zr07d8e9f0a52`  
PAY-9 head: `zs07d8e9f0a53`

| Requirement | Proof | Gate |
| --- | --- | --- |
| Provider capture is not entitlement | PAY-9 application produces Finance allocation/outbox only; PAY-5/PAY-4 activates later | mandatory |
| Only verified captured/settled payment applies | DB capability requires captured/order.paid evidence and captured/settled Finance state | mandatory |
| Caller cannot nominate invoice or amount | capability signature is exactly `(payment_id,payment_event_id)` | mandatory |
| Full payment | invoice becomes paid; one ledger effect; paid outbox emitted | mandatory |
| Partial/underpayment | invoice remains partially_paid; no paid outbox; entitlement remains pending | mandatory |
| Multiple payments / split tender | prior valid allocation + provider allocation converges to exact invoice total | mandatory |
| Overpayment | allocation capped at invoice outstanding; excess remains unapplied | mandatory |
| Unapplied payment | missing/mismatched binding records fail-closed outcome without allocation | mandatory |
| Wrong invoice protection | invoice derives from checkout binding and must match PAY-4 immutable Finance binding | mandatory |
| Duplicate/replayed provider evidence | one allocation/ledger effect; event replay records no new effect | mandatory |
| Bank settlement independence | captured (not settled) payment can qualify once invoice and binding gates pass | mandatory |
| Entitlement | only existing PAY-5 dispatcher + PAY-4 consumer may activate/schedule | mandatory |
| Tenant/security | cross-tenant context rejected; runtime direct PAY-9 table access denied | mandatory |
| Migration lifecycle | empty downgrade restores PAY-8; populated downgrade refuses | mandatory |
| Inherited regressions | architecture/general/Finance/migration suites all green on exact candidate | mandatory |
| Live-money boundary | live provider, live money, refunds, deployment remain unauthorized | mandatory |

Terminal gate: `PAY9_SETTLEMENT_AND_ENTITLEMENT=PASS`.

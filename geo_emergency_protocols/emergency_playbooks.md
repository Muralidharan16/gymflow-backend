# Emergency Playbooks

## SEV1: Data Corruption

### Immediate Actions
1. **Assess scope:** Determine which tables and how many rows are affected
2. **Activate override:** Get 2-principal authorization (on-call + team lead)
3. **Snapshot current state:** `pg_dump` affected tables before any repair
4. **Apply fix:** Direct SQL UPDATE/INSERT as needed
5. **Verify:** Run `scripts/governance_validation.py` — all 12 checks must pass

### Post-Incident (within 48 hours)
1. Run full replay validation against corpus
2. Run semantic drift detector
3. Create decision_archive entry documenting root cause
4. Review whether constitutional rules prevented or missed the issue

---

## SEV2: Service Degradation

### Immediate Actions
1. **Identify root cause:** Redis failure? DB connection pool exhaustion?
2. **Activate override:** Get 1-principal authorization
3. **Apply temporary mitigation:** Cache bypass, increased connection limits, etc.
4. **Monitor:** Watch `app/observability/branch_contacts_queries.py` metrics

### Post-Incident (within 24 hours)
1. Verify all geo lookups return correct results
2. Confirm cache consistency after Redis recovery
3. Review whether nondeterminism boundary expansion is needed permanently

---

## SEV3: Governance Bypass

### Immediate Actions
1. **Document justification:** Why is the CI gate blocking? False positive or real?
2. **Activate override:** 1 principal + written justification in override log
3. **Deploy with gate skipped**
4. **Create follow-up ticket:** Fix the root cause (false positive or actual violation)

### Post-Incident (within 24 hours)
1. Fix the underlying issue that triggered the gate
2. Re-run all constitutional gates — must all pass
3. If rule change needed, create constitutional amendment

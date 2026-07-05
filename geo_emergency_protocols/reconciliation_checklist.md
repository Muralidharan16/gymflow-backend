# Post-Incident Reconciliation Checklist

Use this checklist after any emergency override to restore constitutional trust.

## Required Checks (All Must Pass)

- [ ] **Replay validation passes** — `python -m geo_constitutional_enforcement.replay_validator`
- [ ] **Semantic drift check passes** — `python -m geo_constitutional_enforcement.drift_detector`
- [ ] **Purity scan passes** — `python -m geo_constitutional_enforcement.purity_scanner`
- [ ] **Dependency lockfile verified** — `python -m geo_constitutional_enforcement.dependency_governance`
- [ ] **Runtime governance passes** — `python scripts/governance_validation.py` (all 12 checks)
- [ ] **Enforcement integrity verified** — `python -m geo_constitutional_enforcement.enforcement_integrity_checker`
- [ ] **Override has expired** — Confirm max_duration_hours has elapsed or override explicitly closed
- [ ] **Decision archive entry created** — Document root cause, actions taken, and lessons learned
- [ ] **Budget updated** — Record override against quarterly budget in governance_budgets.yaml

## If Permanent Change Needed

- [ ] Create constitutional amendment in `decision_archive/decisions/`
- [ ] Update affected specs (purity rules, boundaries, schema rules, etc.)
- [ ] Snapshot new specs to `spec_versions/vN+1/`
- [ ] Run full replay recertification
- [ ] Get dual review approval

## Sign-Off

| Field | Value |
|---|---|
| Incident ID | |
| Severity | SEV1 / SEV2 / SEV3 |
| Override activated by | |
| Override authorized by | |
| Override activated at | |
| Override expired at | |
| All checks passed at | |
| Reconciled by | |

# Human Governance Model v1

## Purpose

Technical governance assumes reviewers behave correctly, amendments are honest,
and overrides are legitimate. Real institutional systems fail socially before
they fail technically. This document defines the human governance layer.

## Separation of Duties

| Role | Modify Code | Modify Constitutional Specs | Approve Amendments | Authorize Overrides |
|---|---|---|---|---|
| Engineer | Yes | No | No | No |
| Senior Engineer | Yes | Propose only | Yes (1 of 2) | SEV3 only |
| Team Lead | Yes | Propose + review | Yes (1 of 2) | SEV2 + SEV3 |
| Architecture Owner | Yes | Full access | Yes (either) | All SEVs |

## Amendment Review Protocol

- **Quorum:** 2 approvers required for any `constitutional/` change
- **Self-review prohibition:** Author cannot be an approver
- **Enforcement module changes:** Require Architecture Owner approval
  (per `enforcement_integrity.yaml`)
- **Emergency amendments:** 1 approver + mandatory 48-hour retrospective

## Audit Review Cadence

| Review | Frequency | Participants | Deliverable |
|---|---|---|---|
| Override audit | Monthly | Team Lead + 1 | Override log review, verify all expired |
| Exemption audit | Quarterly | Architecture Owner | Exemption justification revalidation |
| Corpus coverage review | Quarterly | 2 engineers | Edge case gap analysis per `corpus_governance.yaml` |
| Budget utilization review | Quarterly | Team Lead | Current vs max for all budget categories |
| Full constitutional review | Annually | Full team | Architecture fitness assessment |

## Organizational Succession

- Constitutional knowledge must be held by minimum 2 people
- Architecture Owner role has documented succession plan
- All `decision_archive/` entries are self-contained (readable without oral tradition)
- `bootstrap_protocol.md` is executable by any engineer with DB access

## Anti-Patterns to Monitor

1. **Amendment velocity creep** — more than 3 amendments/month signals instability
2. **Exemption accumulation** — approaching `max_per_file` (5) signals rule problems
3. **Override frequency** — approaching quarterly cap (6) signals operational issues
4. **Rubber-stamp reviews** — amendments approved in <1 hour signal disengagement
5. **Spec version stagnation** — no version bump in 6+ months signals drift risk
6. **Budget ceiling approach** — any budget >80% utilization triggers proactive review

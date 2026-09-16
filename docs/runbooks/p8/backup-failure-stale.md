# P8 runbook — Backup failure or stale backup

## Alert and ownership

Alert: `DoersBackupFailureOrStale`  
Owner: `data-reliability`  
Failure mode: `backup_failure_or_stale_backup`

## Customer impact

The recovery point objective may no longer be trustworthy if infrastructure backups fail, become stale or stop reporting. Application telemetry is evidence only; it cannot manufacture backup success.

## Evidence to preserve

Preserve infrastructure backup job IDs, timestamps, backup class, failure reason, storage/retention evidence, restore-verification evidence, last known good recovery point and the application-ingested backup age/failure signal.

## Diagnosis

1. Verify the alert against the infrastructure-owned backup system rather than application state alone.
2. Identify whether the issue is job failure, stale successful backup, missing telemetry ingress, storage/retention failure or restore-verification failure.
3. Determine the last independently verified successful backup and its age.
4. Check backup storage accessibility, encryption/key dependencies and retention policy evidence.
5. Distinguish a backup failure from an observability-pipeline failure before declaring the recovery point lost.
6. If a new backup is run, verify it through the normal infrastructure-owned path and preserve its evidence.

## Safe first actions

- Trigger/retry only the approved infrastructure backup workflow when permitted by operational policy.
- Preserve the last known good recovery point until a newer backup is independently verified.
- Perform or schedule restore verification according to the existing data-reliability process.
- Escalate storage/KMS/infrastructure dependency failures to their owners.

## Forbidden actions

- Never emit or record a fabricated application-side backup success.
- Never delete the last known good backup to make retention metrics look normal.
- Never change production data to test whether a backup contains it.
- Never weaken encryption, retention or access controls to complete a backup.

## Escalation

Escalate when database backup age exceeds 30 hours, two consecutive approved backup attempts fail, restore verification is unavailable/failing, or the last known good recovery point cannot be independently located.

## Recovery verification

Verify the infrastructure backup system reports a successful current backup, age returns within the 24-hour freshness objective, restore verification is available/passed for the required class, storage/encryption evidence is intact, and application telemetry reflects—but does not originate—the verified result.

# PAY-18 Runbook — Credential Compromise

## Alert and ownership

Alert: `DoersPay18CredentialCompromise`  
Owner: `finance-security`  
Failure mode: `credential_compromise`

This runbook is operational guidance only. PostgreSQL durable financial state,
provider evidence, PAY-16 audit evidence, and certified state machines remain
authoritative.

## Customer impact

Repeated invalid webhook signatures or critical privileged-Finance security events may indicate stolen credentials, forged provider traffic, or an abused administrative session.

## Evidence to preserve

Preserve PAY-16 tamper-evident security audit rows, sanitized structured logs, signature-failure counts/timestamps, session-family revocation evidence, provider secret-version metadata, deployment/configuration audit evidence, and incident timeline. Never copy raw secrets.

## Diagnosis

1. Determine whether the alert is driven by invalid webhook signatures, privileged Finance security events, or both.
2. Validate provider webhook secret configuration/version and rotation status without displaying the secret value.
3. Review PAY-16 chained audit events and bounded event categories for unauthorized privileged actions.
4. Identify affected session families/actors through authorized security tooling and correlate only with sanitized Finance correlation in shared logs.
5. Check for environment/account confusion, unexpected configuration deployment, or secret exposure in external systems.
6. Review provider dashboard/security events and webhook source behavior for active forgery or replay patterns.

## Safe first actions

- Revoke affected sessions and rotate compromised provider/application secrets through the approved rotation mechanism.
- Keep high-risk Finance writes fail-closed while revocation or credential truth is uncertain.
- Preserve audit chain and configuration/deployment evidence before changing credentials.

## Forbidden actions

- Never paste, log, email, or ticket raw webhook secrets, tokens, passwords, signatures, PAN/CVV, UPI PIN, or bank credentials.
- Never disable signature verification, recent-auth, CSRF, revocation, maker-checker, or velocity controls during investigation.
- Never delete security audit rows or rewrite financial history to conceal suspected unauthorized activity.

## Escalation

Treat confirmed secret exposure, unauthorized privileged activity, or sustained forgery as a security incident. Escalate to finance-security, platform security, provider security, and incident command according to organizational severity policy.

## Recovery verification

Verify old credentials/sessions are revoked, rotated secrets work through the supported overlap window, invalid-signature rate returns to baseline, PAY-16 audit chain validates, no unauthorized financial effect occurred, and all affected money state is reconciled before normal high-risk operations resume.

Recovery does not authorize merge, release, deployment, production credentials,
live-provider enablement, or direct financial-state mutation.

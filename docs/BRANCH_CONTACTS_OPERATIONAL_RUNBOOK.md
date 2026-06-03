# Branch Contacts Operational Runbook

This document serves as the complete operational runbook for the Branch Contact Information system. It explains the current production behavior for future developers, operators, and AI agents.

## 1. Feature Overview
This module strictly manages **branch-level contact information only**. Its capabilities include:
- Branch phone contacts
- Branch email contacts
- WhatsApp/SMS/voice/fax capability metadata
- Visibility scoping (public/internal/management/emergency/billing)
- Enforcing one primary phone and one primary email per branch
- Soft delete functionality
- Comprehensive audit logging
- Strict tenant/org isolation through PostgreSQL Row-Level Security (RLS)

**Explicit Non-Goals (What this is NOT yet):**
- A universal contact platform
- A member contact system
- A staff contact system
- An OTP verification system
- A WhatsApp Business API integration

## 2. Data Model Summary

### `branch_contacts`
Core table for storing branch contacts.
- `id`: UUID primary key
- `org_id`: UUID for tenant isolation (RLS)
- `branch_id`: UUID linking contact to a specific branch
- `contact_kind`: Enum restricting to `phone` or `email`
- `phone_e164`: Standardized E.164 phone format
- `normalized_digits`: Digits-only phone format for searching
- `display_format`: Localized display format for the phone number
- `country_code`: ISO 3166-1 alpha-2 code
- `email_raw`: Original email string
- `email_normalized`: Lowercased and punycode-normalized email
- `contact_label`: Human-readable label (e.g., "Main", "Billing")
- `visibility_scope`: Enum (public, internal, management, emergency, billing)
- `channel_capabilities`: JSONB metadata for capabilities (WhatsApp, SMS, etc.)
- `is_whatsapp_enabled`: Generated boolean column extracted from `channel_capabilities`
- `is_primary`: Boolean indicating if this is the primary contact for its kind
- `is_active`: Boolean indicating if the contact is currently active
- Verification fields: `email_reachability_verified`, `verified_at`, `verification_method`
- Audit columns: `created_at`, `created_by`, `updated_at`, `updated_by`, `deleted_at`, `deleted_by`

### `branch_contacts_audit`
- Partitioned audit table capturing all changes.
- **Append-only behavior**: Triggers prevent updates and deletes.
- Stores `changed_fields` in JSONB format.
- Avoids unnecessary raw PII exposure where possible (masks or redacts based on configuration).

## 3. Contact Kinds
Only two contact kinds are allowed:
- `phone`
- `email`

**Crucial Distinction:** `whatsapp` is **NOT** a `contact_kind`. WhatsApp is a capability that belongs inside the `channel_capabilities` JSONB structure.

**Example Payload:**
```json
{
  "contact_kind": "phone",
  "phone_number": "9876543210",
  "country_code": "IN",
  "channel_capabilities": {
    "whatsapp": true,
    "sms": true,
    "voice": true,
    "fax": false
  }
}
```

## 4. India-First Phone Normalization
The system is currently configured for an **India-first product stage**:
- The default phone parsing region is `IN`.
- A local Indian number like `9876543210` automatically normalizes to `+919876543210`.
- Explicit `+91` numbers remain valid.
- Future global support should be added deliberately (e.g., passing explicit `country_code` overrides), rather than blindly changing defaults.

## 5. Visibility Scopes
Contacts are categorized by intended usage:
- `public`: Visible to customers and members.
- `internal`: Visible only for staff and internal operations.
- `management`: Restricted to branch/org management users.
- `emergency`: Urgent operational contacts.
- `billing`: Invoice, payment, and account-related contacts.

## 6. Primary Contact Rules
- Invariant: Exactly **one primary phone** and **one primary email** must exist per active branch.
- The promote API endpoint atomically demotes the old primary and promotes the target to `is_primary = TRUE`.
- The database heavily protects this invariant with:
  - Statement-level DB triggers that auto-promote an older active contact if a primary is deleted or demoted.
  - A unique partial index (`uq_primary_contact_guard_idx`) guaranteeing no two contacts of the same kind can be primary simultaneously.
- Deleted or inactive contacts cannot be promoted to primary.

## 7. Soft Delete Behavior
The `DELETE` endpoint does **not** hard delete rows from the database.
Instead, it performs a soft delete by setting:
- `is_active = false`
- `is_primary = false`
- `deleted_at = now()`
- `deleted_by = current_user_id` (if available via context)

**Invariant:** Deleted contacts **must not be resurrected**. To reactivate a contact, you must insert a new contact row.

## 8. Audit Behavior
- All `INSERT`, `UPDATE`, and `DELETE` actions are audited automatically via DB triggers.
- Audit records are strictly append-only. They cannot be updated or deleted.
- The audit API endpoint ensures tenant and resource authorization by validating the `branch_id` and `contact_id` relationship before returning rows.
- The `changed_fields` JSON payload may obfuscate or hide raw phone/email values to minimize PII exposure in the logs.

## 9. RLS and Session Context Requirements
PostgreSQL Row-Level Security (RLS) policies are active on the tables. The following session variables must be present:
- `app.current_org_id`
- `app.current_user_id`
- `app.request_id`
- `app.ip_address`
- `app.user_agent`
- `app.change_reason`

The application **must** set these transaction-locally:
```sql
SELECT set_config('app.current_org_id', :org_id, true);
```
> [!WARNING]
> **Never** use global `SET` (with `is_local=false` or omitting `true`) in an async connection pooling environment, as it may leak context across requests and violate tenant isolation.

## 10. API Contract

- `POST /branches/{branch_id}/contacts`
  Creates a new branch contact (phone or email). Validates normalization rules and capability payloads.

- `GET /branches/{branch_id}/contacts`
  Lists all active contacts for the specified branch.

- `GET /branches/{branch_id}/contacts/{contact_id}`
  Retrieves the detailed information of a specific active contact.

- `PATCH /branches/{branch_id}/contacts/{contact_id}`
  Updates modifiable fields of an existing contact. Critical fields like `contact_kind` are immutable.

- `DELETE /branches/{branch_id}/contacts/{contact_id}`
  Soft deletes the specified contact. Auto-promotes a fallback if the deleted contact was a primary contact.

- `POST /branches/{branch_id}/contacts/{contact_id}/promote`
  Atomically sets the specified contact to primary and demotes the previous primary contact of the same kind.

- `GET /branches/{branch_id}/contacts/{contact_id}/audit`
  Retrieves the immutable audit trail history for the given contact.

## 11. Audit Partition Operations
The `branch_contacts_audit` table is **range partitioned** by `changed_at`.
- Partitions must be pre-created.
- If the `pg_cron` extension is available, it can automate partition creation and dropping.
- If `pg_cron` is unavailable, a DBA or an application cron-like scheduler must call the partition functions manually.

**Manual Partition Creation Command:**
```sql
SELECT app_private.create_branch_contacts_audit_partition(
  date_trunc('month', CURRENT_DATE)::date
);
```

**Manual Retention Cleanup Command (e.g., delete older than 24 months):**
```sql
SELECT app_private.drop_audit_partitions_older_than(24);
```

## 12. Common Errors and Fixes

### A. Valid contact returns 404
**Likely cause:**
`app.current_org_id` was not set before executing the RLS-protected query.
**Fix:**
Ensure `set_session_context` runs before any `SELECT`, `INSERT`, or `UPDATE`.

### B. Duplicate primary contact error
**Likely cause:**
Trying to promote and demote contacts in separate SQL statements. A statement-level trigger auto-promotes in the gap, causing constraints to conflict.
**Fix:**
Use atomic promote update logic (a single `UPDATE` with a `CASE` statement to handle both rows simultaneously).

### C. Audit update forbidden
**Likely cause:**
Code inadvertently mutated the SQLAlchemy ORM object representing the audit log (e.g., modifying `changed_fields`), prompting SQLAlchemy to attempt an `UPDATE` on flush.
**Fix:**
Deserialize JSON strictly within Pydantic (e.g., `field_validator(mode="before")`) without assigning back to the underlying ORM object.

### D. Unknown channel capability rejected
**Cause:**
Only strictly defined capabilities (`whatsapp`, `sms`, `voice`, `fax`) are permitted in the JSON schema.

### E. Local phone parsed incorrectly
**Cause:**
The application may be using the wrong default region (e.g., US) to parse national significant numbers.
**Fix:**
The default region must remain `IN` for the India-first product stage.

## 13. Test Commands
To verify the system's integrity, execute the following commands:
```bash
PYTHONPATH=. ./.venv/bin/python -m compileall app alembic
PYTHONPATH=. ./.venv/bin/pytest tests/test_branch_contacts_schema.py -q
PYTHONPATH=. ./.venv/bin/pytest tests/test_branch_contacts_api.py -q
```
**Current Expected Result:**
- Schema validation tests pass successfully.
- API integration tests pass successfully (no 500s or RLS leaks).

## 14. Future Roadmap — Do Not Implement Yet
The following phases are conceptually designed but strictly out-of-scope for the current implementation:
- Universal `contact_identity` / `contact_methods` platform
- Member, staff, and emergency person contacts
- OTP verification
- WhatsApp Business API integrations
- Email deliverability verifications (SMTP handshake)
- Global country normalization rules (beyond `IN` fallback)

*It is critical that these future phases are not mixed into the current, focused branch contact module.*

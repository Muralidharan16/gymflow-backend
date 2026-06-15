# Platform Billing — Session Security Migration Plan

**Status:** Phase 0 plan only; no implementation changes authorized
**Date:** 15 June 2026

## 1. Current Authentication State

### 1.1 Backend (gymflow-backend)

| Aspect | Current State |
|--------|--------------|
| Token issuance | JWT access_token (30 min) + refresh_token (7 days), Bearer header |
| Cookie support | Backend reads `access_token` from cookie as fallback (TenantMiddleware) |
| Storage | Frontend sends Bearer header; backend also checks `request.cookies.get("access_token")` |
| Token revocation | Redis blacklist for individual tokens (`blacklist:{jti}`) and family revocation (`family_revoked:{family_id}`) |
| CSRF protection | None for cookie-authenticated requests |
| Recent-auth enforcement | None |
| MFA | Not implemented |
| Step-up auth | Not implemented |
| Refresh rotation | Rotation family stored in `auth_sessions` table with family ID; replay/reuse detection via Redis |
| Session tracking | `auth_sessions` and `auth_session_families` tables |
| Logout | Blacklists current token via Redis; frontend clears local storage |

### 1.2 Frontend (doers-frontend)

| Aspect | Current State |
|--------|--------------|
| Token storage | `localStorage` key `auth-storage` via Zustand persist middleware |
| Token format | `{ state: { tokens: { access_token, refresh_token } } }` |
| Token attachment | Axios request interceptor: `Authorization: Bearer <access_token>` |
| Token refresh | Axios response interceptor on 401: POST `/auth/refresh` with `refresh_token` from localStorage |
| Refresh queue | Concurrent requests during refresh are queued and retried |
| Cookie setting | `withCredentials: true` on axios instance |
| Cleanup on failure | Clears `localStorage` (auth-storage, branch-storage, signup-email, signup-poll-token) |

### 1.3 Dual Token Path Issue

The backend accepts tokens from both `Authorization: Bearer` header and `access_token` cookie. The frontend currently uses only the Bearer header from localStorage. This dual path creates:

- Two code paths for token validation
- Inconsistent revocation coverage (cookie path may bypass Redis blacklist check)
- Increased attack surface

## 2. Target Model

### 2.1 Backend target

- Auth cookies: `HttpOnly`, `Secure`, `SameSite=Lax`
- Short-lived access session cookie (15-30 minutes)
- Rotating refresh session cookie (7 days max, family-linked)
- CSRF protection via custom header token for all state-changing cookie-authenticated requests
- Route-level recent-auth check (10-minute window, policy-configurable) for privileged billing actions
- MFA as hard requirement for internal operators, strongly recommended for org owners
- Step-up authentication for: plan change, payment-method change, legal/tax identity change, cancellation, billing-admin management, large exports, internal overrides/refunds
- Dedicated session-revocation endpoint with device/session listing

### 2.2 Frontend target

- No `access_token` or `refresh_token` in `localStorage` or `sessionStorage`
- Auth state in memory via Zustand (no persist middleware for tokens)
- HttpOnly cookies carry the actual credentials (set by backend on login/refresh)
- CSRF token fetched on app init, sent as `X-CSRF-Token` header on all state-changing requests
- Recent-auth timestamp tracked; step-up modal when required
- Theme and UI preferences remain in localStorage (non-sensitive)

## 3. CSRF Strategy

1. On authenticated page load, backend returns CSRF token (bound to session, short-lived, single-use or rotating)
2. Frontend sends `X-CSRF-Token` header on all POST/PATCH/PUT/DELETE
3. Backend validates token per-request; rejects with 403 on mismatch
4. `Origin`/`Referer` checked against allowlist as defense-in-depth
5. `GET`, `HEAD`, `OPTIONS` remain side-effect free
6. Provider webhook routes excluded from CSRF (protected by provider signature verification)

## 4. Token Rotation and Revocation

- Refresh tokens rotate on each use; old token family invalidated
- Redis-based replay/reuse detection with short grace window
- Explicit logout: invalidates session family, clears all tokens, broadcasts revocation
- Device/session UI: owner can view active sessions, revoke specific devices
- Suspicious activity notifications: new device login, password change, billing admin change

## 5. Recent-Auth Requirements

| Action | Window | MFA |
|--------|--------|-----|
| Change payment method | 10 min | Recommended |
| Upgrade/downgrade plan | 10 min | Recommended |
| Cancel subscription | 10 min | Recommended |
| Change legal/tax identity | 10 min | Strongly recommended |
| Large data export (restricted mode) | 10 min | Optional |
| Internal refund/credit/override | 5 min | Mandatory |

## 6. MFA / Step-Up Authentication

- TOTP-based MFA as primary second factor
- SMS/WhatsApp backup codes for recovery
- Internal operators: MFA mandatory before any billing action
- Organization owners: MFA strongly recommended at GA; mandatory before Phase 9 cutover
- Step-up: re-verify credentials (password + MFA if enrolled) before privileged actions
- Recent-auth timestamp recorded server-side; not trusted from client

## 7. Phased Backend Rollout

| Phase | Change | Feature Flag |
|-------|--------|-------------|
| Phase 8 | Add `Set-Cookie` for auth responses alongside Bearer | `AUTH_COOKIE_ISSUANCE` (off) |
| Phase 8 | Add CSRF token endpoint and validation middleware | `AUTH_CSRF_ENFORCEMENT` (off) |
| Phase 8 | Add recent-auth timestamp and gating middleware | `AUTH_RECENT_AUTH` (off) |
| Phase 8 | Add MFA enrollment/verification APIs | `AUTH_MFA` (off) |
| Phase 8 | Add step-up auth endpoint | `AUTH_STEP_UP` (off) |
| Phase 9 | Enable cookie issuance, keep Bearer acceptance | `AUTH_COOKIE_ISSUANCE` (on) |

## 8. Phased Frontend Rollout

| Phase | Change |
|-------|--------|
| Phase 8 | Add CSRF token fetch on app init; attach to mutations |
| Phase 8 | Add recent-auth modal/flow for privileged actions |
| Phase 8 | Add MFA enrollment UI |
| Phase 9 | Remove localStorage token persistence; rely on HttpOnly cookies |
| Phase 9 | Add session/device management page |
| Phase 9 | Remove Bearer token code path from API client |

## 9. Compatibility Period

During Phase 9, the backend will:

- Accept both Bearer header and auth cookie (dual acceptance)
- Issue both Bearer token in response body AND Set-Cookie header
- Frontend progressively migrates from Bearer to cookie-only
- After all tenants on cookie-only, remove Bearer acceptance from tenant routes (internal routes may keep Bearer for service accounts)

## 10. Rollback Plan

Each feature flag can be independently disabled:

- `AUTH_COOKIE_ISSUANCE=off`: stop setting cookies, frontend falls back to Bearer
- `AUTH_CSRF_ENFORCEMENT=off`: CSRF validation becomes no-op
- `AUTH_RECENT_AUTH=off`: recent-auth gate returns success
- `AUTH_MFA=off`: MFA endpoints return not-supported
- `AUTH_STEP_UP=off`: step-up endpoints pass-through

Frontend detection: if CSRF token fetch fails (cookie not set), fall back to Bearer-only mode.

## 11. Security Tests (Phase 8)

- CSRF token missing → 403
- CSRF token wrong → 403
- CSRF token replayed → 403
- CSRF bypass on GET → 200 (allowed)
- Webhook routes exempt from CSRF
- Recent-auth expired → step-up required
- Recent-auth valid → action proceeds
- MFA not enrolled → action blocked for internal operators
- Session revoked → all tokens invalid
- Refresh token replay → family revoked
- localStorage does not contain tokens after migration
- Cookie flags: HttpOnly, Secure, SameSite verified

## 12. Current Phase 0 Status

Phase 0 changes **no** authentication behaviour. All session security features remain in planning.
Current token-in-localStorage, Bearer-header, and trial-lock interceptor behaviour is documented
but unchanged.

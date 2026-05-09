# Frontend Integration Guide: Short Gym ID (`gymu_id`)

This document outlines the recent backend API changes introduced to support short, memorable Gym IDs for authentication.

## 1. Overview
Previously, gym owners were required to use a long UUID (`gym_id`) to log into their workspace. To improve the user experience, the system now automatically generates a short, memorable 8-character ID (e.g., `FIT49218`) called `gymu_id` for every newly registered gym. 

Users can now log in using either this new short ID or their original UUID.

---

## 2. API Changes

### Registration Endpoint
**`POST /auth/register`**

The registration payload remains the same, but the successful `201 Created` response has been updated to include the new `gymu_id` field.

**New Response Format:**
```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "refresh_token": "eyJhbGciOi...",
  "expires_at": "2026-05-04T03:30:00.000Z",
  "gym_id": "81a287c9-db71-48dd-93a2-9eb72d5a50db",   // Existing UUID
  "gymu_id": "FIT49218",                             // NEW: Short Gym ID
  "owner_id": "54ae3038-1885-4eff-aeef-b57147f44ba9"
}
```

**Action Required for Frontend:** 
Update the registration success screen. Once a user successfully signs up, clearly display their `gymu_id` to them (e.g., *"Your Gym ID is FIT49218. Please save this for logging in."*). 

---

### Login Endpoint
**`POST /auth/login`**

The request payload shape has **not changed**. The `gym_id` field in the request body is fully backwards compatible and will now accept **both** formats.

**Supported Request Body:**
```json
{
  "gym_id": "FIT49218",          // Can be the short ID OR the long UUID
  "email": "owner@gym.com",
  "password": "SecurePassword123!"
}
```

**Action Required for Frontend:**
- No API contract changes are required. 
- You may want to update the UI placeholder text or label on the login screen from something like "Enter Gym UUID" to simply "Enter Gym ID (e.g., FIT12345)".

---

## 3. Backwards Compatibility
- Older gyms that registered before this change can still log in using their UUIDs without any interruption.
- Existing JWT tokens remain perfectly valid as they still use the UUID internally.

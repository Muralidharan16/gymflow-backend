# GymFlow Backend API

Production-ready FastAPI backend for multi-tenant Gym Management SaaS.

## Tech Stack
- Python 3.11+ with FastAPI
- PostgreSQL (async via SQLAlchemy + asyncpg)
- Redis (optional, gracefully degrades if unavailable)
- JWT authentication (access + refresh tokens)

## Setup

### 1. Clone and create venv
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env with your credentials
```

### 3. Run locally (dev)
```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Access API docs at: `http://localhost:8000/docs`

## Project Structure

```
app/
├── main.py                 # FastAPI app, startup/shutdown events
├── config.py               # Environment settings
├── database.py             # Async SQLAlchemy setup
├── redis_client.py         # Redis connection with graceful degradation
├── models/
│   └── models.py           # SQLAlchemy ORM models (Gym, Member, Subscription, etc.)
├── schemas/
│   ├── auth.py             # LoginRequest, TokenResponse
│   ├── member.py           # MemberCreate, MemberRead, MemberUpdate
│   ├── subscription.py     # SubscriptionCreate, ExpiringMember
│   ├── attendance.py       # AccessVerifyRequest, AccessVerifyResponse
│   ├── device.py           # DeviceRegisterRequest, DeviceRegisterResponse
│   ├── payment.py          # PaymentCreate, PaymentRead
│   └── dashboard.py        # Dashboard data schemas
├── routers/
│   ├── auth.py             # POST /auth/login, /auth/refresh
│   ├── members.py          # GET/POST/PUT/DELETE /members, POST /enroll-fingerprint
│   ├── subscriptions.py    # GET /subscriptions/expiring, POST /subscriptions, PUT /renew
│   ├── access.py           # POST /access/verify (main bridge endpoint)
│   ├── devices.py          # POST /devices/connect, GET /status, POST /sync-members
│   ├── dashboard.py        # GET /dashboard/today, /attendance, /revenue
│   └── payments.py         # GET/POST /payments
├── services/
│   ├── access_control.py   # Member access validation, attendance logging
│   ├── whatsapp_service.py # Twilio WhatsApp integration
│   └── razorpay_service.py # Razorpay payment integration
└── middleware/
    ├── auth_middleware.py  # JWT token validation, get_current_owner()
    └── tenant_middleware.py # Tenant context (optional)
```

## Key Features

### 1. Authentication
- Gym owner login: `POST /auth/login`
- JWT tokens with refresh flow
- Secure password hashing (bcrypt)

### 2. Multi-tenant Isolation
- Every query filters by `gym_id` from JWT token
- Database Row-Level Security (RLS) optional
- Tenant context enforced at application layer

### 3. Door Access Control (Main Feature)
- Bridge calls: `POST /access/verify` with device token + fingerprint_id
- Validates member subscription status
- Publishes unlock command to Redis pub/sub (for real-time door control)
- Logs all access attempts (granted/denied)
- Sends WhatsApp alerts for expired subscriptions

### 4. Real-time Features
- Redis pub/sub for door commands (`tenant:{gym_id}:door_control`)
- Rate limiting on access endpoint (graceful if Redis unavailable)
- Dashboard with today's entries, expiring members, revenue

### 5. Graceful Degradation
- If Redis unavailable, app still runs (rate limiting and pub/sub skipped)
- Async operations are non-blocking
- Proper error handling and logging

## API Examples

### 1. Gym Owner Login
```bash
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "gym_id": "550e8400-e29b-41d4-a716-446655440000",
    "email": "owner@gym.com",
    "password": "secure_password"
  }'

# Response:
# {
#   "access_token": "eyJhbGc...",
#   "token_type": "bearer",
#   "refresh_token": "eyJhbGc...",
#   "expires_at": "2026-05-02T12:30:00"
# }
```

### 2. Create Member
```bash
curl -X POST http://localhost:8000/members \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Priya Sharma",
    "phone": "+919876543210",
    "email": "priya@example.com"
  }'
```

### 3. Verify Access (Bridge → Backend)
```bash
curl -X POST http://localhost:8000/access/verify \
  -H "X-Bridge-Token: <device_auth_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "550e8400-e29b-41d4-a716-446655440001",
    "fingerprint_id": "fp-member-12345"
  }'

# Response (allowed):
# {
#   "allowed": true,
#   "member_id": "550e8400-e29b-41d4-a716-446655440010",
#   "member_name": "Priya Sharma",
#   "subscription_end": "2026-06-01",
#   "reason": null
# }

# Response (denied):
# {
#   "allowed": false,
#   "member_id": null,
#   "member_name": null,
#   "subscription_end": null,
#   "reason": "no_subscription"
# }
```

### 4. Get Dashboard (Today)
```bash
curl -X GET http://localhost:8000/dashboard/today \
  -H "Authorization: Bearer <access_token>"

# Response:
# {
#   "entries": 42,
#   "revenue": 15000.50,
#   "expiring_members": 5
# }
```

## Database Schema

All tables are created automatically at startup. For production, use Alembic migrations:

```bash
alembic init alembic
alembic revision --autogenerate -m "Initial schema"
alembic upgrade head
```

## Deployment (Railway)

1. Create a new Railway project
2. Connect PostgreSQL and Redis from Railway marketplace
3. Push code to GitHub
4. Connect GitHub repo to Railway
5. Set environment variables from `.env.example`
6. Deploy

```bash
# .env for Railway
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/gymflow
REDIS_URL=redis://:password@host:6379/0
JWT_SECRET=<strong-random-secret>
RAZORPAY_KEY_ID=rzp_test_xxx
RAZORPAY_KEY_SECRET=xxx
TWILIO_ACCOUNT_SID=ACxxx
TWILIO_AUTH_TOKEN=xxx
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
```

## Testing

```bash
# All endpoints available at /docs (Swagger UI)
http://localhost:8000/docs

# Or use ReDoc
http://localhost:8000/redoc
```

## Error Handling

All endpoints return meaningful HTTP status codes:
- `200` OK
- `201` Created
- `204` No Content
- `400` Bad Request (validation)
- `401` Unauthorized (auth)
- `403` Forbidden (permission)
- `404` Not Found
- `429` Too Many Requests (rate limited)
- `500` Server Error

## Performance Notes

- Async database queries via asyncpg (10x faster than sync)
- Connection pooling built-in
- Rate limiting per device (skip if Redis unavailable)
- Indexes on `gym_id`, `member_id`, `fingerprint_id`, `scan_time`
- Partial indexes on subscription end_date for expiry queries

## Next Steps

1. Setup PostgreSQL and Redis (via Railway or Docker)
2. Update `.env` with credentials
3. Create test gym owner and members
4. Integrate with ZKTeco bridge script
5. Deploy to production

---

**Built for GymFlow SaaS** — Multi-tenant gym management platform

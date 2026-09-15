"""
app/main.py
============
FastAPI application entrypoint for the Doers SaaS platform.

Middleware order (add_middleware is applied bottom-to-top, so innermost first):
  TenantMiddleware            ← innermost (runs last on request, first on response)
  IdempotencyMiddleware
  AdaptiveWriteThrottler
  RedisRateLimiterMiddleware
  OpenTelemetryTraceMiddleware
  CorrelationIdMiddleware
  DrainAdmissionMiddleware    ← P7 ordinary-request admission/in-flight boundary
  P7SecurityHeadersMiddleware
  CORSMiddleware
  SystemControlMiddleware     ← outermost; exact system paths bypass business middleware
"""

from __future__ import annotations

import logging.config
import os
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.api_resources import close_api_runtime_resources
from app.core.api_runtime import (
    DrainAdmissionMiddleware,
    P7SecurityHeadersMiddleware,
    SYSTEM_REQUEST_PATHS,
    SystemControlMiddleware,
)
from app.core.config import settings
from app.core.middleware import (
    AdaptiveWriteThrottler,
    CorrelationIdMiddleware,
    EXEMPT_PATHS,
    IdempotencyMiddleware,
    OpenTelemetryTraceMiddleware,
    RedisRateLimiterMiddleware,
    TenantMiddleware,
)
from app.core.redis import init_redis
from app.core.supervisor import platform_lifespan
from app.core.telemetry import sentry_before_send

if os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], before_send=sentry_before_send)

from app.routers import (
    address,
    assets,
    attendance,
    auth,
    branch_contacts,
    branch_lifecycle,
    branch_operating_hours,
    geo,
    gyms,
    imports,
    members,
    notification_webhooks,
    onboarding,
    organizations,
    payments,
    reports,
    staff_roles,
    subscriptions,
    membership_plans,
    member_subscriptions_v2,
)
from app.platform_billing.api import tenant as platform_billing_tenant
from app.platform_billing.api import checkout_options as platform_billing_checkout_options
from app.platform_billing.api import checkout as platform_billing_checkout
from app.platform_billing.api import checkout_simulation as platform_billing_checkout_simulation
from app.finance_core.api import payment_boundary as finance_payment_boundary


# Resend is not a tenant/JWT caller. The exact webhook path bypasses tenant auth,
# while provider authenticity is established from the untouched raw body and
# Svix headers inside the P4C router before any database capability is invoked.
EXEMPT_PATHS.add("/webhooks/notifications/resend")

# P7 system paths are intercepted by the outer SystemControlMiddleware. Keeping
# them exempt is defense-in-depth for alternate ASGI/test composition and does not
# grant drain authority: preStop still requires its dedicated orchestration secret.
EXEMPT_PATHS.update(SYSTEM_REQUEST_PATHS)


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": "%(asctime)s %(name)s %(levelname)s %(message)s"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "default"}},
    "loggers": {
        "doers": {"handlers": ["console"], "level": settings.LOG_LEVEL.upper(), "propagate": False},
    },
}
logging.config.dictConfig(LOGGING_CONFIG)

# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()
    try:
        # Database partition lifecycle is infrastructure-owned (pg_partman).  The
        # ordinary application identity deliberately performs no schema/table DDL
        # during startup. API-local supervised tasks are stopped by platform_lifespan
        # before process-owned Redis/database pools are disposed in the outer finally.
        async with platform_lifespan():
            yield
    finally:
        await close_api_runtime_resources()


# ─────────────────────────────────────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Doers Gym SaaS",
    version="2.0.0",
    description="Enterprise multi-tenant fitness platform",
    lifespan=lifespan,
)

# ── Exception handler ──────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": "An unexpected error occurred."})

# ── Middleware (bottom = innermost, top = outermost for request flow) ──────
# Registration order is reversed — last added is outermost.

app.add_middleware(TenantMiddleware)
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(AdaptiveWriteThrottler)
app.add_middleware(RedisRateLimiterMiddleware)
app.add_middleware(OpenTelemetryTraceMiddleware)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(DrainAdmissionMiddleware)
app.add_middleware(P7SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SystemControlMiddleware)

# ── Static storage ─────────────────────────────────────────────────────────

storage_dir = os.path.join(os.getcwd(), "storage", settings.S3_BUCKET_NAME)
os.makedirs(storage_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=storage_dir), name="static")

# ── Routers ────────────────────────────────────────────────────────────────

app.include_router(auth.router)
app.include_router(branch_contacts.router)
app.include_router(gyms.router)
app.include_router(members.router)
app.include_router(members.modern_router)
app.include_router(subscriptions.router)
app.include_router(payments.router)
app.include_router(attendance.router)
app.include_router(reports.router)
app.include_router(imports.router)
app.include_router(onboarding.router)
app.include_router(organizations.router)
app.include_router(assets.router)
app.include_router(address.router)
app.include_router(address.org_address_router)
app.include_router(address.member_address_router)
app.include_router(staff_roles.router)
app.include_router(branch_operating_hours.router)
app.include_router(branch_lifecycle.router)
app.include_router(geo.router)
app.include_router(membership_plans.router)
app.include_router(member_subscriptions_v2.router)
app.include_router(notification_webhooks.router)
app.include_router(platform_billing_tenant.router)
app.include_router(platform_billing_checkout_options.router)
app.include_router(platform_billing_checkout.router)
app.include_router(platform_billing_checkout_simulation.router)
app.include_router(finance_payment_boundary.router)


# ── Public root ────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"message": "Doers SaaS API v2.0 — Enterprise Edition"}

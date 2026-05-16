# FIXED: [FIX 1] Replaced deprecated @app.on_event("startup")/@app.on_event("shutdown")
#        with a single @asynccontextmanager lifespan function.
# FIXED: [FIX 4] Moved hardcoded CORS origins to settings.CORS_ORIGINS.
# app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.middleware import (
    TenantMiddleware,
    SecurityHeadersMiddleware,
    CorrelationIdMiddleware,
    RateLimitMiddleware,
    IdempotencyMiddleware,
)
from app.routers import auth, gyms, members, subscriptions, payments, attendance, reports, imports, onboarding

# Redis lifecycle helpers (ensure app/core/redis.py implements these)
from app.core.redis import init_redis, close_redis

import logging.config


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown lifecycle events."""
    # ── Startup ──
    await init_redis()
    # add other startup tasks here if needed
    yield
    # ── Shutdown ──
    await close_redis()
    # add other shutdown tasks here if needed


app = FastAPI(title="Doers Gym SaaS", version="1.0.0", lifespan=lifespan)

# --- UNIFIED LOGGING ---
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
        },
    },
    "loggers": {
        "doers": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
logging.config.dictConfig(LOGGING_CONFIG)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong. Please try again."},
    )


# Middlewares (Order matters: Bottom is Outermost, Top is Innermost)
app.add_middleware(TenantMiddleware)
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,  # THIS MUST BE TRUE FOR COOKIES
    allow_methods=["*"],
    allow_headers=["*"],
)


# Include routers
app.include_router(auth.router)
app.include_router(gyms.router)
app.include_router(members.router)
app.include_router(subscriptions.router)
app.include_router(payments.router)
app.include_router(attendance.router)
app.include_router(reports.router)
app.include_router(imports.router)
app.include_router(onboarding.router)


@app.get("/")
async def root():
    return {"message": "Welcome to Doers Gym SaaS API"}


@app.get("/health")
async def health_check():
    """
    Liveness + readiness probe.
    Checks DB connectivity (SELECT 1) and Redis ping.
    Returns HTTP 503 if either dependency is unreachable.
    """
    from sqlalchemy import text as sa_text
    from app.core.database import AsyncSessionLocal
    from app.core.redis import redis_client

    db_ok = False
    redis_ok = False

    # ── DB check ──
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    # ── Redis check ──
    try:
        pong = await redis_client.ping()
        redis_ok = bool(pong)
    except Exception:
        pass

    payload = {
        "status": "healthy" if (db_ok and redis_ok) else "degraded",
        "db": db_ok,
        "redis": redis_ok,
        "version": "1.0.0",
    }

    if not (db_ok and redis_ok):
        return JSONResponse(status_code=503, content=payload)

    return payload

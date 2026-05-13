# app/main.py
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

app = FastAPI(title="Doers Gym SaaS", version="1.0.0")


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong. Please try again."},
    )


# CORS configuration
origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:8000",
]


# Middlewares (Order matters: Bottom is Outermost, Top is Innermost)
app.add_middleware(TenantMiddleware)
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

# CORS must be outermost to catch all errors and add headers
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
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


@app.on_event("startup")
async def on_startup():
    # Initialize Redis utils and preload scripts (no-op if already initialized)
    await init_redis()
    # add other startup tasks here if needed


@app.on_event("shutdown")
async def on_shutdown():
    # Close Redis connections cleanly
    await close_redis()
    # add other shutdown tasks here if needed


@app.get("/")
async def root():
    return {"message": "Welcome to Doers Gym SaaS API"}

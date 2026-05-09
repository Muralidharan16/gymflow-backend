from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.middleware import (
    TenantMiddleware, 
    SecurityHeadersMiddleware, 
    CorrelationIdMiddleware, 
    RateLimitMiddleware,
    IdempotencyMiddleware
)
from app.routers import auth, gyms, members, subscriptions, payments, attendance, reports, imports

app = FastAPI(title="Doers Gym SaaS", version="1.0.0")

# CORS configuration
origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Middlewares (Order matters: Bottom to Top)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(TenantMiddleware)

# Include routers
app.include_router(auth.router)
app.include_router(gyms.router)
app.include_router(members.router)
app.include_router(subscriptions.router)
app.include_router(payments.router)
app.include_router(attendance.router)
app.include_router(reports.router)
app.include_router(imports.router)

@app.get("/")
async def root():
    return {"message": "Welcome to Doers Gym SaaS API"}

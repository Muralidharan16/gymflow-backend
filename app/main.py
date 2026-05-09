from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging
from .config import settings
from .database import engine, Base
from .redis_client import init_redis, close_redis
from .middleware.observability import ObservabilityMiddleware

from .routers import auth, members, subscriptions, access, devices, dashboard, payments, plans, system, organizations

app = FastAPI(title="Doers API", version="1.0.0")

origins = [
    "https://app.gymflow.com",
    "https://admin.gymflow.com",
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
app.add_middleware(ObservabilityMiddleware)


@app.on_event("startup")
async def on_startup():
    logging.getLogger().setLevel(settings.LOG_LEVEL.upper())
    # Schema creation is now managed by Alembic.
    # Do NOT use Base.metadata.create_all() in production.
    # Run: alembic upgrade head
    await init_redis()


@app.on_event("shutdown")
async def on_shutdown():
    await close_redis()


# include routers
app.include_router(auth.router)
app.include_router(members.router)
app.include_router(subscriptions.router)
app.include_router(access.router)
app.include_router(devices.router)
app.include_router(dashboard.router)
app.include_router(payments.router)
app.include_router(plans.router)
app.include_router(system.router)
app.include_router(organizations.router)

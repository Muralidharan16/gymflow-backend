from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from app.core.security import decode_token

EXEMPT_PATHS = {
    "/auth/signup",
    "/auth/login",
    "/auth/refresh",
    "/docs",
    "/openapi.json",
    "/redoc",
}


class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip auth for exempt paths
        if any(request.url.path.startswith(p) for p in EXEMPT_PATHS):
            return await call_next(request)

        # Skip auth for check-access (door lock) endpoints
        if request.url.path.startswith("/check-access/"):
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid Authorization header",
            )

        try:
            payload = decode_token(auth.split(" ")[1])
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            )

        if payload.get("type") != "access":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type",
            )

        request.state.staff_id = payload["sub"]
        request.state.org_id = payload["org_id"]
        request.state.gym_id = payload.get("gym_id")  # None = org-level access
        request.state.role = payload["role"]
        return await call_next(request)

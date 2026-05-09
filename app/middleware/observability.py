from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
import time
import uuid
import logging

logger = logging.getLogger(__name__)

class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        start_time = time.time()
        
        request.state.request_id = request_id
        
        response = await call_next(request)
        
        process_time = (time.time() - start_time) * 1000
        formatted_process_time = f"{process_time:.2f}ms"
        
        logger.info(
            f"RID={request_id} | {request.method} {request.url.path} | Status: {response.status_code} | Time: {formatted_process_time}"
        )
        
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time"] = formatted_process_time
        
        return response

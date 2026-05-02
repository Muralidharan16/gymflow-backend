from fastapi import Request


async def set_tenant_from_request(request: Request):
    """Optional helper to derive tenant from headers or JWT and attach to request.state.

    This can be used by middleware if desired. Not enabled by default in main.py.
    """
    # Example: if Authorization header contains JWT with gym_id claim
    auth = request.headers.get('Authorization')
    if not auth:
        request.state.gym_id = None
        return
    try:
        token = auth.split(' ')[1]
    except Exception:
        request.state.gym_id = None
        return
    # Decoding is intentionally omitted here; use dependencies instead.
    request.state.gym_id = None

import httpx
from ..config import settings


async def create_order(amount_rupees: float, currency: str = 'INR', receipt: str = None) -> dict:
    """Create a Razorpay order (minimal implementation).

    Note: Razorpay expects amount in paise (integer).
    """
    url = 'https://api.razorpay.com/v1/orders'
    amount_paise = int(round(amount_rupees * 100))
    payload = {
        'amount': amount_paise,
        'currency': currency,
        'receipt': receipt or 'receipt_' ,
        'payment_capture': 1
    }
    auth = (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload, auth=auth)
        resp.raise_for_status()
        return resp.json()

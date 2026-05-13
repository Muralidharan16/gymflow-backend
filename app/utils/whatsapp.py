import httpx
from app.core.config import settings

BASE_URL = f"https://graph.facebook.com/v19.0/{settings.WA_PHONE_NUMBER_ID}/messages"
HEADERS = {
    "Authorization": f"Bearer {settings.WA_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}

async def send_whatsapp_template(phone: str, template_name: str, params: list[str]) -> bool:
    """Send a pre-approved template message. phone = 10-digit Indian number."""
    payload = {
        "messaging_product": "whatsapp",
        "to": f"91{phone}",
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "en"},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params]
            }]
        }
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(BASE_URL, headers=HEADERS, json=payload)
        return r.status_code == 200

async def send_whatsapp_document(phone: str, doc_url: str, filename: str) -> bool:
    """Send a PDF document message."""
    payload = {
        "messaging_product": "whatsapp",
        "to": f"91{phone}",
        "type": "document",
        "document": {"link": doc_url, "filename": filename}
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(BASE_URL, headers=HEADERS, json=payload)
        return r.status_code == 200

async def send_whatsapp_message(phone: str, message: str) -> bool:
    """Send a plain text WhatsApp message."""
    payload = {
        "messaging_product": "whatsapp",
        "to": f"91{phone}",
        "type": "text",
        "text": {"body": message}
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(BASE_URL, headers=HEADERS, json=payload)
        return r.status_code == 200


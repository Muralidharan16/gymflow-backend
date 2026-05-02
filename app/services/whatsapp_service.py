import httpx
from typing import Optional
from ..config import settings
from ..models.models import WhatsappLog
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime


async def send_whatsapp(db: AsyncSession, to_phone: str, content: str, member_id: Optional[str] = None, message_type: str = 'reminder') -> dict:
    """Send a WhatsApp message via Twilio and log the event to DB."""
    log = WhatsappLog(member_id=member_id, message_type=message_type, content=content, status='queued')
    db.add(log)
    await db.flush()

    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"
    data = {
        'From': settings.TWILIO_WHATSAPP_FROM,
        'To': f'whatsapp:{to_phone}',
        'Body': content,
    }

    auth = (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(url, data=data, auth=auth)
            if resp.status_code >= 200 and resp.status_code < 300:
                resp_json = resp.json()
                log.status = 'sent'
                log.sent_at = datetime.utcnow()
                await db.commit()
                return {'ok': True, 'sid': resp_json.get('sid')}
            else:
                err = resp.text
                log.status = 'failed'
                await db.commit()
                return {'ok': False, 'error': err}
        except Exception as exc:
            log.status = 'failed'
            await db.commit()
            return {'ok': False, 'error': str(exc)}

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..middleware.auth_middleware import get_current_owner
from datetime import date
from sqlalchemy import text

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get('/today')
async def today_dashboard(owner = Depends(get_current_owner), db: AsyncSession = Depends(get_db)):
    gym_id = owner.gym_id
    # entries today
    q = await db.execute(text("SELECT COUNT(*) FILTER (WHERE access_granted) as entries FROM attendance_logs WHERE gym_id = :gid AND scan_time::date = current_date"), {"gid": str(gym_id)})
    entries = q.scalar() or 0

    # revenue today
    q = await db.execute(text("SELECT COALESCE(SUM(amount),0) FROM payments WHERE gym_id = :gid AND payment_date::date = current_date AND status = 'success'"), {"gid": str(gym_id)})
    revenue = float(q.scalar() or 0)

    # expiring in 3 days
    q = await db.execute(text("SELECT COUNT(*) FROM member_subscriptions ms JOIN members m ON m.id = ms.member_id WHERE m.gym_id = :gid AND ms.status = 'active' AND ms.end_date BETWEEN current_date AND current_date + interval '3 days'"), {"gid": str(gym_id)})
    expiring = q.scalar() or 0

    return {"entries": entries, "revenue": revenue, "expiring_members": expiring}


@router.get('/attendance')
async def attendance_chart(owner = Depends(get_current_owner), db: AsyncSession = Depends(get_db)):
    gid = str(owner.gym_id)
    q = await db.execute(text("SELECT date_trunc('day', scan_time)::date AS day, COUNT(*) FILTER (WHERE access_granted) AS entries, COUNT(*) FILTER (WHERE NOT access_granted) AS denials FROM attendance_logs WHERE gym_id = :gid AND scan_time >= now() - interval '30 days' GROUP BY day ORDER BY day"), {"gid": gid})
    rows = q.fetchall()
    return [{"day": r[0].isoformat(), "entries": int(r[1]), "denials": int(r[2])} for r in rows]


@router.get('/revenue')
async def revenue(owner = Depends(get_current_owner), db: AsyncSession = Depends(get_db)):
    gid = str(owner.gym_id)
    q = await db.execute(text("SELECT date_trunc('month', payment_date)::date AS month, COALESCE(SUM(amount),0) AS total FROM payments WHERE gym_id = :gid AND status = 'success' AND payment_date >= date_trunc('month', now()) - interval '11 months' GROUP BY month ORDER BY month"), {"gid": gid})
    rows = q.fetchall()
    return [{"month": r[0].isoformat(), "total_revenue": float(r[1])} for r in rows]

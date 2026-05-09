from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..middleware.auth_middleware import get_tenant_context, RequireRole
from ..schemas.tenant import TenantContext
from ..models.models import StaffRole
from datetime import date
from sqlalchemy import text

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get('/today')
async def today_dashboard(
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin])),
    db: AsyncSession = Depends(get_db),
):
    org_id = str(context.org_id)

    # Entries today
    q = await db.execute(
        text("SELECT COUNT(*) FILTER (WHERE access_granted) as entries FROM attendance_logs WHERE org_id = :oid AND scan_time::date = current_date"),
        {"oid": org_id},
    )
    entries = q.scalar() or 0

    # Revenue today
    q = await db.execute(
        text("SELECT COALESCE(SUM(amount),0) FROM payments WHERE org_id = :oid AND payment_date::date = current_date AND status = 'success'"),
        {"oid": org_id},
    )
    revenue = float(q.scalar() or 0)

    # Expiring in 3 days
    q = await db.execute(
        text("""
            SELECT COUNT(*) FROM member_subscriptions ms 
            JOIN members m ON m.id = ms.member_id 
            WHERE m.org_id = :oid AND m.deleted_at IS NULL
            AND ms.status = 'active' AND ms.end_date BETWEEN current_date AND current_date + interval '3 days'
        """),
        {"oid": org_id},
    )
    expiring = q.scalar() or 0

    return {"entries": entries, "revenue": revenue, "expiring_members": expiring}


@router.get('/attendance')
async def attendance_chart(
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin])),
    db: AsyncSession = Depends(get_db),
):
    org_id = str(context.org_id)
    q = await db.execute(
        text("""
            SELECT date_trunc('day', scan_time)::date AS day,
                   COUNT(*) FILTER (WHERE access_granted) AS entries,
                   COUNT(*) FILTER (WHERE NOT access_granted) AS denials
            FROM attendance_logs
            WHERE org_id = :oid AND scan_time >= now() - interval '30 days'
            GROUP BY day ORDER BY day
        """),
        {"oid": org_id},
    )
    rows = q.fetchall()
    return [{"day": r[0].isoformat(), "entries": int(r[1]), "denials": int(r[2])} for r in rows]


@router.get('/revenue')
async def revenue(
    context: TenantContext = Depends(RequireRole([StaffRole.owner, StaffRole.admin])),
    db: AsyncSession = Depends(get_db),
):
    org_id = str(context.org_id)
    q = await db.execute(
        text("""
            SELECT date_trunc('month', payment_date)::date AS month,
                   COALESCE(SUM(amount),0) AS total
            FROM payments
            WHERE org_id = :oid AND status = 'success'
            AND payment_date >= date_trunc('month', now()) - interval '11 months'
            GROUP BY month ORDER BY month
        """),
        {"oid": org_id},
    )
    rows = q.fetchall()
    return [{"month": r[0].isoformat(), "total_revenue": float(r[1])} for r in rows]

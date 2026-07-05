import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

@pytest.mark.asyncio
async def test_phase1_stable_function_contract(db_session: AsyncSession):
    """
    Test 1.1: app_private.role_id('manager') and app_private.membership_status_id('org_admin') 
    must return non-null INT values immediately after migrations.
    """
    result = await db_session.execute(text("SELECT app_private.role_id('manager')"))
    role_id = result.scalar()
    assert role_id is not None, "app_private.role_id('manager') returned NULL. This will silently deny all branch manager RLS policies."
    
    result = await db_session.execute(text("SELECT app_private.membership_status_id('org_admin')"))
    status_id = result.scalar()
    assert status_id is not None, "app_private.membership_status_id('org_admin') returned NULL. This will silently deny all org admin RLS policies."




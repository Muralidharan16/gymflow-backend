from fastapi import APIRouter
from ..schemas.saas import SaaSPlanList, SaaSPlanRead, SaaSFeature
from ..models.models import SaaSPlanTier

router = APIRouter(prefix="/system", tags=["system"])

SAAS_PLANS = [
    SaaSPlanRead(
        tier=SaaSPlanTier.basic,
        name="Basic",
        price_monthly=0.0,
        max_branches=1,
        max_members=500,
        pan_required=False,
        features=[
            SaaSFeature(name="Single Branch", description="Manage one gym branch", is_enabled=True),
            SaaSFeature(name="Member Management", description="Up to 500 members", is_enabled=True),
            SaaSFeature(name="Attendance Tracking", description="Basic attendance logs", is_enabled=True),
        ]
    ),
    SaaSPlanRead(
        tier=SaaSPlanTier.pro,
        name="Pro",
        price_monthly=2999.0,
        max_branches=3,
        max_members=2000,
        pan_required=True,
        features=[
            SaaSFeature(name="Multi-Branch", description="Up to 3 branches", is_enabled=True),
            SaaSFeature(name="Advanced Analytics", description="Detailed revenue and attendance charts", is_enabled=True),
            SaaSFeature(name="Custom Reports", description="Export data to CSV/PDF", is_enabled=True),
        ]
    ),
    SaaSPlanRead(
        tier=SaaSPlanTier.elite,
        name="Elite",
        price_monthly=9999.0,
        max_branches=100,
        max_members=None,
        pan_required=True,
        features=[
            SaaSFeature(name="Unlimited Branches", description="Manage your entire gym chain", is_enabled=True),
            SaaSFeature(name="White-labeling", description="Use your own branding and domain", is_enabled=True),
            SaaSFeature(name="Priority Support", description="24/7 dedicated support", is_enabled=True),
        ]
    )
]

@router.get("/plans", response_model=SaaSPlanList)
async def list_saas_plans():
    """List all available SaaS subscription tiers and their features."""
    return SaaSPlanList(plans=SAAS_PLANS)

"""
app/platform_billing/__init__.py
=================================
Platform Billing bounded context root.

This package implements the organization-to-Doers commercial
billing system as defined by the V2 Constitution and V3.1
Execution Specification.

Domain boundary rule:
    No code in this package may import from facility-commerce
    modules (app.models.subscription, app.models.payment,
    app.services.subscription_service, app.services.payment_service,
    member_subscriptions_v2, membership_plans).

    Conversely, facility-commerce code must never import from
    this package to determine Doers platform access.
"""

"""
app/platform_billing/domain/events.py
======================================
Platform Billing domain event type definitions.

Events are inserted transactionally into the outbox alongside
the domain mutation that produced them. Each event type has a
namespaced string identifier used for routing.
"""

from enum import Enum


class PlatformBillingEventType(str, Enum):
    CATALOG_PLAN_VERSION_PUBLISHED = "platform.billing.catalog.plan_version_published"
    SUBSCRIPTION_CHANGED = "platform.billing.subscription.changed"
    ACCESS_CHANGED = "platform.billing.access.changed"
    ENTITLEMENTS_CHANGED = "platform.billing.entitlements.changed"
    INVOICE_ISSUED = "platform.billing.invoice.issued"
    PAYMENT_SUCCEEDED = "platform.billing.payment.succeeded"
    NOTIFICATION_REQUESTED = "platform.billing.notification.requested"

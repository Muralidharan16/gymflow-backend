from __future__ import annotations

from app.platform_billing.domain.money import Money
from app.platform_billing.domain.read_models import (
    AuditEventRead,
    BillingAccountRead,
    FeatureDefinitionRead,
    PlanEntitlementRead,
    PlanVersionRead,
    PolicyVersionRead,
    PriceRead,
    ProductRead,
    SubscriptionEventRead,
    SubscriptionItemRead,
    SubscriptionPeriodRead,
    SubscriptionRead,
)
from app.platform_billing.models.audit import PlatformBillingAuditEvent
from app.platform_billing.models.billing_account import PlatformBillingAccount
from app.platform_billing.models.catalog import (
    PlatformFeatureDefinition,
    PlatformPlanEntitlement,
    PlatformPlanVersion,
    PlatformPolicyVersion,
    PlatformPrice,
    PlatformProduct,
)
from app.platform_billing.models.subscription import (
    PlatformSubscription,
    PlatformSubscriptionEvent,
    PlatformSubscriptionItem,
    PlatformSubscriptionPeriod,
)


def product_to_read(row: PlatformProduct) -> ProductRead:
    return ProductRead(
        id=row.id,
        code=row.code,
        name=row.name,
        description=row.description,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def policy_version_to_read(row: PlatformPolicyVersion) -> PolicyVersionRead:
    return PolicyVersionRead(
        id=row.id,
        code=row.code,
        policy_type=row.policy_type,
        version=row.version,
        payload=row.payload,
        status=row.status,
        payload_sha256=row.payload_sha256,
        published_at=row.published_at,
        created_at=row.created_at,
    )


def price_to_read(row: PlatformPrice) -> PriceRead:
    return PriceRead(
        id=row.id,
        plan_version_id=row.plan_version_id,
        code=row.code,
        money=Money(amount_minor=row.amount_minor, currency_code=row.currency_code),
        country_code=row.country_code,
        billing_interval=row.billing_interval,
        interval_count=row.interval_count,
        tax_behavior=row.tax_behavior,
        status=row.status,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        provider_price_hint=row.provider_price_hint,
        published_at=row.published_at,
        created_at=row.created_at,
    )


def feature_definition_to_read(row: PlatformFeatureDefinition) -> FeatureDefinitionRead:
    return FeatureDefinitionRead(
        id=row.id,
        key=row.key,
        display_name=row.display_name,
        value_type=row.value_type,
        enforcement_mode=row.enforcement_mode,
        unit=row.unit,
        description=row.description,
        status=row.status,
        created_at=row.created_at,
    )


def plan_entitlement_to_read(row: PlatformPlanEntitlement) -> PlanEntitlementRead:
    feature_key = row.feature_definition.key if row.feature_definition is not None else None
    return PlanEntitlementRead(
        id=row.id,
        plan_version_id=row.plan_version_id,
        feature_definition_id=row.feature_definition_id,
        feature_key=feature_key,
        value_type=row.value_type,
        value_boolean=row.value_boolean,
        value_integer=row.value_integer,
        value_string=row.value_string,
        value_json=row.value_json,
        created_at=row.created_at,
    )


def plan_version_to_read(
    row: PlatformPlanVersion,
    *,
    include_prices: bool = False,
    include_entitlements: bool = False,
) -> PlanVersionRead:
    return PlanVersionRead(
        id=row.id,
        product_id=row.product_id,
        version=row.version,
        code=row.code,
        display_name=row.display_name,
        description=row.description,
        status=row.status,
        trial_policy_version_id=row.trial_policy_version_id,
        dunning_policy_version_id=row.dunning_policy_version_id,
        cancellation_policy_version_id=row.cancellation_policy_version_id,
        downgrade_policy_version_id=row.downgrade_policy_version_id,
        metadata_json=row.metadata_json,
        published_at=row.published_at,
        retired_at=row.retired_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        prices=tuple(price_to_read(price) for price in row.prices) if include_prices else (),
        entitlements=(
            tuple(plan_entitlement_to_read(entitlement) for entitlement in row.entitlements)
            if include_entitlements
            else ()
        ),
    )


def billing_account_to_read(row: PlatformBillingAccount) -> BillingAccountRead:
    return BillingAccountRead(
        id=row.id,
        organization_id=row.organization_id,
        status=row.status,
        legal_name=row.legal_name,
        billing_email=row.billing_email,
        billing_phone_e164=row.billing_phone_e164,
        country_code=row.country_code,
        default_currency_code=row.default_currency_code,
        address_line1=row.address_line1,
        address_line2=row.address_line2,
        city=row.city,
        subdivision=row.subdivision,
        postal_code=row.postal_code,
        tax_registration_type=row.tax_registration_type,
        tax_registration_masked=row.tax_registration_masked,
        tax_verified=row.tax_verified,
        tax_verified_at=row.tax_verified_at,
        invoice_locale=row.invoice_locale,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def subscription_to_read(row: PlatformSubscription) -> SubscriptionRead:
    return SubscriptionRead(
        id=row.id,
        organization_id=row.organization_id,
        billing_account_id=row.billing_account_id,
        status=row.status,
        current_plan_version_id=row.current_plan_version_id,
        current_price_id=row.current_price_id,
        policy_snapshot_json=row.policy_snapshot_json,
        started_at=row.started_at,
        current_period_start=row.current_period_start,
        current_period_end=row.current_period_end,
        cancel_at_period_end=row.cancel_at_period_end,
        cancellation_requested_at=row.cancellation_requested_at,
        cancellation_effective_at=row.cancellation_effective_at,
        canceled_at=row.canceled_at,
        ended_at=row.ended_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def subscription_item_to_read(row: PlatformSubscriptionItem) -> SubscriptionItemRead:
    return SubscriptionItemRead(
        id=row.id,
        organization_id=row.organization_id,
        subscription_id=row.subscription_id,
        item_type=row.item_type,
        plan_version_id=row.plan_version_id,
        price_id=row.price_id,
        quantity=row.quantity,
        effective_from=row.effective_from,
        effective_until=row.effective_until,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def subscription_period_to_read(row: PlatformSubscriptionPeriod) -> SubscriptionPeriodRead:
    return SubscriptionPeriodRead(
        id=row.id,
        organization_id=row.organization_id,
        subscription_id=row.subscription_id,
        period_type=row.period_type,
        status=row.status,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        source_invoice_id=row.source_invoice_id,
        source_change_id=row.source_change_id,
        source_override_id=row.source_override_id,
        metadata_json=row.metadata_json,
        created_at=row.created_at,
    )


def subscription_event_to_read(row: PlatformSubscriptionEvent) -> SubscriptionEventRead:
    return SubscriptionEventRead(
        id=row.id,
        organization_id=row.organization_id,
        subscription_id=row.subscription_id,
        sequence_number=row.sequence_number,
        event_type=row.event_type,
        occurred_at=row.occurred_at,
        recorded_at=row.recorded_at,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        source_type=row.source_type,
        source_id=row.source_id,
        evidence_sha256=row.evidence_sha256,
        payload_json=row.payload_json,
        payload_sha256=row.payload_sha256,
    )


def audit_event_to_read(row: PlatformBillingAuditEvent) -> AuditEventRead:
    return AuditEventRead(
        id=row.id,
        recorded_at=row.recorded_at,
        organization_id=row.organization_id,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        request_id=row.request_id,
        correlation_id=row.correlation_id,
        before_hash=row.before_hash,
        after_hash=row.after_hash,
        metadata_redacted_json=row.metadata_redacted_json,
        outcome=row.outcome,
        reason_code=row.reason_code,
    )

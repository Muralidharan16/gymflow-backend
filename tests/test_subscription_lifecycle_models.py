from sqlalchemy import text

from app.domain.subscription_lifecycle import (
    SubscriptionAssignmentStatus,
    SubscriptionEventType,
    SubscriptionFreezeStatus,
    SubscriptionSeriesStatus,
    SubscriptionSlotRole,
    SubscriptionTermSourceType,
    SubscriptionTermStatus,
)
from app.models.subscription_lifecycle import (
    SubscriptionEvent,
    SubscriptionSeries,
    SubscriptionTerm,
)


async def enum_values(db_session, enum_name: str) -> list[str]:
    result = await db_session.execute(text(f"SELECT unnest(enum_range(NULL::{enum_name}))::text"))
    return list(result.scalars())


async def test_lifecycle_enum_classes_match_database_values(db_session):
    expected = {
        "subscription_series_status": SubscriptionSeriesStatus,
        "subscription_term_status": SubscriptionTermStatus,
        "subscription_term_source": SubscriptionTermSourceType,
        "subscription_slot_role": SubscriptionSlotRole,
        "subscription_assignment_state": SubscriptionAssignmentStatus,
        "subscription_freeze_status": SubscriptionFreezeStatus,
        "subscription_event_type": SubscriptionEventType,
    }

    for enum_name, enum_class in expected.items():
        assert await enum_values(db_session, enum_name) == [item.value for item in enum_class]


def test_lifecycle_models_map_metadata_columns_safely():
    assert "metadata_json" in SubscriptionSeries.__mapper__.attrs
    assert SubscriptionSeries.__table__.c.metadata.name == "metadata"
    assert "metadata_json" in SubscriptionEvent.__mapper__.attrs
    assert SubscriptionEvent.__table__.c.metadata.name == "metadata"
    assert "source_metadata" in SubscriptionTerm.__mapper__.attrs

"""Address regressions with P2D geocoding boundary updates.

The historical address suite is loaded unchanged from address_regression_baseline.py.
Only tests whose contract was intentionally changed by P2D are redefined below;
all other regression functions remain part of this collected module.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import uuid
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from celery.exceptions import MaxRetriesExceededError

_BASELINE_PATH = pathlib.Path(__file__).with_name("address_regression_baseline.py")
_SPEC = importlib.util.spec_from_file_location("_doers_address_regression_baseline", _BASELINE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Unable to load address regression baseline")
_BASELINE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _BASELINE
_SPEC.loader.exec_module(_BASELINE)

for _name, _value in vars(_BASELINE).items():
    if _name.startswith("test_"):
        globals()[_name] = _value

from app.models.address import OrganizationAddress
from app.tasks.geocoding import geocode_address_task


def _worker_geocode_row(
    *,
    address_id: uuid.UUID,
    org_id: uuid.UUID,
    line1: str = "FAIL",
) -> dict[str, object]:
    return {
        "id": address_id,
        "org_id": org_id,
        "address_line1": line1,
        "city": "Chennai",
        "state_province": "TN",
        "postal_code": "600002",
        "country_code": "IN",
        "formatted_address": None,
        "google_place_id": None,
        "validation_status": "pending",
        "geocode_attempts": 0,
        "next_retry_at": None,
    }


def _worker_session_factory_mock() -> tuple[MagicMock, MagicMock]:
    factory = MagicMock()
    db = MagicMock()
    factory.return_value.__enter__.return_value = db
    factory.return_value.__exit__.return_value = False
    return factory, db


def test_listener_address_field_changed() -> None:
    address = OrganizationAddress(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        address_line1="12 Anna Salai",
        city="Chennai",
        state_province="Tamil Nadu",
        postal_code="600002",
        country_code="IN",
        is_verified=True,
        formatted_address="12 Anna Salai, Chennai, TN, 600002, IN",
    )

    with patch("app.tasks.geocoding.geocode_address_task.delay") as mock_task:
        from app.models.address import receive_after_update

        connection = MagicMock()
        receive_after_update(None, connection, address)

    assert connection.execute.call_count == 2
    mock_task.assert_called_once_with(str(address.id), str(address.org_id))


def test_geocoding_retry_on_api_failure() -> None:
    address_id = uuid.uuid4()
    org_id = uuid.uuid4()
    factory, _ = _worker_session_factory_mock()

    with patch("app.core.database.WorkerSyncSessionLocal", factory), \
         patch(
             "app.tasks.geocoding._load_geocoding_input_sync",
             return_value=_worker_geocode_row(address_id=address_id, org_id=org_id),
         ), \
         patch("app.tasks.geocoding.geocode_address_task.retry") as mock_retry:
        geocode_address_task.run(str(address_id), str(org_id))

    mock_retry.assert_called_once()


def test_geocoding_retries_three_times_on_api_failure() -> None:
    address_id = uuid.uuid4()
    org_id = uuid.uuid4()
    factory, _ = _worker_session_factory_mock()
    request = MagicMock()
    request.retries = 2

    with patch("app.core.database.WorkerSyncSessionLocal", factory), \
         patch(
             "app.tasks.geocoding._load_geocoding_input_sync",
             return_value=_worker_geocode_row(address_id=address_id, org_id=org_id),
         ), \
         patch("celery.app.task.Task.request", new_callable=PropertyMock, return_value=request), \
         patch("app.tasks.geocoding.geocode_address_task.retry") as mock_retry:
        geocode_address_task.run(str(address_id), str(org_id))

    assert mock_retry.call_count == 1
    assert mock_retry.call_args.kwargs["countdown"] == 240


def test_geocoding_sets_failed_flag_after_max_retries() -> None:
    address_id = uuid.uuid4()
    org_id = uuid.uuid4()
    factory, _ = _worker_session_factory_mock()
    request = MagicMock()
    request.retries = 3

    with patch("app.core.database.WorkerSyncSessionLocal", factory), \
         patch(
             "app.tasks.geocoding._load_geocoding_input_sync",
             return_value=_worker_geocode_row(address_id=address_id, org_id=org_id),
         ), \
         patch("celery.app.task.Task.request", new_callable=PropertyMock, return_value=request), \
         patch(
             "app.tasks.geocoding.geocode_address_task.retry",
             side_effect=MaxRetriesExceededError("Max retries exceeded"),
         ), \
         patch("app.tasks.geocoding._mark_failure_sync") as mark_failure:
        with pytest.raises(MaxRetriesExceededError):
            geocode_address_task.run(str(address_id), str(org_id))

    kwargs = mark_failure.call_args.kwargs
    assert kwargs["address_id"] == address_id
    assert kwargs["org_id"] == org_id
    assert kwargs["permanent"] is True
    assert kwargs["retry_count"] == 4


def test_geocoding_creates_notification_after_max_retries() -> None:
    address_id = uuid.uuid4()
    org_id = uuid.uuid4()
    factory, _ = _worker_session_factory_mock()
    request = MagicMock()
    request.retries = 3

    with patch("app.core.database.WorkerSyncSessionLocal", factory), \
         patch(
             "app.tasks.geocoding._load_geocoding_input_sync",
             return_value=_worker_geocode_row(address_id=address_id, org_id=org_id),
         ), \
         patch("celery.app.task.Task.request", new_callable=PropertyMock, return_value=request), \
         patch(
             "app.tasks.geocoding.geocode_address_task.retry",
             side_effect=MaxRetriesExceededError("Max retries exceeded"),
         ), \
         patch("app.tasks.geocoding._mark_failure_sync") as mark_failure:
        with pytest.raises(MaxRetriesExceededError):
            geocode_address_task.run(str(address_id), str(org_id))

    assert mark_failure.call_count == 1
    assert "review and re-save" in mark_failure.call_args.kwargs[
        "notification_message"
    ].lower()

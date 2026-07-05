from datetime import date, timedelta

from dateutil.relativedelta import relativedelta


def calculate_subscription_end_date(
    start_date: date,
    duration_value: int,
    duration_unit: str,
) -> date:
    if duration_value <= 0:
        raise ValueError("duration_value must be greater than zero")

    unit = duration_unit.value if hasattr(duration_unit, "value") else str(duration_unit)
    if unit == "days":
        return start_date + timedelta(days=duration_value)
    if unit == "months":
        return start_date + relativedelta(months=duration_value)
    if unit == "years":
        return start_date + relativedelta(years=duration_value)

    raise ValueError("duration_unit must be one of: days, months, years")

"""
app/platform_billing/domain/money.py
=====================================
Platform Billing money primitives.

All monetary values use signed BIGINT minor units and explicit
ISO 4217 currency codes. Floating-point money is forbidden.
Every calculation uses an explicit rounding policy.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from typing import Optional

DEFAULT_ROUNDING = decimal.ROUND_HALF_UP

# Known currency minor-unit exponents (ISO 4217).
# Value is the number of decimal places: INR=2, JPY=0, BHD=3, etc.
# Serves as the canonical lookup during the India-first launch.
_CURRENCY_EXPONENTS: dict[str, int] = {
    "INR": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "JPY": 0,
    "KRW": 0,
    "BHD": 3,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
}


def _get_exponent(currency_code: str) -> int:
    exp = _CURRENCY_EXPONENTS.get(currency_code.upper())
    if exp is None:
        raise ValueError(
            f"Unknown currency exponent for {currency_code!r}. "
            f"Add to _CURRENCY_EXPONENTS before using this currency."
        )
    return exp


@dataclass(frozen=True)
class Money:
    amount_minor: int
    currency_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be an integer (minor units)")
        if not self.currency_code or len(self.currency_code) != 3:
            raise ValueError("currency_code must be a 3-character ISO 4217 code")
        object.__setattr__(self, "currency_code", self.currency_code.upper())
        _get_exponent(self.currency_code)

    @property
    def exponent(self) -> int:
        return _get_exponent(self.currency_code)

    @classmethod
    def from_major(
        cls,
        amount: decimal.Decimal,
        currency_code: str,
        rounding: str = DEFAULT_ROUNDING,
        decimal_places: int | None = None,
    ) -> Money:
        currency_code = currency_code.upper()
        places = decimal_places if decimal_places is not None else _get_exponent(currency_code)
        multiplier = decimal.Decimal(10**places)
        minor = int((amount * multiplier).to_integral_value(rounding))
        return cls(amount_minor=minor, currency_code=currency_code)

    @classmethod
    def zero(cls, currency_code: str) -> Money:
        return cls(amount_minor=0, currency_code=currency_code)

    def to_major(self, decimal_places: int | None = None) -> decimal.Decimal:
        places = decimal_places if decimal_places is not None else self.exponent
        return decimal.Decimal(self.amount_minor) / decimal.Decimal(10**places)

    def __add__(self, other: Money) -> Money:
        self._check_same_currency(other)
        return Money(
            amount_minor=self.amount_minor + other.amount_minor,
            currency_code=self.currency_code,
        )

    def __sub__(self, other: Money) -> Money:
        self._check_same_currency(other)
        return Money(
            amount_minor=self.amount_minor - other.amount_minor,
            currency_code=self.currency_code,
        )

    def __neg__(self) -> Money:
        return Money(amount_minor=-self.amount_minor, currency_code=self.currency_code)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return (
            self.amount_minor == other.amount_minor
            and self.currency_code == other.currency_code
        )

    def __hash__(self) -> int:
        return hash((self.amount_minor, self.currency_code))

    def is_zero(self) -> bool:
        return self.amount_minor == 0

    def is_positive(self) -> bool:
        return self.amount_minor > 0

    def is_negative(self) -> bool:
        return self.amount_minor < 0

    def _check_same_currency(self, other: Money) -> None:
        if self.currency_code != other.currency_code:
            raise ValueError(
                f"Cannot operate on different currencies: "
                f"{self.currency_code} vs {other.currency_code}"
            )

    def __repr__(self) -> str:
        return f"Money({self.amount_minor} {self.currency_code})"


@dataclass(frozen=True)
class TaxRate:
    basis_points: int

    def __post_init__(self) -> None:
        if self.basis_points < 0:
            raise ValueError("Tax rate basis points must be non-negative")

    def apply(self, amount: Money, rounding: str = DEFAULT_ROUNDING) -> Money:
        tax_exact = (
            decimal.Decimal(amount.amount_minor * self.basis_points)
            / decimal.Decimal(10000)
        )
        tax_minor = int(tax_exact.to_integral_value(rounding))
        return Money(amount_minor=tax_minor, currency_code=amount.currency_code)

    @classmethod
    def from_percent(cls, percent: decimal.Decimal) -> TaxRate:
        return cls(basis_points=int(percent * 100))

    def to_percent(self) -> decimal.Decimal:
        return decimal.Decimal(self.basis_points) / decimal.Decimal(100)


ZERO_INR = Money(amount_minor=0, currency_code="INR")


def validate_currency_pair(
    from_currency: str,
    to_currency: str,
    /,
    *,
    allow_same: bool = True,
) -> None:
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if len(from_currency) != 3 or len(to_currency) != 3:
        raise ValueError("currency_code must be a 3-character ISO 4217 code")
    if not allow_same and from_currency == to_currency:
        raise ValueError(
            f"Currency conversion requires different currencies: "
            f"{from_currency} -> {to_currency}"
        )

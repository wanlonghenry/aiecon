"""Amount unit conversion (PLAN.md §7.3, V13).

OpenAI cost results carry ``amount.value`` in currency units with a lowercase ISO-4217
code (``0.06`` / ``"usd"``). Anthropic cost results carry ``amount`` as a decimal string in
the *lowest* currency unit: "Cost amount in lowest currency units (e.g. cents) as a decimal
string. For example, "123.45" in "USD" represents $1.23." aiecon keeps full precision, so
``"123.45"`` cents becomes ``"1.2345"`` USD. Anything with an unknown unit or currency is
kept as reported but excluded from USD totals.
"""

from __future__ import annotations

from decimal import Decimal

USD_UNITS = {"usd", "dollars", "currency_units", "units"}
CENT_UNITS = {"cents", "cent", "lowest_currency_unit"}


class UnsupportedAmountError(ValueError):
    pass


def to_usd(amount_original: Decimal, amount_unit: str | None, currency: str | None) -> Decimal:
    """Convert a reported amount to USD or raise :class:`UnsupportedAmountError`."""

    if currency is None or currency.upper() != "USD":
        raise UnsupportedAmountError(f"currency {currency!r} is not USD")
    unit = (amount_unit or "").strip().lower()
    if unit in USD_UNITS:
        return amount_original
    if unit in CENT_UNITS:
        return amount_original / Decimal(100)
    raise UnsupportedAmountError(f"amount unit {amount_unit!r} is not supported")

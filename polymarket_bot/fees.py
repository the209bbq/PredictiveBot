"""Polymarket US fee schedule effective 12:00 AM ET, Friday 25 Sep 2026.

Source: https://docs.polymarket.us/fees

Standard formula:
    Fee = Θ × C × p × (1 − p)

    Taker Θ = 0.0695 (a cost)
    Maker Θ = 0.0125 (a rebate credited at fill)

Combo taker formula:
    Fee = C × p × [0.0695 × (1 − p) + 0.04 × (1 − p)^4]

Maker rebates on combo fills still use the standard maker formula.

Fees/rebates are rounded to the nearest cent with banker's rounding
(round-half-to-even). A market may publish its own taker coefficient in
`feeCoefficient`; maker rebate theta is unchanged unless the docs change.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

CENTS = Decimal("0.01")
TAKER_THETA = Decimal("0.0695")
MAKER_REBATE_THETA = Decimal("0.0125")
COMBO_TAKER_EXTRA = Decimal("0.04")
MIN_PRICE = Decimal("0.01")
MAX_PRICE = Decimal("0.99")


def _as_decimal(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def round_cents(amount: Decimal | int | float | str) -> Decimal:
    """Banker's rounding to the nearest cent (ROUND_HALF_EVEN)."""
    return _as_decimal(amount).quantize(CENTS, rounding=ROUND_HALF_EVEN)


def _validate(contracts: Decimal, price: Decimal) -> None:
    if contracts < 0:
        raise ValueError("contracts must be >= 0")
    if price < MIN_PRICE or price > MAX_PRICE:
        raise ValueError(f"price must be between {MIN_PRICE} and {MAX_PRICE}")


def uncertainty(price: Decimal | int | float | str) -> Decimal:
    p = _as_decimal(price)
    return p * (Decimal("1") - p)


def exact_taker_fee(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    theta: Decimal | int | float | str = TAKER_THETA,
    combo: bool = False,
) -> Decimal:
    c = _as_decimal(contracts)
    p = _as_decimal(price)
    _validate(c, p)
    one_minus_p = Decimal("1") - p
    t = _as_decimal(theta)
    if combo:
        return c * p * (t * one_minus_p + COMBO_TAKER_EXTRA * (one_minus_p**4))
    return t * c * p * one_minus_p


def exact_maker_rebate(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    theta: Decimal | int | float | str = MAKER_REBATE_THETA,
) -> Decimal:
    c = _as_decimal(contracts)
    p = _as_decimal(price)
    _validate(c, p)
    return _as_decimal(theta) * c * p * (Decimal("1") - p)


def taker_fee(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    theta: Decimal | int | float | str = TAKER_THETA,
    combo: bool = False,
) -> Decimal:
    """Taker cost as a positive Decimal in USD, rounded to cents."""
    return round_cents(exact_taker_fee(contracts, price, theta=theta, combo=combo))


def maker_rebate(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    theta: Decimal | int | float | str = MAKER_REBATE_THETA,
) -> Decimal:
    """Maker rebate as a positive Decimal in USD, rounded to cents."""
    return round_cents(exact_maker_rebate(contracts, price, theta=theta))


def signed_maker_cash(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
) -> Decimal:
    """Cash flow for a maker fill: +rebate."""
    return maker_rebate(contracts, price)


def signed_taker_cash(
    contracts: Decimal | int | float | str,
    price: Decimal | int | float | str,
    *,
    combo: bool = False,
    theta: Decimal | int | float | str = TAKER_THETA,
) -> Decimal:
    """Cash flow for a taker fill: −fee."""
    return -taker_fee(contracts, price, theta=theta, combo=combo)
